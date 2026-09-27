import base64
import contextlib
import json
import logging
import random
import threading
import time
from pathlib import Path

from curl_cffi import requests

import config

logger = logging.getLogger(__name__)


class HttpClient:
    # Akamai bot-management cookies (_abck, bm_*) must be sent: combined with the
    # safari17_0 TLS impersonation below, Akamai accepts the browser's _abck token
    # and returns 200. Stripping them causes Akamai to return 403 on protected
    # endpoints (e.g. /api/v2/epubs/), which surfaces as a spurious "auth" error.
    _AKAMAI_COOKIE_PREFIXES = ("_abck", "bm_", "ak_", "akaalb_")

    def __init__(self, cookies_file: Path | None = None):
        self._auth_cookies: dict = {}
        # curl_cffi sessions are not thread-safe: each thread gets its own,
        # all configured identically and replaying the same auth cookies.
        self._local = threading.local()
        # Global rate limiting across threads, one schedule per lane.
        self._rate_lock = threading.Lock()
        self._next_slot: dict[str, float] = {}

        self._auth_cookies = self._read_cookies(cookies_file or config.COOKIES_FILE)

    @property
    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session(impersonate="safari17_0")
            session.headers.update(config.HEADERS)
            self._local.session = session
        return session

    @staticmethod
    def _read_cookies(path: Path) -> dict:
        """Read the cookie JSON file; returns {} when missing or invalid.

        All cookies are kept, including the Akamai bot-management cookies
        (_abck, bm_*) — they are required to pass Akamai (see class note).
        """
        with contextlib.suppress(OSError, json.JSONDecodeError, ValueError):
            cookies = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(cookies, dict):
                return dict(cookies)
        return {}

    def _apply_auth_cookies(self, session: requests.Session):
        """Reset the session to the original browser cookies before each request.

        Replaying the known-good browser cookies (rather than the evolving set
        Akamai injects via Set-Cookie) keeps every request looking like the
        original browser session."""
        session.cookies.clear()
        session.cookies.update(self._auth_cookies)

    def _rate_limit(self, lane: str):
        """Space request starts by the lane's delay, across all threads."""
        delay = config.ASSET_REQUEST_DELAY if lane == "asset" else config.REQUEST_DELAY
        with self._rate_lock:
            now = time.monotonic()
            start = max(now, self._next_slot.get(lane, 0.0))
            self._next_slot[lane] = start + delay
        if start > now:
            time.sleep(start - now)

    @staticmethod
    def _retry_delay(attempt: int, response=None) -> float:
        """Exponential backoff with jitter, honouring a numeric Retry-After."""
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.strip().isdigit():
                return min(float(retry_after), 60.0)
        return config.RETRY_BACKOFF * (2**attempt) + random.uniform(0, 0.5)

    def get(self, url: str, lane: str = "api", **kwargs) -> requests.Response:
        """GET with rate limiting and retries.

        Retries on network errors and on transient status codes (429/5xx).
        After the last attempt the final response is returned (or the last
        exception raised) so callers can report the real error.
        """
        if not url.startswith("http"):
            url = config.BASE_URL + url
        kwargs.setdefault("timeout", config.REQUEST_TIMEOUT)

        last_exc = None
        for attempt in range(config.MAX_RETRIES):
            self._rate_limit(lane)
            session = self.session
            self._apply_auth_cookies(session)
            is_last = attempt == config.MAX_RETRIES - 1
            try:
                response = session.get(url, **kwargs)
            except Exception as e:  # curl_cffi raises on timeout/connection errors
                last_exc = e
                if not is_last:
                    logger.debug("Request failed (%s), retrying: %s", e, url)
                    time.sleep(self._retry_delay(attempt))
                continue
            if response.status_code in config.RETRY_STATUS_CODES and not is_last:
                logger.debug("HTTP %s, retrying: %s", response.status_code, url)
                time.sleep(self._retry_delay(attempt, response))
                continue
            return response
        raise last_exc

    def get_json(self, url: str, **kwargs) -> dict:
        response = self.get(url, **kwargs)
        self._raise_for_auth_error(response)
        response.raise_for_status()
        return response.json()

    def get_text(self, url: str, **kwargs) -> str:
        response = self.get(url, **kwargs)
        self._raise_for_auth_error(response)
        response.raise_for_status()
        return response.text

    def get_bytes(self, url: str, **kwargs) -> bytes:
        response = self.get(url, **kwargs)
        self._raise_for_auth_error(response)
        response.raise_for_status()
        return response.content

    def _raise_for_auth_error(self, response) -> None:
        """Raise a descriptive RuntimeError on 4xx auth errors instead of raw HTTP errors."""
        if response.status_code == 403:
            if not self._auth_cookies:
                raise RuntimeError(
                    "Not authenticated. Please copy cookies from your browser and POST them to /api/cookies."
                )
            # A 403 has two distinct causes. Only call it "expired" when the JWT
            # actually is; otherwise it is Akamai bot-blocking the request, and
            # telling the user to refresh the *token* is misleading.
            if self._jwt_expired():
                raise RuntimeError(
                    "Session token expired. Please copy fresh cookies from your browser and POST them to /api/cookies."
                )
            raise RuntimeError(
                "Blocked by O'Reilly bot protection (Akamai 403) even though the session "
                "token is still valid. Copy fresh cookies from your browser — including the "
                "_abck and bm_* cookies — and POST them to /api/cookies."
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"HTTP {response.status_code} fetching {response.url}"
            )

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict | None:
        try:
            payload_b64 = token.split(".")[1]
            # JWTs use unpadded base64url ("-" and "_"), not standard base64.
            padded = payload_b64 + "=" * (-len(payload_b64) % 4)
            return json.loads(base64.urlsafe_b64decode(padded))
        except Exception:
            return None

    def get_jwt_status(self) -> dict | None:
        """Return JWT validity info without an HTTP round-trip.

        Returns None if no orm-jwt cookie is present.
        Returns dict with valid/reason/expires_at otherwise.
        """
        token = self._auth_cookies.get("orm-jwt")
        if not token:
            return None
        payload = self._decode_jwt_payload(token)
        if not payload:
            return {"valid": False, "reason": "invalid_token"}
        exp = payload.get("exp", 0)
        expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp))
        if time.time() > exp - 60:
            return {"valid": False, "reason": "token_expired", "expires_at": expires_at}
        return {"valid": True, "reason": None, "expires_at": expires_at}

    def _jwt_expired(self) -> bool:
        status = self.get_jwt_status()
        return status is not None and not status["valid"]

    def reload_cookies(self):
        """Clear and reload cookies from file. Used after browser login."""
        # Every request re-applies _auth_cookies to its thread's session, so
        # swapping the dict (atomically) is enough to update all threads.
        self._auth_cookies = self._read_cookies(config.COOKIES_FILE)

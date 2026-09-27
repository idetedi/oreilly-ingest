"""File system utilities."""

import re
from urllib.parse import unquote, urlsplit


def sanitize_filename(name: str) -> str:
    """Sanitize a string for use as a cross-platform filename."""
    name = name.replace("/", "-").replace("\\", "-")
    name = name.replace(":", " -").replace("?", "").replace("*", "")
    name = name.replace('"', "'").replace("<", "").replace(">", "")
    name = name.replace("|", "-")
    name = name.strip().strip(".")
    if len(name) > 200:
        name = name[:200].strip()
    return name


def image_filename(url: str) -> str:
    """Return the local file name used for an image URL or relative src.

    Shared by the HTML link rewriter and the asset downloader so the
    rewritten <img src> always matches the file saved on disk.
    """
    path = urlsplit(url).path
    name = unquote(path.rsplit("/", 1)[-1])
    return sanitize_filename(name) or "image"


def slugify(name: str) -> str:
    """Convert a string to a URL-friendly slug for folder names."""
    name = name.lower()
    name = re.sub(r"['\"]", "", name)
    name = re.sub(r"[^a-z0-9]+", "-", name)
    name = name.strip("-")
    if len(name) > 100:
        name = name[:100].rstrip("-")
    return name

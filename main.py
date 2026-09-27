#!/usr/bin/env python3
"""O'Reilly Downloader - Main Entry Point"""

import argparse
import logging

from web.server import run_server


def main():
    parser = argparse.ArgumentParser(description="O'Reilly Book Downloader")
    parser.add_argument("--host", default="localhost", help="Server host")
    parser.add_argument("--port", type=int, default=8000, help="Server port")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log HTTP requests and debug details")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # curl_cffi/urllib3 debug output is noise even in verbose mode
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)

    print("=" * 50)
    print("  O'Reilly Downloader")
    print("=" * 50)
    print(f"\n  Open http://{args.host}:{args.port} in your browser\n")
    print("  Press Ctrl+C to stop\n")
    print("=" * 50)

    try:
        run_server(args.host, args.port)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

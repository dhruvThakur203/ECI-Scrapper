"""Command line for the ECI electoral-roll downloader.

CAPTCHAs are solved automatically via OCR (pytesseract + Pillow).
Start with discovery, then download one state:

    python scraper.py --discover
    python scraper.py --discover --state "Goa"
    python scraper.py --dry-run --state "Goa"
    python scraper.py --test-one --state "Goa"
    python scraper.py --run --state "Goa"
    python scraper.py --resume --state "Goa"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from browser.browser_manager import BrowserManager
from eci.checkpoint import Checkpoint
from eci.downloader import DownloadStore
from eci.portal import Portal

import config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download public ECI electoral-roll PDFs. CAPTCHAs are solved automatically."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--discover", action="store_true", help="Inspect the form and APIs. Does not download.")
    mode.add_argument("--run", action="store_true", help="Download PDFs. Requires --state, --state-code, or --all-states.")
    mode.add_argument("--resume", action="store_true", help="Continue a download from state/progress.json.")
    mode.add_argument("--dry-run", action="store_true", help="Enumerate parts and batch counts. Does not download.")
    mode.add_argument("--test-one", action="store_true", help="One roll type, one constituency, one language, one batch.")
    parser.add_argument("--state", help='State name, for example "Goa".')
    parser.add_argument("--state-code", help='State code, for example "S05".')
    parser.add_argument(
        "--all-states",
        action="store_true",
        help="Walk every state. Leave this off until a single state has been checked.",
    )
    return parser


def setup_logging() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("eci")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    file_handler = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(file_handler)
    logger.addHandler(console)


async def async_main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not any([args.discover, args.run, args.resume, args.dry_run, args.test_one]):
        parser.print_help()
        print("\nStart with: python scraper.py --discover")
        return 0
    needs_target = args.run or args.resume or args.dry_run or args.test_one
    if needs_target and not (args.state or args.state_code or args.all_states):
        print("Choose one state with --state or --state-code.")
        print('Example: python scraper.py --test-one --state "Goa"')
        print("Use --all-states only after a single-state run looks right.")
        return 2
    if args.test_one and args.all_states:
        print("--test-one downloads a single batch. Leave off --all-states.")
        return 2

    setup_logging()
    manager = BrowserManager()
    visible = os.environ.get("ECI_HEADLESS", "0") != "1"
    session = await manager.start(visible=visible)
    try:
        portal = Portal(session, Checkpoint(), DownloadStore())
        if args.discover:
            await portal.discover(args.state, args.state_code)
        else:
            await portal.run(
                state_name=args.state,
                state_code=args.state_code,
                all_states=args.all_states and not args.test_one,
                dry_run=args.dry_run,
                test_one=args.test_one,
            )
    finally:
        await manager.stop()
    return 0


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    try:
        raise SystemExit(asyncio.run(async_main(sys.argv[1:])))
    except KeyboardInterrupt:
        print("\nStopped. Completed combinations remain in state/progress.json.")
        raise SystemExit(130)


if __name__ == "__main__":
    main()

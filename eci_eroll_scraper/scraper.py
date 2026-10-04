"""Command line for the ECI electoral-roll downloader.

CAPTCHAs are solved automatically. PDFs go to S3 when configured.

Examples:

    python scraper.py --discover --state "Goa"
    python scraper.py --dry-run --state "Goa"
    python scraper.py --test-one --state "Goa"
    python scraper.py --run --state "Goa"
    python scraper.py --resume --state "Goa"
    python scraper.py --run --all-states
    python scraper.py --resume --all-states
    python scraper.py --status
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
from eci.status_log import StatusLog

import config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download public ECI electoral-roll PDFs. CAPTCHAs are solved automatically."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--discover", action="store_true", help="Inspect the form and APIs. Does not download.")
    mode.add_argument(
        "--run",
        action="store_true",
        help="Download PDFs (auto-skips parts already on S3 / in progress.json). Requires --state or --all-states.",
    )
    mode.add_argument(
        "--resume",
        action="store_true",
        help="Same as --run: continue from state/progress.json + S3, skipping completed work.",
    )
    mode.add_argument("--dry-run", action="store_true", help="Enumerate parts and batch counts. Does not download.")
    mode.add_argument("--test-one", action="store_true", help="One roll type, one constituency, one language, one batch.")
    mode.add_argument("--status", action="store_true", help="Print per-state / per-AC download progress and exit.")
    parser.add_argument("--state", help='State name, for example "Goa".')
    parser.add_argument("--state-code", help='State code, for example "S05".')
    parser.add_argument(
        "--all-states",
        action="store_true",
        help="Walk every state except skips (default skip: NCT OF Delhi).",
    )
    parser.add_argument(
        "--skip-state",
        action="append",
        default=[],
        help='Extra state name to skip with --all-states. Repeatable. Default also skips NCT OF Delhi.',
    )
    parser.add_argument(
        "--include-skipped",
        action="store_true",
        help="With --all-states, do not apply the default NCT OF Delhi skip list.",
    )
    parser.add_argument(
        "--all-languages",
        action="store_true",
        help="Download every language. Default picks English, else Hindi, else any one language.",
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
    if args.status:
        StatusLog().print_summary()
        return 0
    if not any([args.discover, args.run, args.resume, args.dry_run, args.test_one]):
        parser.print_help()
        print("\nStart with: python scraper.py --discover")
        print('All other states: python scraper.py --run --all-states')
        print("Progress:         python scraper.py --status")
        return 0
    needs_target = args.run or args.resume or args.dry_run or args.test_one
    if needs_target and not (args.state or args.state_code or args.all_states):
        print("Choose one state with --state or --state-code, or use --all-states.")
        print('Example: python scraper.py --run --all-states')
        return 2
    if args.test_one and args.all_states:
        print("--test-one downloads a single batch. Leave off --all-states.")
        return 2

    if args.all_languages:
        config.ALL_LANGUAGES = True
        config.ENGLISH_ONLY = False
        os.environ["ECI_ALL_LANGUAGES"] = "1"
        os.environ["ECI_ENGLISH_ONLY"] = "0"

    setup_logging()
    logger = logging.getLogger("eci")
    lang_mode = "all languages" if config.ALL_LANGUAGES or not config.ENGLISH_ONLY else "English→Hindi→any one"
    logger.info(
        "Storage: %s | Language: %s | Preferred year: %s | Fallback year: %s | Checkpoint: %s",
        f"s3://{config.S3_BUCKET}/{config.S3_PREFIX}" if config.S3_ENABLED else f"local:{config.DOWNLOAD_DIR}",
        lang_mode,
        config.REVISION_YEAR,
        config.FALLBACK_YEAR,
        config.PROGRESS_FILE,
    )

    skip_states = set() if args.include_skipped else set(config.SKIP_STATES)
    skip_states.update(args.skip_state)
    skip_codes = set() if args.include_skipped else set(config.SKIP_STATE_CODES)

    manager = BrowserManager()
    visible = os.environ.get("ECI_HEADLESS", "0") != "1"
    session = await manager.start(visible=visible)
    try:
        portal = Portal(session, Checkpoint(), DownloadStore(), StatusLog())
        if args.discover:
            await portal.discover(args.state, args.state_code)
        else:
            await portal.run(
                state_name=args.state,
                state_code=args.state_code,
                all_states=args.all_states and not args.test_one,
                dry_run=args.dry_run,
                test_one=args.test_one,
                skip_states=skip_states,
                skip_state_codes=skip_codes,
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
        print("\nStopped. Progress is in state/progress.json and logs/run_status.json.")
        raise SystemExit(130)


if __name__ == "__main__":
    main()

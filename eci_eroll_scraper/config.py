"""Runtime settings for the ECI electoral-roll downloader."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    """Load .env from project root or scraper folder without requiring python-dotenv."""
    candidates = [
        ROOT.parent / ".env",
        ROOT / ".env",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Do not override variables already set in the process environment.
            os.environ.setdefault(key, value)
        break


_load_dotenv()

PORTAL_URL = "https://voters.eci.gov.in/download-eroll"
REVISION_YEAR = "2026"

# Pause between ordinary navigation steps. CAPTCHA retries are not delayed.
MIN_DELAY = 1.5
MAX_DELAY = 4.0
MAX_RETRIES = 3
# Retries for a single 10-part batch before moving on and reporting a gap.
MAX_BATCH_RETRIES = 5

# The portal rejects more than 10 parts in one submission, and the part
# table shows 10 rows per page. Batches stay inside those windows.
MAX_PARTS_PER_DOWNLOAD = 10
PARTS_PER_PAGE = MAX_PARTS_PER_DOWNLOAD
MIN_PDF_BYTES = 1024

NAVIGATION_TIMEOUT_MS = 60_000
ACTION_TIMEOUT_MS = 30_000
DOWNLOAD_TIMEOUT_S = 180

HEADLESS = os.environ.get("ECI_HEADLESS", "0") == "1"

# Only download English rolls (skip Hindi / other languages).
ENGLISH_ONLY = os.environ.get("ECI_ENGLISH_ONLY", "1") == "1"

DOWNLOAD_DIR = ROOT / "downloads"
CORRUPT_DIR = DOWNLOAD_DIR / "corrupt"
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "scraper.log"
PROGRESS_FILE = ROOT / "state" / "progress.json"
METADATA_FILE = DOWNLOAD_DIR / "metadata.csv"
DISCOVERY_REPORT = LOG_DIR / "last_discovery.json"
GAPS_REPORT = LOG_DIR / "download_gaps.jsonl"

# AWS S3 — when configured, PDFs are stored in the bucket (not kept locally).
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1").strip()
S3_BUCKET = os.environ.get("S3_BUCKET", "").strip()
S3_PREFIX = os.environ.get("S3_PREFIX", "downloads/").strip()
if S3_PREFIX and not S3_PREFIX.endswith("/"):
    S3_PREFIX += "/"
S3_ENABLED = bool(S3_BUCKET and AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY)
# Keep a local copy after S3 upload only if explicitly requested.
KEEP_LOCAL_AFTER_S3 = os.environ.get("ECI_KEEP_LOCAL", "0") == "1"

METADATA_COLUMNS = [
    "state",
    "state_code",
    "year",
    "roll_type",
    "roll_type_id",
    "district",
    "district_code",
    "assembly_constituency",
    "ac_code",
    "language",
    "language_code",
    "part_number",
    "part_name",
    "filename",
    "file_size",
    "download_timestamp",
    "status",
]

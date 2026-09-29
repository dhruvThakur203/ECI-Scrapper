"""Upload existing local downloads/ PDFs to the configured S3 bucket.

Skips keys that already exist with a matching size. Safe to re-run.

    python upload_to_s3.py
    python upload_to_s3.py --workers 8
    python upload_to_s3.py --delete-local   # remove local file after successful upload
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import config
from eci import s3_store

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("eci.upload")


def _iter_pdfs() -> list[Path]:
    root = config.DOWNLOAD_DIR
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("part_*.pdf") if p.is_file())


def _upload_one(path: Path, delete_local: bool, retries: int = 4) -> tuple[str, str]:
    key = s3_store.key_for_local_path(path)
    local_size = path.stat().st_size
    if local_size < config.MIN_PDF_BYTES:
        return "skip_small", key
    client = s3_store.get_s3_client()
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            try:
                head = client.head_object(Bucket=config.S3_BUCKET, Key=key)
                remote_size = int(head.get("ContentLength") or 0)
                if remote_size == local_size:
                    if delete_local:
                        path.unlink(missing_ok=True)
                    return "exists", key
            except Exception:
                pass
            s3_store.upload_file(path, key)
            head = client.head_object(Bucket=config.S3_BUCKET, Key=key)
            remote_size = int(head.get("ContentLength") or 0)
            if remote_size != local_size:
                raise RuntimeError(f"size mismatch after upload local={local_size} remote={remote_size}")
            if delete_local:
                path.unlink(missing_ok=True)
            return "uploaded", key
        except Exception as exc:
            last_error = exc
            import time

            time.sleep(min(2 ** attempt, 20))
    raise RuntimeError(str(last_error))


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload local electoral-roll PDFs to S3")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--delete-local",
        action="store_true",
        help="Delete each local PDF after a successful upload (frees disk).",
    )
    args = parser.parse_args()

    if not config.S3_ENABLED:
        print("S3 is not configured. Check .env for AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_BUCKET.")
        return 2

    files = _iter_pdfs()
    total = len(files)
    print(f"Local PDFs: {total}")
    print(f"Destination: s3://{config.S3_BUCKET}/{config.S3_PREFIX}")
    if total == 0:
        return 0

    counts = {"uploaded": 0, "exists": 0, "skip_small": 0, "error": 0}
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(_upload_one, path, args.delete_local): path for path in files}
        for future in as_completed(futures):
            path = futures[future]
            done += 1
            try:
                status, key = future.result()
                counts[status] = counts.get(status, 0) + 1
                if status == "uploaded" or done % 25 == 0 or done == total:
                    logger.info(
                        "[%s/%s] %s → %s (%s)",
                        done,
                        total,
                        path.name,
                        key,
                        status,
                    )
            except Exception as exc:
                counts["error"] += 1
                logger.error("[%s/%s] FAILED %s: %s", done, total, path, exc)

    print(
        "\n".join(
            [
                "",
                "=== S3 upload complete ===",
                f"uploaded:   {counts.get('uploaded', 0)}",
                f"already:    {counts.get('exists', 0)}",
                f"skip_small: {counts.get('skip_small', 0)}",
                f"errors:     {counts.get('error', 0)}",
                f"bucket:     s3://{config.S3_BUCKET}/{config.S3_PREFIX}",
                "",
            ]
        )
    )
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""AWS S3 helpers for electoral-roll PDF storage."""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

import config

logger = logging.getLogger("eci")


def sanitize(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:140] or "unknown"


@lru_cache(maxsize=1)
def get_s3_client():
    if not config.S3_ENABLED:
        raise RuntimeError("S3 is not configured. Set AWS_* and S3_BUCKET in .env")
    import boto3

    return boto3.client(
        "s3",
        region_name=config.AWS_REGION,
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
    )


def relative_key(*parts: str) -> str:
    """Build an S3 key under S3_PREFIX from path segments."""
    cleaned = [sanitize(part).replace("\\", "/").strip("/") for part in parts if part]
    suffix = "/".join(cleaned)
    return f"{config.S3_PREFIX}{suffix}"


def key_for_local_path(path: Path) -> str:
    """Map a file under downloads/ to the matching S3 key."""
    try:
        relative = path.resolve().relative_to(config.DOWNLOAD_DIR.resolve())
    except ValueError:
        relative = Path(path.name)
    return f"{config.S3_PREFIX}{relative.as_posix()}"


def object_exists(key: str) -> bool:
    client = get_s3_client()
    try:
        client.head_object(Bucket=config.S3_BUCKET, Key=key)
        return True
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        # botocase ClientError 404
        if "404" in str(exc) or "Not Found" in str(exc):
            return False
        raise


def list_part_numbers(prefix: str) -> set[int]:
    """List part_NNN.pdf objects under an S3 prefix."""
    client = get_s3_client()
    found: set[int] = set()
    token = None
    while True:
        kwargs = {"Bucket": config.S3_BUCKET, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        response = client.list_objects_v2(**kwargs)
        for item in response.get("Contents") or []:
            name = item["Key"].rsplit("/", 1)[-1]
            match = re.fullmatch(r"part_(\d+)\.pdf", name, flags=re.IGNORECASE)
            if not match:
                continue
            if int(item.get("Size") or 0) < config.MIN_PDF_BYTES:
                continue
            found.add(int(match.group(1)))
        if not response.get("IsTruncated"):
            break
        token = response.get("NextContinuationToken")
    return found


def upload_bytes(key: str, content: bytes, content_type: str = "application/pdf") -> None:
    client = get_s3_client()
    client.put_object(
        Bucket=config.S3_BUCKET,
        Key=key,
        Body=content,
        ContentType=content_type,
    )


def upload_file(local_path: Path, key: str | None = None) -> str:
    """Upload a local file; returns the S3 key used."""
    client = get_s3_client()
    target = key or key_for_local_path(local_path)
    extra = {"ContentType": "application/pdf"} if local_path.suffix.lower() == ".pdf" else {}
    client.upload_file(
        str(local_path),
        config.S3_BUCKET,
        target,
        ExtraArgs=extra or None,
    )
    return target

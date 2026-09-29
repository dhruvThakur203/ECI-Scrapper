"""Save PDFs (S3 and/or local) and the metadata catalogue. Writes are idempotent."""

from __future__ import annotations

import csv
import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path

from eci.models import Combination, PartRecord
from eci.network import CapturedFile
from eci import s3_store

import config

logger = logging.getLogger("eci")


def sanitize(value: str) -> str:
    return s3_store.sanitize(value)


class DownloadStore:
    def __init__(self) -> None:
        config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self._seen_rows = self._load_existing_rows()
        self._s3_part_cache: dict[str, set[int]] = {}
        if config.S3_ENABLED:
            logger.info(
                "S3 storage enabled: s3://%s/%s",
                config.S3_BUCKET,
                config.S3_PREFIX,
            )

    def directory_for(self, combo: Combination) -> Path:
        return (
            config.DOWNLOAD_DIR
            / sanitize(combo.state)
            / sanitize(combo.roll_type)
            / sanitize(combo.ac)
            / sanitize(combo.language)
        )

    def s3_prefix_for(self, combo: Combination) -> str:
        return s3_store.relative_key(
            combo.state,
            combo.roll_type,
            combo.ac,
            combo.language,
        ).rstrip("/") + "/"

    def s3_key_for(self, combo: Combination, part_number: int) -> str:
        return self.s3_prefix_for(combo) + f"part_{part_number:03d}.pdf"

    def part_path(self, combo: Combination, part_number: int) -> Path:
        return self.directory_for(combo) / f"part_{part_number:03d}.pdf"

    def valid_part_numbers(self, combo: Combination) -> set[int]:
        """Parts already stored (S3 and/or local valid PDFs)."""
        saved: set[int] = set()

        if config.S3_ENABLED:
            prefix = self.s3_prefix_for(combo)
            if prefix not in self._s3_part_cache:
                try:
                    self._s3_part_cache[prefix] = s3_store.list_part_numbers(prefix)
                except Exception:
                    logger.exception("Failed listing S3 parts for %s", prefix)
                    self._s3_part_cache[prefix] = set()
            saved |= self._s3_part_cache[prefix]

        folder = self.directory_for(combo)
        if folder.exists():
            for path in folder.glob("part_*.pdf"):
                match = re.fullmatch(r"part_(\d+)\.pdf", path.name)
                if not match:
                    continue
                number = int(match.group(1))
                if _file_is_valid_pdf(path):
                    saved.add(number)
                else:
                    _quarantine(path)
                    logger.warning("Moved invalid PDF aside: %s", path.name)
        return saved

    def save_batch(
        self,
        combo: Combination,
        parts: list[PartRecord],
        files: list[CapturedFile],
    ) -> list[int]:
        assigned = _assign_files(parts, files)
        saved: list[int] = []
        for part in parts:
            captured = assigned.get(part.part_number)
            if captured is None or not _bytes_are_valid_pdf(captured.content):
                self._append_metadata(combo, part, "", 0, "failed")
                logger.error("Part %s was not saved. The response was not a valid PDF.", part.part_number)
                continue

            # Already on S3 or local?
            already = self.valid_part_numbers(combo)
            if part.part_number in already:
                logger.info("Saved part %s (already present)", part.part_number)
                saved.append(part.part_number)
                continue

            location = ""
            size = len(captured.content)
            try:
                if config.S3_ENABLED:
                    key = self.s3_key_for(combo, part.part_number)
                    s3_store.upload_bytes(key, captured.content)
                    location = f"s3://{config.S3_BUCKET}/{key}"
                    prefix = self.s3_prefix_for(combo)
                    self._s3_part_cache.setdefault(prefix, set()).add(part.part_number)
                    if config.KEEP_LOCAL_AFTER_S3:
                        path = self.part_path(combo, part.part_number)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        self._write(path, captured.content)
                else:
                    path = self.part_path(combo, part.part_number)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if path.exists():
                        _quarantine(path)
                    self._write(path, captured.content)
                    if not _file_is_valid_pdf(path):
                        _quarantine(path)
                        self._append_metadata(combo, part, "", 0, "failed")
                        logger.error("Part %s failed validation after writing", part.part_number)
                        continue
                    location = path.relative_to(config.ROOT).as_posix()
                    size = path.stat().st_size
            except Exception:
                logger.exception("Part %s upload/save failed", part.part_number)
                self._append_metadata(combo, part, "", 0, "failed")
                continue

            self._append_metadata(combo, part, location, size, "success")
            saved.append(part.part_number)
            digest = hashlib.sha256(captured.content).hexdigest()
            logger.info(
                "Saved part %s (%s bytes, %s, sha256 %s) → %s",
                part.part_number,
                size,
                captured.origin,
                digest[:16],
                location or "(local)",
            )
        return saved

    def invalidate_s3_cache(self, combo: Combination | None = None) -> None:
        if combo is None:
            self._s3_part_cache.clear()
            return
        self._s3_part_cache.pop(self.s3_prefix_for(combo), None)

    def _write(self, path: Path, content: bytes) -> None:
        temporary = path.with_suffix(path.suffix + ".partial")
        temporary.write_bytes(content)
        temporary.replace(path)

    def _append_metadata(
        self,
        combo: Combination,
        part: PartRecord,
        location: str,
        file_size: int,
        status: str,
    ) -> None:
        row = {
            "state": combo.state,
            "state_code": combo.state_code,
            "year": combo.year,
            "roll_type": combo.roll_type,
            "roll_type_id": combo.roll_type_id,
            "district": combo.district,
            "district_code": combo.district_code,
            "assembly_constituency": combo.ac,
            "ac_code": combo.ac_number,
            "language": combo.language,
            "language_code": combo.language_code,
            "part_number": str(part.part_number),
            "part_name": part.part_name,
            "filename": location,
            "file_size": str(file_size),
            "download_timestamp": datetime.now().isoformat(timespec="seconds"),
            "status": status,
        }
        key = _row_key(row)
        if key in self._seen_rows_index():
            return
        write_header = not config.METADATA_FILE.exists()
        config.METADATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        with config.METADATA_FILE.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=config.METADATA_COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerow(row)
        self._seen_rows.append(row)

    def _load_existing_rows(self) -> list[dict[str, str]]:
        if not config.METADATA_FILE.exists():
            return []
        with config.METADATA_FILE.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def _seen_rows_index(self) -> set[str]:
        return {_row_key(row) for row in self._seen_rows}


def _row_key(row: dict[str, str]) -> str:
    return "|".join(
        [
            row.get("state_code", ""),
            row.get("year", ""),
            row.get("roll_type_id", ""),
            row.get("ac_code", ""),
            row.get("language_code", ""),
            row.get("part_number", ""),
            row.get("status", ""),
        ]
    )


def _bytes_are_valid_pdf(content: bytes) -> bool:
    return content.startswith(b"%PDF") and len(content) >= config.MIN_PDF_BYTES


def _file_is_valid_pdf(path: Path) -> bool:
    try:
        if path.stat().st_size < config.MIN_PDF_BYTES:
            return False
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def _quarantine(path: Path) -> None:
    config.CORRUPT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    path.replace(config.CORRUPT_DIR / f"{stamp}_{path.name}")


def _assign_files(parts: list[PartRecord], files: list[CapturedFile]) -> dict[int, CapturedFile]:
    candidates = {part.part_number for part in parts}
    assigned: dict[int, CapturedFile] = {}
    unused: list[CapturedFile] = []
    for captured in files:
        if not _bytes_are_valid_pdf(captured.content):
            continue
        number = _part_number_from_file(captured, candidates - set(assigned))
        if number is None:
            unused.append(captured)
        else:
            assigned[number] = captured
    remaining_parts = [part.part_number for part in parts if part.part_number not in assigned]
    if unused and len(unused) == len(remaining_parts):
        for number, captured in zip(remaining_parts, unused):
            assigned[number] = captured
    return assigned


def _part_number_from_file(captured: CapturedFile, candidates: set[int]) -> int | None:
    text = f"{captured.suggested_name} {captured.source_url}"
    hits = []
    for number in sorted(candidates):
        if re.search(rf"(?:^|[^\d]){number}(?:[^\d]|$)", text):
            hits.append(number)
    if len(hits) == 1:
        return hits[0]
    return None

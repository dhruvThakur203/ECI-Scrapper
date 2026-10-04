"""Part-level progress so a crash can resume at the next unfinished part."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from eci.models import Combination

import config


class Checkpoint:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config.PROGRESS_FILE
        self.jobs: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.jobs = {}
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        jobs = data.get("jobs")
        if isinstance(jobs, dict):
            self.jobs = jobs
            return
        self.jobs = {}
        for item in data.get("completed", []):
            if not isinstance(item, dict):
                continue
            combo = Combination.from_dict(item)
            if not combo.state:
                continue
            self.jobs[combo.key()] = _empty_job(combo)
            self.jobs[combo.key()]["completed_parts"] = sorted(set(item.get("parts_saved") or []))
            self.jobs[combo.key()]["status"] = "complete" if item.get("parts_saved") else "in_progress"

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"jobs": self.jobs}
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def completed_parts(self, combo: Combination) -> set[int]:
        job = self.jobs.get(combo.key())
        if not job:
            return set()
        return {int(number) for number in job.get("completed_parts", [])}

    def mark_completed(self, combo: Combination, part_numbers: list[int], expected: list[int]) -> None:
        job = self._ensure(combo, expected)
        saved = set(job["completed_parts"])
        saved.update(int(number) for number in part_numbers)
        failed = set(job["failed_parts"]) - saved
        job["completed_parts"] = sorted(saved)
        job["failed_parts"] = sorted(failed)
        job["last_attempt"] = _now()
        job["status"] = "complete" if expected and set(expected).issubset(saved) else "in_progress"
        self.save()

    def mark_failed(self, combo: Combination, part_numbers: list[int], expected: list[int]) -> None:
        job = self._ensure(combo, expected)
        saved = set(job["completed_parts"])
        failed = set(job["failed_parts"])
        failed.update(int(number) for number in part_numbers if int(number) not in saved)
        job["failed_parts"] = sorted(failed)
        job["last_attempt"] = _now()
        job["status"] = "in_progress"
        self.save()

    def note_attempt(self, combo: Combination, expected: list[int]) -> None:
        job = self._ensure(combo, expected)
        job["last_attempt"] = _now()
        job["status"] = "complete" if expected and set(expected).issubset(set(job["completed_parts"])) else "in_progress"
        self.save()

    def is_complete(self, combo: Combination, expected: list[int] | None = None) -> bool:
        job = self.jobs.get(combo.key())
        if not job:
            return False
        want = list(expected) if expected is not None else list(job.get("expected_parts") or [])
        if not want:
            return job.get("status") == "complete"
        saved = {int(number) for number in job.get("completed_parts", [])}
        return set(want).issubset(saved)

    def _ensure(self, combo: Combination, expected: list[int]) -> dict:
        job = self.jobs.get(combo.key())
        if job is None:
            job = _empty_job(combo)
            self.jobs[combo.key()] = job
        job["expected_parts"] = list(expected)
        job["state_code"] = combo.state_code
        job["roll_type_id"] = combo.roll_type_id
        job["ac_code"] = combo.ac_number
        job["language_code"] = combo.language_code
        job["district"] = combo.district
        job["district_code"] = combo.district_code
        return job


def _empty_job(combo: Combination) -> dict:
    return {
        "completed_parts": [],
        "failed_parts": [],
        "expected_parts": list(combo.parts_expected),
        "last_attempt": "",
        "status": "in_progress",
        "state_code": combo.state_code,
        "roll_type_id": combo.roll_type_id,
        "ac_code": combo.ac_number,
        "language_code": combo.language_code,
        "district": combo.district,
        "district_code": combo.district_code,
    }


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")

"""Per-state / per-constituency download progress and failure log."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from eci.models import Combination

import config


class StatusLog:
    """Live summary of downloaded / left / failed work across states."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config.STATUS_FILE
        self.data: dict[str, Any] = {"updated_at": "", "states": {}}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(payload, dict) and isinstance(payload.get("states"), dict):
            self.data = payload

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = _now()
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def start_state(self, state_name: str, state_code: str, year: str, roll_type_count: int) -> None:
        states = self.data.setdefault("states", {})
        entry = states.get(state_name) or {
            "state_code": state_code,
            "year": year,
            "status": "in_progress",
            "roll_types": roll_type_count,
            "constituencies": {},
            "totals": {"expected": 0, "downloaded": 0, "left": 0, "failed": 0},
            "failures": [],
        }
        entry["state_code"] = state_code
        entry["year"] = year
        entry["status"] = "in_progress"
        entry["roll_types"] = roll_type_count
        entry.setdefault("constituencies", {})
        entry.setdefault("totals", {"expected": 0, "downloaded": 0, "left": 0, "failed": 0})
        entry.setdefault("failures", [])
        states[state_name] = entry
        self.save()

    def skip_state(self, state_name: str, state_code: str, reason: str) -> None:
        states = self.data.setdefault("states", {})
        states[state_name] = {
            "state_code": state_code,
            "year": "",
            "status": "skipped",
            "reason": reason,
            "roll_types": 0,
            "constituencies": {},
            "totals": {"expected": 0, "downloaded": 0, "left": 0, "failed": 0},
            "failures": [],
        }
        self.save()
        _append_failure(
            {
                "timestamp": _now(),
                "scope": "state",
                "state": state_name,
                "state_code": state_code,
                "reason": reason,
            }
        )

    def finish_state(self, state_name: str, *, ok: bool, reason: str = "") -> None:
        entry = self.data.setdefault("states", {}).get(state_name)
        if not entry:
            return
        entry["status"] = "complete" if ok else "failed"
        if reason:
            entry["reason"] = reason
        self._recompute_totals(entry)
        self.save()

    def update_combination(
        self,
        combo: Combination,
        *,
        expected: list[int],
        downloaded: set[int],
        missing: list[int] | None = None,
        failure_reason: str | None = None,
    ) -> None:
        entry = self.data.setdefault("states", {}).setdefault(
            combo.state,
            {
                "state_code": combo.state_code,
                "year": combo.year,
                "status": "in_progress",
                "roll_types": 0,
                "constituencies": {},
                "totals": {"expected": 0, "downloaded": 0, "left": 0, "failed": 0},
                "failures": [],
            },
        )
        entry["year"] = combo.year
        key = f"{combo.roll_type}|{combo.ac}|{combo.language}"
        have = sorted(downloaded)
        left = sorted(missing if missing is not None else [n for n in expected if n not in downloaded])
        ac_entry = {
            "roll_type": combo.roll_type,
            "ac": combo.ac,
            "ac_code": combo.ac_number,
            "language": combo.language,
            "language_code": combo.language_code,
            "year": combo.year,
            "expected": len(expected),
            "downloaded": len(have),
            "left": len(left),
            "status": "complete" if expected and not left else ("failed" if failure_reason else "in_progress"),
            "missing_parts": left[:200],
            "updated_at": _now(),
        }
        if failure_reason:
            ac_entry["last_failure"] = failure_reason
            fail = {
                "timestamp": _now(),
                "scope": "constituency",
                "state": combo.state,
                "state_code": combo.state_code,
                "year": combo.year,
                "roll_type": combo.roll_type,
                "ac": combo.ac,
                "ac_code": combo.ac_number,
                "language": combo.language,
                "downloaded": len(have),
                "left": len(left),
                "expected": len(expected),
                "missing_parts": left[:100],
                "reason": failure_reason,
            }
            entry.setdefault("failures", []).append(fail)
            _append_failure(fail)
        entry.setdefault("constituencies", {})[key] = ac_entry
        self._recompute_totals(entry)
        self.save()

    def record_ac_failure(
        self,
        *,
        state: str,
        state_code: str,
        year: str,
        roll_type: str,
        ac: str,
        ac_code: str,
        reason: str,
    ) -> None:
        entry = self.data.setdefault("states", {}).setdefault(
            state,
            {
                "state_code": state_code,
                "year": year,
                "status": "in_progress",
                "roll_types": 0,
                "constituencies": {},
                "totals": {"expected": 0, "downloaded": 0, "left": 0, "failed": 0},
                "failures": [],
            },
        )
        fail = {
            "timestamp": _now(),
            "scope": "constituency",
            "state": state,
            "state_code": state_code,
            "year": year,
            "roll_type": roll_type,
            "ac": ac,
            "ac_code": ac_code,
            "language": "",
            "downloaded": 0,
            "left": 0,
            "expected": 0,
            "missing_parts": [],
            "reason": reason,
        }
        entry.setdefault("failures", []).append(fail)
        key = f"{roll_type}|{ac}|-"
        entry.setdefault("constituencies", {})[key] = {
            "roll_type": roll_type,
            "ac": ac,
            "ac_code": ac_code,
            "language": "",
            "year": year,
            "expected": 0,
            "downloaded": 0,
            "left": 0,
            "status": "failed",
            "last_failure": reason,
            "missing_parts": [],
            "updated_at": _now(),
        }
        self._recompute_totals(entry)
        self.save()
        _append_failure(fail)

    def print_summary(self) -> None:
        states = self.data.get("states") or {}
        if not states:
            print("No progress recorded yet.")
            return
        print("\n========== DOWNLOAD STATUS ==========")
        print(f"Updated: {self.data.get('updated_at') or '(never)'}")
        for name, entry in states.items():
            totals = entry.get("totals") or {}
            print(
                f"{name} [{entry.get('status')}] year={entry.get('year') or '-'} "
                f"downloaded={totals.get('downloaded', 0)} "
                f"left={totals.get('left', 0)} "
                f"failed_events={totals.get('failed', 0)}"
            )
            for ac_key, ac in (entry.get("constituencies") or {}).items():
                if ac.get("status") == "complete" and not ac.get("left"):
                    continue
                print(
                    f"  - {ac.get('ac')} / {ac.get('language') or '-'} / {ac.get('roll_type')}: "
                    f"{ac.get('downloaded', 0)}/{ac.get('expected', 0)} "
                    f"(left {ac.get('left', 0)}) [{ac.get('status')}]"
                    + (f" :: {ac.get('last_failure')}" if ac.get("last_failure") else "")
                )
        print(f"Full JSON: {self.path}")
        print(f"Failures:  {config.FAILURES_FILE}")
        print("=====================================\n")

    def _recompute_totals(self, entry: dict[str, Any]) -> None:
        expected = downloaded = left = 0
        failed = len(entry.get("failures") or [])
        for ac in (entry.get("constituencies") or {}).values():
            expected += int(ac.get("expected") or 0)
            downloaded += int(ac.get("downloaded") or 0)
            left += int(ac.get("left") or 0)
        entry["totals"] = {
            "expected": expected,
            "downloaded": downloaded,
            "left": left,
            "failed": failed,
        }


def _append_failure(entry: dict[str, Any]) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    with config.FAILURES_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")

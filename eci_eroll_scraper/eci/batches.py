"""Split a full part list into portal-sized download batches."""

from __future__ import annotations

from eci.models import PartRecord

import config


def plan_batches(
    parts: list[PartRecord],
    already_downloaded: set[int],
    size: int | None = None,
) -> list[list[PartRecord]]:
    """Keep the part-list order and the table's 10-row windows.

    Parts 1-48 become 1-10, 11-20, 21-30, 31-40, 41-48. Parts already saved
    are removed inside their window, so a window of 1-10 with 1-3 saved
    submits 4-10 and does not pull part 11 forward.
    """
    window = size or config.MAX_PARTS_PER_DOWNLOAD
    if window < 1:
        raise ValueError("batch size must be at least 1")
    batches: list[list[PartRecord]] = []
    for start in range(0, len(parts), window):
        remaining = [part for part in parts[start : start + window] if part.part_number not in already_downloaded]
        if remaining:
            batches.append(remaining)
    return batches

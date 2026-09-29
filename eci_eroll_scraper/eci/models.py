"""Structured values discovered from the portal."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Option:
    value: str
    text: str


@dataclass(frozen=True)
class StateOption:
    state_name: str
    state_code: str


@dataclass(frozen=True)
class PartRecord:
    part_number: int
    part_name: str
    district_code: str


@dataclass
class Combination:
    state: str
    state_code: str
    year: str
    roll_type: str
    roll_type_id: str
    district: str
    district_code: str
    ac: str
    ac_number: str
    language: str
    language_code: str
    parts_expected: list[int] = field(default_factory=list)
    parts_saved: list[int] = field(default_factory=list)

    def key(self) -> str:
        return "|".join([self.state, self.year, self.roll_type, self.ac, self.language])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Combination":
        known = {item.name for item in cls.__dataclass_fields__.values()}
        payload = {key: value for key, value in data.items() if key in known}
        payload.setdefault("district_code", "")
        return cls(**payload)

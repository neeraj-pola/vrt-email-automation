"""Loads the two knowledge documents that ground the AI's outreach."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Challenge:
    id: int
    name: str
    meaning: str


@lru_cache(maxsize=1)
def vrt_company_text() -> str:
    return (_DIR / "vrt_company.md").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def challenges() -> tuple[Challenge, ...]:
    """Parses the markdown table in smb_challenges.md (| id | name | meaning |)."""
    rows = []
    for line in (_DIR / "smb_challenges.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$", line)
        if m:
            rows.append(Challenge(int(m.group(1)), m.group(2), m.group(3)))
    return tuple(rows)


def challenge_by_id(challenge_id: int) -> Challenge | None:
    return next((c for c in challenges() if c.id == challenge_id), None)


def challenges_prompt_block() -> str:
    return "\n".join(f"{c.id}. {c.name} -- {c.meaning}" for c in challenges())

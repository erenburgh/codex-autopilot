from __future__ import annotations


LEVELS = ("medium", "high", "xhigh", "max")
ALIASES = {"medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max", "ultra": "max"}


def normalize(value: str) -> str:
    normalized = ALIASES.get(value.strip().lower())
    if normalized is None:
        raise ValueError(f"reasoning must be one of {LEVELS}; 'ultra' is accepted as an alias for max")
    return normalized


def next_level(value: str) -> str | None:
    value = normalize(value)
    index = LEVELS.index(value)
    return None if index == len(LEVELS) - 1 else LEVELS[index + 1]


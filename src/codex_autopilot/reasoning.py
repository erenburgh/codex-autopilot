from __future__ import annotations


LEVELS = ("medium", "high", "xhigh", "max")
ALIASES = {"medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max", "ultra": "max"}


def normalize(value: str) -> str:
    normalized = ALIASES.get(value.strip().lower())
    if normalized is None:
        raise ValueError(f"reasoning must be one of {LEVELS}; 'ultra' is accepted as an alias for max")
    return normalized



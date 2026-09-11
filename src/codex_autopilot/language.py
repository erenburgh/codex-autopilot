from __future__ import annotations

import re


DEFAULT_LANGUAGE = "en"

# A deliberately small, safe BCP-47 subset. It covers ordinary language,
# script, and region tags without pretending to validate the complete IANA
# registry. The value is prompt metadata, never a locale or shell argument.
_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


def normalize_language(value: object = DEFAULT_LANGUAGE) -> str:
    """Return a stable language tag used by every task in one run."""

    if not isinstance(value, str):
        raise ValueError("language must be a BCP-47 language tag")
    tag = value.strip().replace("_", "-")
    if not _LANGUAGE_TAG.fullmatch(tag):
        raise ValueError("language must be a BCP-47 language tag such as en, ru, or pt-BR")
    return tag.lower()


def is_russian(language: str) -> bool:
    return normalize_language(language).split("-", 1)[0] == "ru"

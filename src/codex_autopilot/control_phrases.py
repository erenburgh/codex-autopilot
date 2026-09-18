"""The control vocabulary: the words by which a person steers a run.

Kept apart from control.py for two reasons. It is a different concern -
pure text, with no config, state or transport - and control.py stands at
the section-0 limit, where the answer is decomposition, not shorter
explanations.

The names stay re-exported from control: both the hook and the tests have
been importing them from there since before the split.
"""

# The product name spelled the way people actually say it. Russian dictation
# inevitably yields Cyrillic: a control phrase must not depend on whether the
# speaker switched the keyboard layout mid-sentence.
PRODUCT_ALIASES = ("codex autopilot", "кодекс автопайлот", "кодекс автопилот")

# Marks that speech and dictation add without changing the command.
_STRIPPED_PUNCTUATION = ",.!?;:"


# Filler words speech adds at the start without changing the command. The
# list is deliberately short: matching stays exact, or the hook would start
# intercepting ordinary user requests.
_LEADING_FILLERS = frozenset({"просто", "давай", "давайте", "пожалуйста", "just", "please"})


def _normalized_prompt(value: str) -> str:
    text = value.strip().lower()
    for mark in _STRIPPED_PUNCTUATION:
        text = text.replace(mark, " ")
    words = text.split()
    while words and words[0] in _LEADING_FILLERS:
        words.pop(0)
    return " ".join(words)


def _phrases(*templates: str) -> set[str]:
    """Expand the templates over every spelling of the product name."""

    return {
        template.format(product=product)
        for template in templates
        for product in PRODUCT_ALIASES
    }


PAUSE_PROMPTS = _phrases(
    "pause {product}",
    "stop {product}",
    "приостанови {product}",
    "останови {product}",
) | {
    # A bare word is the same intent as with "status": the match covers the
    # whole input, so it cannot land inside a phrase by accident. Uninstall
    # is deliberately not here: it is irreversible and requires the name.
    "останови",
    "пауза",
    "stop",
    "pause",
}
RESUME_PROMPTS = _phrases(
    "resume {product}",
    "continue {product}",
    "возобнови {product}",
    "продолжи {product}",
    "продолжить {product}",
) | {
    "продолжи",
    "возобнови",
    "resume",
    "continue",
}
DETAILED_STATUS_PROMPTS = _phrases(
    "{product} status detail",
    "подробный статус {product}",
) | {
    "подробный статус",
    "статус подробно",
    "detailed status",
    "status detail",
}
STATUS_PROMPTS = _phrases(
    "{product} status",
    "what is {product} doing right now",
    "что сейчас делает {product}",
    "статус {product}",
) | DETAILED_STATUS_PROMPTS | {
    # The skill promises the user exactly one word: "ask `status`". The hook
    # knew four expanded forms and none of this one, and the promised visible
    # path did not work as written. The match covers the whole input, so a
    # bare word is an intent, not an accidental hit inside a phrase.
    "статус",
    "status",
    "статус автопилота",
    # One more word for the same look: in Desktop the run's tasks are visible
    # only after the hook answers, and a person who reaches for "tasks" rather
    # than "status" must not leave empty-handed.
    "задачи",
    "tasks",
    "покажи задачи",
    "show tasks",
}
UNINSTALL_PROMPTS = _phrases(
    "uninstall {product}",
    "remove {product}",
    "удали {product}",
)

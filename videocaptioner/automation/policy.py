"""Policy: steps selection and translation-condition evaluation.

Steps
-----
Valid step names: transcribe, optimize, translate, synthesize
Special alias:    all  →  all four steps in order

The canonical execution order is always:
    transcribe → optimize → translate → synthesize

Condition expressions
---------------------
Grammar (case-insensitive):

    condition  = clause { ("or" | "and") clause }
    clause     = "always" | "never" | "non-cjk" | "non-zh"
               | "is-" LANG_CODE

Examples:
    always
    never
    non-cjk
    is-en
    is-en or is-ja
    non-zh and non-cjk   (same as non-cjk, but explicit)

evaluate_condition(expr, detected_lang) → bool
"""

from __future__ import annotations

import re
from typing import List, Set

# Canonical step order
ALL_STEPS: List[str] = ["transcribe", "optimize", "translate", "synthesize"]
VALID_STEPS: Set[str] = set(ALL_STEPS) | {"all"}

# CJK language codes
_CJK_CODES = {"zh", "zh-hans", "zh-hant", "ja", "ko"}


# ── Step parsing ──────────────────────────────────────────────────────────────

def parse_steps(raw: List[str]) -> List[str]:
    """Validate and expand a list of step tokens.

    Accepts any of: transcribe, optimize, translate, synthesize, all.
    Returns the steps in canonical order, deduplicated.
    Raises ValueError on unknown token.
    """
    expanded: Set[str] = set()
    for token in raw:
        token = token.lower().strip()
        if token == "all":
            expanded.update(ALL_STEPS)
        elif token in VALID_STEPS:
            expanded.add(token)
        else:
            raise ValueError(
                f"Unknown step: {token!r}. "
                f"Valid values: {', '.join(sorted(VALID_STEPS))}"
            )
    # Return in canonical order
    return [s for s in ALL_STEPS if s in expanded]


def steps_summary(steps: List[str]) -> str:
    """Human-readable arrow-separated list, e.g. 'transcribe → optimize → translate'."""
    return " → ".join(steps) if steps else "(none)"


# ── Condition parsing / evaluation ────────────────────────────────────────────

def _normalize_lang(lang: str) -> str:
    """Lowercase and strip region suffix for simple matching: 'zh-Hans' → 'zh'."""
    return lang.lower().split("-")[0].split("_")[0]


def _eval_clause(clause: str, detected_lang: str) -> bool:
    """Evaluate a single condition clause against a detected language code."""
    clause = clause.strip().lower()
    norm = _normalize_lang(detected_lang)
    full = detected_lang.lower()

    if clause == "always":
        return True
    if clause == "never":
        return False
    if clause == "non-cjk":
        return norm not in {"zh", "ja", "ko"} and full not in _CJK_CODES
    if clause == "non-zh":
        return norm != "zh" and not full.startswith("zh")
    if clause.startswith("is-"):
        target = clause[3:]
        return norm == target or full == target or full.startswith(target + "-")
    raise ValueError(f"Unknown condition clause: {clause!r}")


def evaluate_condition(expr: str, detected_lang: str) -> bool:
    """Evaluate a condition expression against the detected audio language.

    The expression is a sequence of clauses joined by 'or' / 'and'.
    Evaluation is left-to-right (no precedence grouping).
    """
    if not expr:
        return True  # default: always translate

    # Tokenise: split by 'or'/'and' while keeping connectors
    tokens = re.split(r"\b(or|and)\b", expr.strip(), flags=re.IGNORECASE)
    tokens = [t.strip() for t in tokens if t.strip()]

    if not tokens:
        return True

    result = _eval_clause(tokens[0], detected_lang)
    i = 1
    while i < len(tokens):
        connector = tokens[i].lower()
        clause = tokens[i + 1] if i + 1 < len(tokens) else "always"
        clause_result = _eval_clause(clause, detected_lang)
        if connector == "or":
            result = result or clause_result
        elif connector == "and":
            result = result and clause_result
        i += 2

    return result


def validate_condition(expr: str) -> None:
    """Raise ValueError if the condition expression is syntactically invalid."""
    # Test against a dummy language code
    try:
        evaluate_condition(expr, "en")
    except ValueError as exc:
        raise ValueError(f"Invalid condition expression: {exc}") from exc

"""Input sanitizer + content screens for CMN-C2-284 (outer backbone pre_process).

Pure, stateless domain helpers (NOT framework gate methods). Four concerns:

- ``sanitize_query``: strip HTML markup and cap length before the raw caller
  text is JSON-serialized and handed to the inner LINE WORKS workflow graph.
- ``TARGET_ID_RE`` / ``MESSAGE_TEXT_RE``: the bounded, inert shapes a
  caller-supplied target id and message body must match before they are
  accepted (docs/02_design.md, "Caller-data contract"). Both render into the
  assembled LINE WORKS request body and back into the caller-facing output, so
  free text here would be caller-controlled output injection. The charset is
  deliberately inert - letters in any script (Japanese message bodies pass
  unchanged), digits, and ordinary sentence punctuation; no markup, quoting,
  path, colon or control characters.
- ``finite_int_in_range``: the parser every caller-controlled number goes
  through - rejects bools, non-numerics, NaN/Infinity, and out-of-range
  magnitudes instead of letting them fail open downstream.
- ``find_injection``: the template-owned prompt-injection screen. The template
  owns this guarantee itself rather than relying on any upstream gate being
  active: chat-template control tokens (``<|im_start|>``, ``[INST]``,
  ``<<SYS>>``, forged role tags) and instruction/role-override phrasing are
  refused in the node that owns the caller contract. Text is normalized first
  (URL-decoding, NFKC, zero-width strip) so escaped or homoglyph variants of
  the same payload do not slip past the patterns.

The screen and the markup strip are ORDERED deliberately: ``find_injection``
runs on the RAW text and again on the sanitized text. ``sanitize_query``
removes ``<...>`` runs, which swallows a control token whole and would forward
its directive residue as ordinary-looking prose; screening before the strip
catches the token, and screening after it catches a directive that only
re-assembles once interleaved markup is removed.
"""

from __future__ import annotations

import math
import re
import unicodedata
import urllib.parse
from typing import Any

_HTML_TAG_RE = re.compile(r"<[^>]+>")

DEFAULT_MAX_LENGTH = 4000

# A caller-supplied LINE WORKS target id (talk room / user / message). Bounded
# and inert; deliberately wider than the ids the bundled stub returns so a
# tenant that issues longer identifiers is not refused at the door.
TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# A caller-supplied message body. `\w` is Unicode-aware, so a Japanese message
# passes unchanged, while markup, quoting, path, colon and control characters
# cannot. Line breaks are allowed (a real message has them); everything that
# could render as anything other than text is not. `\Z` (not `$`) anchors the
# end so a trailing newline cannot smuggle a second line past the check.
_MESSAGE_CHARS = "\\w \\n\\-.,!?'()\\u3002\\u3001\\u30fb\\u300c\\u300d\\uff08\\uff09\\uff01\\uff1f\\u30fc"
MESSAGE_TEXT_RE = re.compile(rf"^[{_MESSAGE_CHARS}]{{1,1000}}\Z")

# Structural cap on the message body a single request may carry (characters);
# mirrors MESSAGE_TEXT_RE's own bound so the two cannot drift apart.
MAX_MESSAGE_LEN = 1000

# Bounds for a caller-supplied target id delivered as a JSON number, and for
# the room-activity page size. These are structural sanity bounds: they exist
# so a non-finite or absurd magnitude is refused at the door rather than
# reaching the LINE WORKS request body.
TARGET_NUMBER_MIN = 0
TARGET_NUMBER_MAX = 10**15
ACTIVITY_LIMIT_MIN = 1
ACTIVITY_LIMIT_MAX = 100
DEFAULT_ACTIVITY_LIMIT = 10

# Bounds for the LINE WORKS call deadline (seconds) declared in config/config.yaml.
TIMEOUT_MIN = 1
TIMEOUT_MAX = 600

_ZERO_WIDTH_RE = re.compile("[\\u200b\\u200c\\u200d\\ufeff\\u00ad]")

# Template-owned injection patterns. Token forms first: a chat-template control
# token is an attack marker regardless of surrounding phrasing, and phrase-only
# screens miss it entirely. Phrases are limited to high-confidence forms so
# legitimate messaging text ("ignore the previous message I sent", "she will
# act as a stand-in host") is unaffected.
_INJECTION_PATTERNS: "tuple[tuple[str, re.Pattern[str]], ...]" = (
    ("chat_template_token", re.compile(r"<\|[A-Za-z0-9_]{1,32}\|>")),
    ("chat_template_token", re.compile(r"\[/?(?:INST|SYS)\]", re.IGNORECASE)),
    ("chat_template_token", re.compile(r"<</?SYS>>", re.IGNORECASE)),
    ("chat_template_token", re.compile(r"<\s*/?(?:system|assistant)\s*>", re.IGNORECASE)),
    (
        "instruction_override",
        re.compile(
            r"(?:ignore|disregard)\s+(?:all\s+|the\s+)?(?:previous|above|prior)\s+"
            r"(?:instructions?|prompts?|context|rules?)",
            re.IGNORECASE,
        ),
    ),
    (
        "instruction_override",
        re.compile(r"\bignore\s+all\s+(?:rules?|instructions?)\b", re.IGNORECASE),
    ),
    # Role-override phrasing is anchored on the SYSTEM-role target, not on the
    # verb: "act as a ..." is ordinary language in a messaging request ("Tell
    # the team Yuki will act as a stand-in host"), so a screen firing on the
    # verb alone would refuse the work this template exists to do. Requiring an
    # assistant/model noun keeps the attack forms and leaves human roles alone.
    (
        "role_override",
        re.compile(
            r"\b(?:act|behave|respond|reply)\s+as\s+(?:a|an|the)\s+(?:[A-Za-z]+\s+){0,2}"
            r"(?:ai|llm|language\s+model|model|assistant|chatbot|bot|dan)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role_override",
        re.compile(
            r"\byou\s+are\s+now\s+(?:a|an|the)\s+(?:[A-Za-z]+\s+){0,2}"
            r"(?:ai|llm|language\s+model|model|assistant|chatbot|bot|dan)\b",
            re.IGNORECASE,
        ),
    ),
    ("role_override", re.compile(r"\b(?:developer|jailbreak|dan)\s+mode\b", re.IGNORECASE)),
)


def _normalize(text: str) -> str:
    """Undo common obfuscation (URL-encoding, homoglyphs, zero-width chars) before scanning."""
    text = urllib.parse.unquote(text)
    text = unicodedata.normalize("NFKC", text)
    return _ZERO_WIDTH_RE.sub("", text)


def find_injection(text: str) -> "list[str]":
    """Return the injection pattern types found in ``text`` ([] = clean).

    Callers refuse on any finding; the finding NAMES the pattern type only -
    the matched text is never included, so nothing hostile is ever echoed.
    """
    normalized = _normalize(text)
    found: list[str] = []
    for pattern_type, pattern in _INJECTION_PATTERNS:
        if pattern_type not in found and pattern.search(normalized):
            found.append(pattern_type)
    return found


def screen_text(text: str) -> "list[str]":
    """Screen a caller string BOTH raw and after the markup strip.

    A markup strip is not a refusal and can make an attack harder to see: it
    removes ``<|im_start|>`` silently and forwards the directive that followed
    it as ordinary text, and it can splice ``ig<b>nore all rules`` back into a
    matchable phrase. Screening both representations catches the token before
    it is eaten and the phrase after it re-assembles.
    """
    findings = find_injection(text)
    for finding in find_injection(sanitize_query(text)):
        if finding not in findings:
            findings.append(finding)
    return findings


def is_non_finite_spelling(value: str) -> bool:
    """True when a caller STRING is exactly a non-finite float spelling.

    A message body is prose, not a number, so a string is normally accepted as
    text. The one exception is a value that IS a non-finite float spelling
    ("NaN", "-Infinity", "inf"): the template hands field values to an external
    messaging system, where later readers we do not control will parse them,
    and a stored NaN compares False against every bound it is ever checked
    against. The match is on the WHOLE stripped value, so ordinary text that
    merely contains those letters ("Nancy", "Infinity Tower") is unaffected.
    """
    return value.strip().lower().lstrip("+-") in {"nan", "inf", "infinity"}


def finite_int_in_range(value: Any, minimum: int, maximum: int) -> "int | None":
    """Parse a caller-controlled number into a bounded int, or None (refuse).

    Fail closed: bools, non-numeric types, non-numeric strings, NaN and
    +/-Infinity (both the float objects and their JSON/string spellings),
    non-integral floats, and out-of-range magnitudes are all refused. NaN is
    the sharp edge: it parses cleanly via float() and every comparison against
    it is False, so an unchecked NaN would silently disable the exact bound
    this parser exists to enforce.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError:
            return None
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except (ValueError, OverflowError):
            return None
    else:
        return None
    if not math.isfinite(number) or number != int(number):
        return None
    result = int(number)
    if result < minimum or result > maximum:
        return None
    return result


def sanitize_query(query: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Strip HTML tags (injection/markup guard) and cap length."""
    cleaned = _HTML_TAG_RE.sub("", query)
    return cleaned[:max_length]

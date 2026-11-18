"""What in a text must not leave carelessly: secrets, financial and identity numbers.

Patterns with checksums, in any language, using the standard library. Names and
places are not attempted: a detector cannot know them well enough to hide them
without damaging the text. A number with no checksum, such as a passport, only
counts beside a word naming it, and a BSN too since one nine-digit number in
eleven passes its check.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

# Hidden before a model sees them, and they keep a conversation from agents on import.
MASKED = frozenset({"api_key", "password", "card", "iban", "bsn", "cn_id", "passport"})
LABELS = {
    "api_key": "API key",
    "password": "password",
    "card": "card number",
    "iban": "IBAN",
    "bsn": "BSN",
    "cn_id": "ID number",
    "passport": "passport number",
    "cn_mobile": "mobile number",
    "email": "email address",
    "phone": "phone number",
}
_BEFORE = r"(?<![0-9A-Za-z])"
_AFTER = r"(?![0-9A-Za-z])"
_BSN_WORD = re.compile(r"(?i)bsn|burgerservice|sofi|公民服务号")
_NEAR = 40


def _luhn(text: str) -> bool:
    digits = [int(c) for c in re.sub(r"\D", "", text)][::-1]
    if digits[-1] not in (2, 3, 4, 5, 6):
        return False
    total = sum(
        d if n % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for n, d in enumerate(digits)
    )
    return total % 10 == 0


def _iban(text: str) -> bool:
    packed = text.replace(" ", "")
    moved = packed[4:] + packed[:4]
    return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1


def _bsn(text: str) -> bool:
    weights = (9, 8, 7, 6, 5, 4, 3, 2, -1)
    return sum(int(c) * w for c, w in zip(text, weights, strict=True)) % 11 == 0


def _cn_id(text: str) -> bool:
    try:
        date(int(text[6:10]), int(text[10:12]), int(text[12:14]))
    except ValueError:
        return False
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    total = sum(int(c) * w for c, w in zip(text[:17], weights, strict=True))
    return "10X98765432"[total % 11] == text[17].upper()


_Check = Callable[[str], bool] | None
# In order: what an earlier pattern claimed, a later one does not.
_PATTERNS: tuple[tuple[str, re.Pattern[str], _Check], ...] = (
    (
        "api_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,8000}?"
            r"(?:-----END [A-Z ]*PRIVATE KEY-----|$)"
            r"|\b(?:sk-[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}"
            r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})"
            r"|(?<=Bearer )[A-Za-z0-9._~+/-]{20,}=*"
        ),
        None,
    ),
    (
        "password",
        re.compile(
            r"(?i:password|passwd|pwd|密码)\s*(?:[:：=]|是|为)\s*[\"']?(?P<value>[^\s，。,;；\"']+)"
        ),
        None,
    ),
    ("iban", re.compile(_BEFORE + r"[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}" + _AFTER), _iban),
    ("cn_id", re.compile(_BEFORE + r"\d{17}[\dXx]" + _AFTER), _cn_id),
    ("card", re.compile(_BEFORE + r"\d(?:[ -]?\d){12,18}" + _AFTER), _luhn),
    (
        "passport",
        re.compile(
            r"(?i:passport|paspoort|护照)\s*(?i:号码?|number|nummer|no\.?)?\s*[:：#]?\s*"
            r"(?P<value>[A-Z]{1,2}\d{6,8}|[A-Z]{2}[A-Z0-9]{6}\d)" + _AFTER
        ),
        None,
    ),
    ("bsn", re.compile(_BEFORE + r"\d{9}" + _AFTER), _bsn),
    ("cn_mobile", re.compile(_BEFORE + r"1[3-9]\d{9}" + _AFTER), None),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*\w"), None),
    ("phone", re.compile(r"\+\d{1,3}(?:[ -]?\d){7,12}" + _AFTER), None),
)


@dataclass(frozen=True)
class Finding:
    kind: str
    start: int
    end: int


def _named_bsn(text: str, start: int, end: int) -> bool:
    return _BSN_WORD.search(text, max(0, start - _NEAR), end + _NEAR) is not None


def scan(text: str) -> list[Finding]:
    """What is in the text, in order. Holds only places, never the values."""
    found: list[Finding] = []
    for kind, pattern, check in _PATTERNS:
        for match in pattern.finditer(text):
            part = "value" if "value" in pattern.groupindex else 0
            start, end = match.span(part)
            value = text[start:end]
            if check is not None and not check(value):
                continue
            if kind == "bsn" and not _named_bsn(text, start, end):
                continue
            if not any(f.start < end and start < f.end for f in found):
                found.append(Finding(kind, start, end))
    return sorted(found, key=lambda f: f.start)


def counts(text: str) -> Counter[str]:
    return Counter(finding.kind for finding in scan(text))


def masked(text: str) -> tuple[str, Counter[str]]:
    """The text with what must not leave replaced by a label, and what was replaced."""
    done: Counter[str] = Counter()
    for finding in reversed(scan(text)):
        if finding.kind in MASKED:
            done[finding.kind] += 1
            label = f"[{LABELS[finding.kind]} hidden by the office]"
            text = text[: finding.start] + label + text[finding.end :]
    return text, done

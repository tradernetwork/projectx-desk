"""Number-grounding check for a model reply: every number in the reply is compared with the numbers in the digest text the desk produced;
the ones with no counterpart come back as `ungrounded` ("not in the data the desk has"). Soft flag, never blocks. Port of the TraderMatrix
desktop `explainGround.ts` (Apache-2.0) with futures cases. Pure, linear, no network.

Tolerance: relative 0.6% or half of the last decimal the reply shows, whichever is wider. Percent vs decimal: a reply `2.1%` matches a digest
`0.021` or `2.1`; a plain reply decimal below 1 also matches a digest percent. Sign is ignored. Thousands separators match in both texts
(`29,412.75` is 29412.75). Not graded: years, ISO dates, month-day dates, clock times, list markers, ordinals and unit-glued tokens (15m, 3rd,
Q3, IV30, 1.5R, contract codes like MESZ6), `N-day` style adjectives, plain integers up to 10, and the closing `Used:` line.
"""
import re

MAX_FLAGS = 20
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_MASKS = [re.compile(p, f) for p, f in (
    (r"\b\d{4}-\d{2}-\d{2}\b", 0),
    (r"\b\d{1,2}:\d{2}(?::\d{2})?\b", 0),
    (r"\b" + _MONTH + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?\b", re.I),
    (r"\b\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH + r"\b", re.I),
    (r"^[ \t]*Used:.*$", re.I | re.M),
    (r"^[ \t]*\d{1,3}[.)](?=[ \t])", re.M),
)]
_NUMBER = re.compile(r"(?<![A-Za-z0-9.])(\$)?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(%)?(?![A-Za-z0-9]|\.\d)")
_ADJ = re.compile(r"^-(?:day|days|period|bar|bars|week|weeks|month|months|minute|min|hour|hr|year|years)\b", re.I)
_DIGEST_PLAIN = re.compile(r"\d+(?:\.\d+)?")
_DIGEST_COMMA = re.compile(r"(?<![\d,])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?!\d)")
_EPS = 1e-9


def _digest_numbers(digest: str) -> list:
    out = []
    for m in _DIGEST_COMMA.finditer(digest):
        out.append(float(m.group(0).replace(",", "")))
    for m in _DIGEST_PLAIN.finditer(digest):
        try:
            out.append(float(m.group(0)))
        except ValueError:
            pass
    return [v for v in out if v == v and abs(v) != float("inf")]


def _matches(value: float, decimals: int, pct: bool, nums: list) -> bool:
    half = 0.5 * 10 ** (-decimals)
    for v in nums:
        cands = (v, v * 100) if pct else ((v, v / 100) if value < 1 and decimals > 0 else (v,))
        for c in cands:
            if abs(value - c) <= max(0.006 * abs(c), half) + _EPS:
                return True
    return False


def ground_reply(reply, digest) -> list:
    """Numbers in `reply` with no counterpart in `digest`, as strings (at most 20, in order)."""
    if not isinstance(reply, str) or not reply:
        return []
    digest = digest if isinstance(digest, str) else ""
    nums = _digest_numbers(digest)
    skip = [(m.start(), m.end()) for rx in _MASKS for m in rx.finditer(reply)]
    flagged = []
    for m in _NUMBER.finditer(reply):
        start, end = m.start(), m.end()
        if any(start < b and end > a for a, b in skip):
            continue
        dollar, whole, frac, pct = m.groups()
        plain_int = not dollar and not frac and not pct and "," not in whole
        if plain_int and len(whole) <= 2 and int(whole) <= 10:
            continue
        if plain_int and len(whole) == 4 and 1900 <= int(whole) <= 2100:
            continue
        if _ADJ.match(reply[end:end + 12]):
            continue
        try:
            value = float(whole.replace(",", "") + (frac or ""))
        except (ValueError, OverflowError):
            value = float("inf")
        if value == float("inf") or not _matches(value, len(frac) - 1 if frac else 0, bool(pct), nums):
            flagged.append(m.group(0))
            if len(flagged) >= MAX_FLAGS:
                break
    return flagged

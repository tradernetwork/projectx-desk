"""Soft output lint for a model REPLY: flags prescriptive wording (advice) so the chat box can show a muted notice. It never blocks or
rewrites anything. Port of the TraderMatrix desktop `explainLint.ts` (Apache-2.0) plus desk verbs. Pure; every pattern is linear (no nested
quantifiers). Descriptions of what the data shows ("price closed above the 21 EMA", "1R is 12 ticks") must pass.
"""
import re

MAX_MATCHES = 10
NOTICE = "Some wording here may read like advice. The desk agent only describes what the data shows; any decision is yours."

_I = re.IGNORECASE
_START = r"(?:^|[.!?:;]\s+|\n[ \t]*[-*•]?[ \t]*)"                      # sentence or list-item start
_MODAL = r"\b(?:should|could|must|would|let's|let’s|lets|please|I'd|I’d|you'd|you’d|you\s+can|consider|try)\s+"
_LEAD = r"(?:" + _START + r"|" + _MODAL + r")"

_PATTERNS = [re.compile(p, _I) for p in (
    r"\byou\s+(?:should|must|ought\s+to|need\s+to|might\s+want\s+to|may\s+want\s+to|had\s+better)\b",
    r"\b(?:I|we)(?:'d|’d|\s+would|\s+will)?\s+(?:recommend|suggest|advise)\b",
    r"\bmy\s+(?:advice|recommendation)\b",
    r"\b(?:consider|try|think\s+about)\s+(?:buying|selling|opening|closing|holding|hedging|adding|trimming|rolling|entering|exiting|shorting)\b",
    r"\b(?:should|would|could|must|let's|let’s|lets|please|I'd|I’d|you'd|you’d)\s+(?:buy|sell)(?![-\w])",
    r"(?:^|[.!?:;]\s+|\n[ \t]*[-*•]?[ \t]*)(?:buy|sell)(?![-\w])",
    r"\bgo\s+(?:long|short)\b",
    r"\btake\s+profits?\b",
    # sizing advice: a figure of risk or size given as an instruction, never as a description of what the data shows
    r"\brisk(?:ing)?\s+\d+(?:\.\d+)?\s*%",
    r"\b(?:risk|size)\s+(?:no\s+more\s+than|at\s+most|up\s+to|only|at\s+least)\s+\$?\d",
    r"\b(?:cut|reduce|increase|raise|scale|double|halve|trim)\s+(?:your\s+|the\s+)?(?:size|position\s+size|risk)\b",
    r"\b(?:use|take|put\s+on|go\s+with)\s+(?:a\s+|an\s+)?(?:position\s+size|size|risk)\s+of\b",
    # desk verbs: imperative or modal use only, so "the desk flattened at 14:55" and "spreads tend to widen" pass
    _LEAD + r"(?:flatten|scale\s+in|add\s+to|widen|tighten|trail)(?![-\w])",
    r"\b(?:consider|try)\s+(?:flattening|scaling\s+in|adding\s+to|widening|tightening|trailing)\b",
    r"\bmove\s+(?:your|the)\s+stop\b",
    r"\bstage\s+(?:a|the|this)\s+ticket\b",
    r"\btrail\s+(?:it|the\s+stop|your\s+stop)\b",
    r"\bcut\s+(?:the|your)\s+(?:loss(?:es)?|position)\b",
)]


def lint_reply(text) -> list:
    """Matched phrases (at most 10, in pattern order). Empty means nothing flagged."""
    if not isinstance(text, str) or not text:
        return []
    out = []
    for rx in _PATTERNS:
        for m in rx.finditer(text):
            out.append(m.group(0).strip())
            if len(out) >= MAX_MATCHES:
                return out
    return out

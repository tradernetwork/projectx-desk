"""Soft checks on a model reply from the text-only HTTP backend: leaked tool-call markup, leaked reasoning tags, a run of CJK
characters in an English desk, and a repeated paragraph (a looping model). Pure: text in, fixed words out. Imports nothing from the
desk. `check` only FLAGS (the flags ride in the AG-UI `px.reply_flags` event as `guard`); the one hard case is `only_markup`: a reply
that is nothing but tool-call markup has no answer in it. The `claude -p` desk-agent path is not guarded here."""
import re

WORDS = ("tool_markup", "reasoning_leak", "cjk", "repeat")

_TOOL_BLOCK = re.compile(
    r"<\s*(tool_call|function_call|tool_use|tool_calls|invoke|function_calls)\b[^>]*>.*?(?:<\s*/\s*\1\s*>|\Z)"
    r"|<\|?\s*(?:tool_call|tool_calls_begin|tool_call_begin)\s*\|?>.*?(?:<\|?\s*(?:tool_call_end|tool_calls_end)\s*\|?>|\Z)"
    r"|<[｜|]\s*DSML\s*[｜|][^>]*>.*?(?:<[｜|]\s*DSML\s*[｜|]\s*/[^>]*>|\Z)",
    re.I | re.S)
_TOOL_TAG = re.compile(r"<\s*/?\s*(?:tool_call|function_call|tool_use|tool_calls|invoke|function_calls|function)\b[^>]*>|<[｜|]\s*DSML\s*[｜|][^>]*>"
                       r"|<function=[^>]*>", re.I)
_THINK_OPEN_START = re.compile(r"\A\s*<\s*(think|thinking|reasoning)\s*>", re.I)
_THINK_PAIR = re.compile(r"<\s*(think|thinking|reasoning)\s*>.{40,}?<\s*/\s*\1\s*>", re.I | re.S)
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]{3,}")


def _repeat(text):
    seen = set()
    for part in re.split(r"\n", text):
        p = " ".join(part.split()).lower()
        if len(p) < 20:
            continue
        if p in seen:
            return True
        seen.add(p)
    return False


def check(text):
    """Fixed words (a subset of WORDS, in that order) for what looks wrong with `text`. Never quotes the text."""
    if not isinstance(text, str) or not text:
        return []
    out = []
    if _TOOL_TAG.search(text) or _TOOL_BLOCK.search(text):
        out.append("tool_markup")
    if _THINK_OPEN_START.search(text) or _THINK_PAIR.search(text):
        out.append("reasoning_leak")
    if _CJK.search(text):
        out.append("cjk")
    if _repeat(text):
        out.append("repeat")
    return out


def only_markup(text):
    """True when the reply is tool-call markup and nothing else (no prose left once the markup is removed)."""
    if not isinstance(text, str) or not text.strip():
        return False
    if "tool_markup" not in check(text):
        return False
    rest = _TOOL_TAG.sub(" ", _TOOL_BLOCK.sub(" ", text))
    return not re.search(r"[A-Za-z]{2,}", rest)

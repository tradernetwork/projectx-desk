"""Secret redaction for text that crosses the chat relay (typed prompt and agent reply). Pure, one pass over the characters, no regex engine,
so a hostile input cannot go quadratic. Port of the TraderMatrix desktop `redact.rs` / `redact.ts` (Apache-2.0) plus desk specifics.

Replaced with `[redacted]`: `sk-` / `sk-or-` keys (16+ key characters), `Bearer <20+ chars>`, `td_live_<8+>`, a 32+ character hex/base64-ish
run right after the word key, token or secret, a bare 64+ hex run (the desk's `openssl rand -hex 32` tokens), and the VALUE of a
`PX_*_TOKEN/KEY/SECRET=...` or `PROJECTX_API_KEY=...` line. A price, a 40 character git sha in prose and words like keyboard, tokens
or secretary are left alone. Idempotent: the marker itself matches nothing.
"""

MARKER = "[redacted]"
SK_MIN = 16
BEARER_MIN = 20
TD_MIN = 8
RUN_MIN = 32
HEX_MIN = 64
ENV_MIN = 8
RUN_PEEK = 512          # a word-trailing run is only looked at this far for a digit, so hostile input cannot make the scan quadratic
_FIRST = frozenset("sSbBtTkKPp")   # characters a secret can start with; everything else skips the checks
_HEX = frozenset("0123456789abcdefABCDEF")
_FILLER = frozenset(":=\"'`(")


def _ascii_alnum(c: str) -> bool:
    return c.isascii() and c.isalnum()


def _is_word(c: str) -> bool:
    return _ascii_alnum(c) or c == "_"


def _is_key_char(c: str) -> bool:
    return _ascii_alnum(c) or c in "_-"


def _is_token_char(c: str) -> bool:
    return _ascii_alnum(c) or c in "._~+/=-"


def _is_b64_char(c: str) -> bool:
    return _ascii_alnum(c) or c in "+/=_-"


def _is_hex(c: str) -> bool:
    return c in _HEX


def _is_value_char(c: str) -> bool:
    return not c.isspace() and c not in "\"'`,;)"


def _is_filler(c: str) -> bool:
    return c.isspace() or c in _FILLER


def _starts(s: str, i: int, lit: str, ci: bool = False) -> bool:
    seg = s[i:i + len(lit)]
    return seg.lower() == lit if ci else seg == lit


def _run_end(s: str, start: int, cap: int, f) -> int:
    """End of the run of `f` characters from `start`, looking at most `cap` characters (a run that hits the cap is at least that long)."""
    j, n = start, len(s)
    while j < n and j - start < cap and f(s[j]):
        j += 1
    return j


def _run_all(s: str, start: int, f) -> int:
    return _run_end(s, start, 1 << 62, f)


def _secret_at(s: str, i: int):
    """(start, end) of a secret beginning at i, or None."""
    n = len(s)
    boundary = i == 0 or not _is_word(s[i - 1])
    if boundary and _starts(s, i, "sk-"):
        frm = i + 3
        if _run_end(s, frm, SK_MIN, _is_key_char) - frm >= SK_MIN:
            return i, _run_all(s, frm, _is_key_char)
    if boundary and _starts(s, i, "bearer", True):
        k = i + 6
        ws = k
        while k < n and s[k] in " \t":
            k += 1
        if k > ws and _run_end(s, k, BEARER_MIN, _is_token_char) - k >= BEARER_MIN:
            return i, _run_all(s, k, _is_token_char)
    if boundary and _starts(s, i, "td_live_"):
        frm = i + 8
        if _run_end(s, frm, TD_MIN, _is_word) - frm >= TD_MIN:
            return i, _run_all(s, frm, _is_word)
    if boundary and (_starts(s, i, "PX_") or _starts(s, i, "PROJECTX_")):
        e = _run_all(s, i, _is_word)
        name = s[i:e]
        if name.endswith(("TOKEN", "KEY", "SECRET")):
            k = e
            while k < n and _is_filler(s[k]):
                k += 1
            if k > e and _run_end(s, k, ENV_MIN, _is_value_char) - k >= ENV_MIN:
                return k, _run_all(s, k, _is_value_char)
    if boundary and s[i] in _HEX:
        if _run_end(s, i, HEX_MIN, _is_hex) - i >= HEX_MIN:
            return i, _run_all(s, i, _is_hex)
    for w in ("key", "token", "secret"):
        if _starts(s, i, w, True):
            k = i + len(w)
            if k < n and _ascii_alnum(s[k]):
                continue                      # keyboard, tokens, secretary
            while k < n and _is_filler(s[k]):
                k += 1
            for lead in ("is", "was"):        # "the key is <token>"
                if _starts(s, k, lead, True) and k + len(lead) < n and (s[k + len(lead)].isspace() or s[k + len(lead)] in ":="):
                    k += len(lead)
                    while k < n and _is_filler(s[k]):
                        k += 1
                    break
            peek = _run_end(s, k, RUN_PEEK, _is_b64_char)
            if peek - k >= RUN_MIN and any(ch.isdigit() for ch in s[k:peek]):
                return k, _run_all(s, k, _is_b64_char)
    return None


def redact_secrets(text) -> str:
    s = text if isinstance(text, str) else ""
    out, i, emitted, n = [], 0, 0, len(s)
    while i < n:
        if s[i] in _FIRST or s[i] in _HEX:
            hit = _secret_at(s, i)
            if hit and hit[1] > hit[0]:
                out.append(s[emitted:hit[0]])
                out.append(MARKER)
                i = max(hit[1], i + 1)
                emitted = i
                continue
        i += 1
    out.append(s[emitted:])
    return "".join(out)


def key_like(text) -> bool:
    """True when the text IS a key rather than a question that mentions one: nothing but a secret (optionally quoted), or one bare 32+
    character token with both a digit and a letter. Such a prompt is refused outright, never relayed."""
    if not isinstance(text, str):
        return False
    t = text.strip().strip("\"'`").strip()
    if not t:
        return False
    red = redact_secrets(t).strip().strip("\"'`").strip()
    if red == MARKER:
        return True
    if red.endswith(MARKER) and len(red) <= 64 and " " not in red.replace(": ", ":").replace("= ", "="):
        return True                           # a lone NAME=value env line
    return (len(t) >= RUN_MIN and _run_all(t, 0, _is_b64_char) == len(t)
            and any(c.isdigit() for c in t) and any(c.isalpha() for c in t))

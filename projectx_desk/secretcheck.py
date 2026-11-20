"""One place that decides a secret is a placeholder or too weak to use. Pure: no I/O, imports nothing from the desk.

`weak_secret_reason(value, strict=True)` returns a reason string or None, and NEVER quotes the value (this work is streamed).
strict=True is for secrets WE generate (bearer tokens: `openssl rand -hex 32`): length, placeholder words, character variety.
strict=False is for a vendor's key, whose format we do not know: a real key may contain 'token' or 'secret' and may be short, so only
the obvious template words are refused. Callers add the variable NAME to the message themselves."""
from typing import Optional

PLACEHOLDERS = ("your_", "changeme", "change_me", "example", "placeholder", "secret", "token", "password")
TEMPLATE_WORDS = ("your_", "changeme", "change_me", "example", "placeholder")


def weak_secret_reason(value: Optional[str], strict: bool = True) -> Optional[str]:
    if not value:
        return "no token set" if strict else "no key set"
    low = value.lower()
    if not strict:
        return "key looks like a placeholder" if any(p in low for p in TEMPLATE_WORDS) else None
    if len(value) < 32:
        return "token shorter than 32 characters"
    if any(p in low for p in PLACEHOLDERS):
        return "token looks like a placeholder"
    if len(set(value)) < 12:
        return "token has too little variety (generate one with: openssl rand -hex 32)"
    return None

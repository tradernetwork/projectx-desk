"""Secret redactor: a port of the TraderMatrix redact.rs cases plus the desk's own token shapes."""
import time

from projectx_desk.redact import MARKER as R, key_like, redact_secrets

SK = "sk-proj-abcdefghijklmnop1234"
OR = "sk-or-v1-0123456789abcdef0123456789abcdef"


def test_sk_keys_and_openrouter_keys():
    assert redact_secrets(f"my key is {SK} ok") == f"my key is {R} ok"
    assert redact_secrets(OR) == R
    assert redact_secrets(f"({OR})") == f"({R})"
    assert redact_secrets(f"a {SK}\n{OR} b") == f"a {R}\n{R} b"
    assert redact_secrets("sk-0123456789abcdef") == R
    assert redact_secrets("sk-0123456789abcde") == "sk-0123456789abcde"


def test_bearer_tokens():
    assert redact_secrets("Authorization: Bearer abcdefghij0123456789xyz") == f"Authorization: {R}"
    assert redact_secrets("bearer ABCDEFGHIJ.0123456789-xyz_==") == R
    assert redact_secrets("Bearer short") == "Bearer short"
    s = "a Bearer of bad news and a very long sentence follows"
    assert redact_secrets(s) == s


def test_td_live_keys():
    assert redact_secrets("td_live_AbCd1234") == R
    assert redact_secrets("use td_live_AbCd1234efgh please") == f"use {R} please"
    assert redact_secrets("td_live_short") == "td_live_short"


def test_long_runs_after_key_token_or_secret():
    hexs = "0123456789abcdef0123456789abcdef"
    b64 = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0MTIzNDU2Nzg5"
    assert redact_secrets(f"api key: {hexs}") == f"api key: {R}"
    assert redact_secrets(f"token={b64}") == f"token={R}"
    assert redact_secrets(f'the secret is "{hexs}".') == f'the secret is "{R}".'
    assert redact_secrets(f"API_KEY={hexs}") == f"API_KEY={R}"
    assert redact_secrets(f"Secret {b64}") == f"Secret {R}"
    short = "0123456789abcdef0123456789abcde"
    assert redact_secrets(f"key {short}") == f"key {short}"


def test_ordinary_text_is_left_alone():
    for s in ["The last price is 187.32 and the high is 189.10.", "What is IV rank 72 telling me at the 95 strike?",
              "risk-reward-ratio-calculation-example-with-long-words", "ask-me-anything-about-the-chart-and-its-levels",
              "task-list-of-things-that-are-long-enough-to-matter", "sk-", "sk-abc", "the key level is 187.32", "token 12345",
              "keyboard 0123456789abcdef0123456789abcdef0123", "tokens 0123456789abcdef0123456789abcdef0123",
              "secretary 0123456789abcdef0123456789abcdef0123", "MES 7704.5 stop 7698.25 1R is 12 ticks", ""]:
        assert redact_secrets(s) == s, s
    sha = "3f2a9c1b7d4e5f60718293a4b5c6d7e8f9012345"
    assert len(sha) == 40
    prose = f"The fix landed in commit {sha} last week."
    assert redact_secrets(prose) == prose


def test_desk_tokens_and_env_lines():
    tok = "9f" * 32                                   # openssl rand -hex 32
    assert redact_secrets(f"mine is {tok} ok") == f"mine is {R} ok"
    assert redact_secrets(f"PX_MCP_TOKEN={tok}") == f"PX_MCP_TOKEN={R}"
    assert redact_secrets("PX_OPERATOR_TOKEN=hunter2hunter2") == f"PX_OPERATOR_TOKEN={R}"
    assert redact_secrets('PROJECTX_API_KEY: "abcDEF123+/xyz"') == f'PROJECTX_API_KEY: "{R}"'
    assert redact_secrets("PX_TRADING=0") == "PX_TRADING=0"
    assert redact_secrets("PX_MCP_TOKEN=short") == "PX_MCP_TOKEN=short"
    sha40 = "3f2a9c1b7d4e5f60718293a4b5c6d7e8f9012345"
    assert redact_secrets(sha40) == sha40


def test_idempotent_and_unicode_safe():
    allt = f"a {SK} b Bearer abcdefghij0123456789xyz c td_live_AbCd1234 d token={'0123456789abcdef' * 2} e PX_MCP_TOKEN={'ab' * 32}"
    once = redact_secrets(allt)
    assert "sk-" not in once and "td_live" not in once and "abcdefghij0123456789xyz" not in once and "abab" not in once
    assert redact_secrets(once) == once
    u = "café \U0001f4c8 price 187.32 中文"
    assert redact_secrets(u) == u
    assert redact_secrets(f"\U0001f4c8{SK}\U0001f4c8") == f"\U0001f4c8{R}\U0001f4c8"


def test_hostile_input_stays_linear():
    for big in ["ab12 " * 200_000, "a" * 1_000_000, "a" * 1_000_000 + "z", "key " * 250_000, "sk-" * 300_000, "a1" * 500_000]:
        t0 = time.time()
        redact_secrets(big)
        assert time.time() - t0 < 5, big[:6]


def test_non_strings_are_safe():
    assert redact_secrets(None) == "" and key_like(None) is False


def test_key_like():
    assert key_like(SK) and key_like(f'  "{OR}"  ') and key_like("Bearer abcdefghij0123456789xyz")
    assert key_like("9f" * 32) and key_like("QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0MTIzNDU2Nzg5")
    assert key_like(f"PX_MCP_TOKEN={'ab' * 32}")
    assert not key_like("what is my 1R if the stop is 12 ticks")
    assert not key_like(f"why does {SK} fail?")           # a question that mentions a key is redacted, not refused
    assert not key_like("") and not key_like("keyboard")
    assert not key_like("a" * 40)                          # no digit: a word, not a key

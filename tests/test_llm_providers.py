"""AI provider table, CLI detection and URL guard (ported from the TraderMatrix Desktop test matrix). Pure, offline."""
import ast
import os
import pathlib
import stat

import pytest

from projectx_desk import llm_providers as lp

ROOT = pathlib.Path(__file__).resolve().parent.parent


def fake_bin(d, name):
    p = d / name
    p.write_text("#!/bin/sh\necho 'tool 1.2.3'\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


# ---- registry -----------------------------------------------------------------------------------------------------------------

def test_roster_is_25_unique_and_only_claude_is_usable():
    assert len(lp.REGISTRY) == 25
    assert len({e.id for e in lp.REGISTRY}) == 25
    assert [e.id for e in lp.REGISTRY if e.support == lp.USABLE] == ["claude"]
    assert [e.id for e in lp.REGISTRY if e.support == lp.EXPERIMENTAL] == ["hermes"]
    for e in lp.REGISTRY:
        if e.support == lp.DETECT_ONLY:
            assert e.reason and not e.provider


def test_gate_allows_only_two_ids():
    g = lp.Gate()
    assert g.allows("claude_cli") and not g.allows("hermes_cli")
    assert lp.Gate(experimental=True).allows("hermes_cli")
    for pid in ("codex", "gemini", "openrouter", "", "opencode_cli"):
        assert not lp.Gate(experimental=True).allows(pid)


# ---- detection ----------------------------------------------------------------------------------------------------------------

def test_scan_finds_by_exact_name_on_absolute_entries_and_runs_nothing(tmp_path):
    marker = tmp_path / "ran"
    p = tmp_path / "claude"
    p.write_text(f"#!/bin/sh\ntouch {marker}\n")
    p.chmod(0o755)
    fake_bin(tmp_path, "codex")
    found = {f["id"]: f for f in lp.scan(str(tmp_path))}
    assert set(found) == {"claude", "codex"}
    assert found["claude"]["support"] == "usable" and found["claude"]["provider"] == "claude_cli"
    assert found["codex"]["provider"] is None
    assert "path" not in found["claude"] and str(tmp_path) not in repr(found)
    assert not marker.exists()


def test_scan_skips_relative_empty_nonexec_and_directories(tmp_path):
    (tmp_path / "claude").write_text("x")           # not executable
    (tmp_path / "codex").mkdir()                    # a directory
    assert lp.scan(str(tmp_path)) == []
    fake_bin(tmp_path, "gemini")
    assert lp.scan("relative/dir::" + "") == []
    os.chdir(tmp_path)
    assert lp.scan(".") == []


def test_find_in_path_refuses_path_tricks(tmp_path):
    fake_bin(tmp_path, "claude")
    for bad in ("../claude", "a/claude", "", ".", ".."):
        assert lp.find_in_path(str(tmp_path), bad) is None


def test_alias_binaries_are_found(tmp_path):
    fake_bin(tmp_path, "clawdbot")
    assert [f["binary"] for f in lp.scan(str(tmp_path))] == ["clawdbot"]


def test_check_version_runs_only_the_named_entry_and_cleans_output(tmp_path):
    fake_bin(tmp_path, "claude")
    assert lp.check_version("claude", str(tmp_path)) == ("tool 1.2.3", None)
    assert lp.check_version("nope", str(tmp_path)) == (None, "bad_request")
    assert lp.check_version("codex", str(tmp_path)) == (None, "not_installed")


def test_check_version_timeout_and_bad_exit(tmp_path, monkeypatch):
    p = tmp_path / "claude"
    p.write_text("#!/bin/sh\nsleep 5\n")
    p.chmod(0o755)
    monkeypatch.setattr(lp, "VERSION_TIMEOUT_S", 0.3)
    pv = f"{tmp_path}:/usr/bin:/bin"          # the child gets only PATH, and the script needs `sleep`
    assert lp.check_version("claude", pv) == (None, "timeout")
    p.write_text("#!/bin/sh\nexit 3\n")
    assert lp.check_version("claude", pv) == (None, "bad_reply")


# ---- URL guard ----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("s", ["https://openrouter.ai/api/v1", "https://example.com", "https://203.0.113.5:8443/v1",
                               "http://127.0.0.1:11434/v1", "http://localhost:1234/v1", "http://[::1]:8080/v1", "http://127.0.0.2/v1", "http://LOCALHOST:1234/v1"])
def test_base_url_accepts(s):
    assert lp.validate_base_url(s)


@pytest.mark.parametrize("s", ["http://example.com/v1", "http://192.168.1.5:11434/v1", "http://127.0.0.1.evil.com/v1", "http://localhost.evil.com/v1",
                               "http://evil.com/127.0.0.1", "http://0.0.0.0:11434/v1", "http://[::2]/v1", "http://localhost@evil.com/v1",
                               "https://user@openrouter.ai/api/v1", "https://u:pw@openrouter.ai", "https://openrouter.ai/api/v1?x=1",
                               "https://openrouter.ai/api/v1#f", "ftp://127.0.0.1/v1", "file:///etc/passwd", "javascript:alert(1)",
                               "ws://127.0.0.1/v1", "//openrouter.ai/v1", "openrouter.ai/v1", "", "   ", "https://", "https://example.com/" + "a" * 300,
                               "https://example.com:99999/v1"])
def test_base_url_refuses(s):
    assert lp.validate_base_url(s) is None


def test_trailing_slash_normalised_and_chat_url_appends_once():
    assert lp.chat_url(lp.validate_base_url("https://openrouter.ai/api/v1/")) == "https://openrouter.ai/api/v1/chat/completions"
    assert lp.chat_url(lp.validate_base_url("http://localhost:1234")) == "http://localhost:1234/chat/completions"


def test_remote_agent_url_matrix():
    ok = lambda s, lan: lp.validate_remote_agent_url(s, lan) is not None  # noqa: E731
    for s in ["https://hermes.example.com", "https://192.168.1.5", "https://8.8.8.8/v1", "https://[2001:db8::1]/v1"]:
        assert ok(s, False) and ok(s, True), s
    for s in ["http://127.0.0.1:8642", "http://localhost:8642/v1", "http://[::1]:8642"]:
        assert ok(s, False) and ok(s, True), s
    for s in ["http://192.168.1.5", "http://10.0.0.5:8642", "http://172.16.0.1", "http://172.31.255.255:8642", "http://100.64.0.1", "http://100.127.255.255"]:
        assert not ok(s, False) and ok(s, True), s
    for s in ["http://8.8.8.8", "http://172.15.255.255", "http://172.32.0.1", "http://100.63.255.255", "http://100.128.0.1", "http://169.254.1.1",
              "http://0.0.0.0:8642", "http://134744072", "http://[fd00::1]:8642", "http://[::ffff:192.168.1.5]", "http://hermes.lan:8642",
              "http://192.168.1.5.evil.com", "http://192.168.1.5.nip.io", "http://192.168.1.5@evil.com", "http://user:pw@192.168.1.5", "192.168.1.5", ""]:
        assert not ok(s, True) and not ok(s, False), s
    assert lp.validate_remote_agent_url("http://192.168.1.5:8642/v1/", True) == "http://192.168.1.5:8642/v1"


# ---- presets ------------------------------------------------------------------------------------------------------------------

def test_preset_table_shape():
    assert len(lp.PRESETS) == 10 and len({p.id for p in lp.PRESETS}) == 10
    for p in lp.PRESETS:
        if p.kind == lp.HTTP and p.base_url:
            u = lp.validate_base_url(p.base_url)
            assert u, p.id
            assert (u.startswith("http://") and lp.is_loopback_host(lp.urlsplit(u).hostname)) if p.url_editable else u.startswith("https://"), p.id
        if p.needs_key:
            assert p.kind == lp.HTTP and p.key_help_url


def test_hosted_ignores_stored_url_local_stays_on_loopback_hermes_normalised():
    assert lp.effective_base_url(lp.preset("openrouter"), "https://evil.example.com/v1") == "https://openrouter.ai/api/v1"
    assert lp.effective_base_url(lp.preset("ollama"), "http://127.0.0.1:9999/v1").endswith(":9999/v1")
    assert lp.effective_base_url(lp.preset("ollama"), "https://example.com/v1") is None
    assert lp.effective_base_url(lp.preset("ollama")).endswith(":11434/v1")
    assert lp.effective_base_url(lp.preset("custom")) is None
    assert lp.effective_base_url(lp.preset("custom"), "https://example.com/v1")
    assert lp.effective_base_url(lp.preset("custom"), "http://example.com/v1") is None
    h = lp.preset("hermes")
    assert lp.effective_base_url(h) is None and lp.effective_base_url(h, "  ", True) is None
    for typed, lan, want in [("https://hermes.example.com", False, "https://hermes.example.com/v1"), ("https://hermes.example.com/v1/", False, "https://hermes.example.com/v1"),
                             ("http://127.0.0.1:8642", False, "http://127.0.0.1:8642/v1"), ("http://192.168.1.5:8642", True, "http://192.168.1.5:8642/v1")]:
        assert lp.effective_base_url(h, typed, lan) == want
    assert lp.effective_base_url(h, "http://192.168.1.5:8642", False) is None


def test_key_shape():
    p = lp.preset
    assert "Paste" in lp.key_shape_error(p("openrouter"), " ")
    assert lp.key_shape_error(p("openrouter"), "sk-or-v1-abc") is None
    assert lp.key_shape_error(p("openrouter"), "sk-ant-abc") and lp.key_shape_error(p("openrouter"), "sk-or-")
    assert lp.key_shape_error(p("anthropic"), "sk-ant-api03-x") is None and lp.key_shape_error(p("anthropic"), "sk-or-abc")
    assert lp.key_shape_error(p("openai"), "sk-or-abc") and lp.key_shape_error(p("openai"), "sk-ant-abc")
    assert lp.key_shape_error(p("openai"), "sk-proj-abc") is None
    assert lp.key_shape_error(p("openai"), "sk a") and lp.key_shape_error(p("openai"), "k" * 257)


def test_model_error():
    assert lp.model_error(lp.preset("openai"), "") is None            # has a default
    assert lp.model_error(lp.preset("lmstudio"), "")                  # none: must be typed
    assert lp.model_error(lp.preset("openai"), "gpt 4; rm") and lp.model_error(lp.preset("openai"), "-x")
    assert lp.model_error(lp.preset("openrouter"), "deepseek/deepseek-v4-flash:free") is None


# ---- ordering and payload -----------------------------------------------------------------------------------------------------

def test_order_detected_first_and_hermes_cli_needs_found_and_switch(tmp_path):
    fake_bin(tmp_path, "claude")
    fake_bin(tmp_path, "hermes")
    fake_bin(tmp_path, "ollama")
    d = lp.detect(str(tmp_path))
    ids = [c["id"] for c in d["providers"]]
    assert ids[:2] == ["ollama", "claude_cli"] or ids[:2] == ["claude_cli", "ollama"]
    assert "hermes_cli" not in ids
    assert [n["id"] for n in d["found_cli"]] == ["hermes"]           # found but experimental and off: a note, no card
    d2 = lp.detect(str(tmp_path), lp.Gate(experimental=True))
    assert "hermes_cli" in [c["id"] for c in d2["providers"]] and d2["found_cli"] == []


def test_nothing_installed_still_lists_every_hosted_card(tmp_path):
    d = lp.detect(str(tmp_path))
    ids = [c["id"] for c in d["providers"]]
    assert ids == [p.id for p in lp.PRESETS if p.id != "hermes_cli"]
    assert not any(c["found"] for c in d["providers"]) and d["found_cli"] == []


def test_detect_only_clis_become_notes_not_cards(tmp_path):
    fake_bin(tmp_path, "codex")
    fake_bin(tmp_path, "gemini")
    d = lp.detect(str(tmp_path))
    assert {n["id"] for n in d["found_cli"]} == {"codex", "gemini"}
    assert all(n["reason"] and "path" not in n for n in d["found_cli"])
    assert "codex" not in [c["id"] for c in d["providers"]]


# ---- isolation ----------------------------------------------------------------------------------------------------------------

def test_module_reaches_no_desk_internals_and_no_bare_write():
    src = (ROOT / "projectx_desk" / "llm_providers.py").read_text()
    tree = ast.parse(src)
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.split(".")[0] in {"guard", "client", "hub", "mcp", "app", "projectx_desk"} for m in mods)
    assert not any(isinstance(n, ast.Attribute) and n.attr == "write" for n in ast.walk(tree))
    assert "—" not in src and "–" not in src

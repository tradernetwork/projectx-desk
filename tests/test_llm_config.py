import sys
import json
import os
import stat

import pytest

from projectx_desk import llm_config as lc

KEY = "sk-or-SECRETKEY1234567890"


@pytest.fixture
def path(tmp_path):
    return tmp_path / "sub" / "llm_config.json"


def raw(path):
    return json.loads(path.read_text())


def test_unconfigured_when_missing(path):
    v = lc.view(path, {})
    assert v["configured"] is False and v["key_present"] is False
    assert lc.resolve(path, {}) is None
    assert lc.route(path, {}) == "cli"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes and exec bits")
def test_save_hosted_mode_atomic_and_no_key_in_view(path):
    v = lc.save({"provider": "openrouter", "key": KEY}, path, {})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert v["key_present"] is True and v["provider"] == "openrouter" and v["model"] == "deepseek/deepseek-v4-flash"
    assert KEY not in repr(v) and KEY not in repr(lc.view(path, {}))
    assert [p.name for p in path.parent.iterdir()] == ["llm_config.json"]       # no temp files left
    r = lc.resolve(path, {})
    assert r.key == KEY and KEY not in repr(r) and KEY not in str(r)
    assert lc.route(path, {}) == "http"


def test_hosted_ignores_stored_url(path):
    lc.save({"provider": "openrouter", "key": KEY, "base_url": "https://evil.example/v1"}, path, {})
    assert raw(path)["base_url"] == ""
    d = raw(path)
    d["base_url"] = "https://evil.example/v1"
    path.write_text(json.dumps(d))
    assert lc.resolve(path, {}).base_url == "https://openrouter.ai/api/v1"


def test_key_sealed_to_url(path):
    lc.save({"provider": "custom", "base_url": "https://a.example/v1", "model": "m", "key": "abc123"}, path, {})
    assert lc.view(path, {})["key_present"] is True
    v = lc.save({"base_url": "https://b.example/v1"}, path, {})
    assert v["key_present"] is False and v["needs_key_again"] is True
    assert raw(path)["key"] == ""
    assert lc.resolve(path, {}).key == ""


def test_same_url_keeps_key_and_model_change_ok(path):
    lc.save({"provider": "openrouter", "key": KEY}, path, {})
    v = lc.save({"model": "deepseek/deepseek-v4.1-flash"}, path, {})
    assert v["key_present"] is True and v["model"] == "deepseek/deepseek-v4.1-flash"


def test_hand_edited_url_unseals_key(path):
    lc.save({"provider": "custom", "base_url": "https://a.example/v1", "model": "m", "key": "abc123"}, path, {})
    d = raw(path)
    d["base_url"] = "https://b.example/v1"
    path.write_text(json.dumps(d))
    v = lc.view(path, {})
    assert v["key_present"] is False and v["needs_key_again"] is True
    assert lc.resolve(path, {}).key == ""


def test_provider_switch_drops_key(path):
    lc.save({"provider": "openrouter", "key": KEY}, path, {})
    lc.save({"provider": "openai", "key": "sk-openai-1"}, path, {})
    lc.save({"provider": "openrouter"}, path, {})
    assert lc.view(path, {})["key_present"] is False


@pytest.mark.parametrize("junk", ["", "not json", "[]", '{"provider": 5}', '{"provider": "nope"}', '{"provider":"openai","model":3}',
                                  '{"provider":"openai","lan_http":"yes"}', "\x00\xff"])
def test_garbled_is_unconfigured(path, junk):
    path.parent.mkdir(parents=True)
    path.write_text(junk, errors="ignore")
    assert lc.view(path, {})["configured"] is False
    assert lc.resolve(path, {}) is None


def test_validation_errors_are_fixed_codes_without_key(path):
    cases = [({"provider": "nope"}, "bad_provider"),
             ({"provider": "openrouter", "key": "sk-ant-wrong"}, "bad_key"),
             ({"provider": "custom", "base_url": "http://evil.example/v1", "model": "m"}, "bad_url"),
             ({"provider": "custom", "base_url": "https://x.example/v1"}, "bad_model"),
             ({"provider": "openrouter", "model": "bad model!"}, "bad_model"),
             ({"provider": "ollama", "key": "abc"}, "no_key_needed"),
             ({"provider": "claude_cli"}, None),
             ({"provider": "hermes_cli"}, "provider_off")]
    for upd, code in cases:
        if code is None:
            assert lc.save(upd, path, {})["provider"] == "claude_cli"
            continue
        with pytest.raises(lc.ConfigError) as e:
            lc.save(upd, path, {})
        assert e.value.code == code
        assert "sk-ant-wrong" not in repr(e.value) + e.value.message


def test_local_must_stay_loopback(path):
    with pytest.raises(lc.ConfigError):
        lc.save({"provider": "ollama", "base_url": "http://192.168.1.5:11434/v1"}, path, {})
    # a local preset is not editable: stored URL is ignored, loopback preset URL used
    lc.save({"provider": "ollama"}, path, {})
    assert lc.resolve(path, {}).base_url == "http://127.0.0.1:11434/v1"


def test_cli_routes_to_relay_and_gate(path):
    lc.save({"provider": "claude_cli"}, path, {})
    assert lc.route(path, {}) == "cli" and lc.resolve(path, {}) is None
    lc.save({"provider": "hermes_cli"}, path, {"PX_LLM_EXPERIMENTAL": "1"})
    assert lc.load(path, {"PX_LLM_EXPERIMENTAL": "1"}).provider == "hermes_cli"
    assert lc.load(path, {}) is None            # gate off: unconfigured, never a guess


def test_env_key_only_for_hosted_and_never_persisted(path):
    env = {"PX_LLM_KEY": KEY}
    lc.save({"provider": "openrouter"}, path, env)
    assert raw(path)["key"] == ""
    v = lc.view(path, env)
    assert v["key_present"] is True and v["key_from_env"] is True and KEY not in repr(v)
    assert lc.resolve(path, env).key == KEY
    lc.save({"provider": "custom", "base_url": "https://x.example/v1", "model": "m"}, path, env)
    assert lc.resolve(path, env).key == ""      # never sent to an editable URL
    lc.save({"provider": "openrouter"}, path, {"PX_LLM_KEY": "wrong shape"})
    assert lc.resolve(path, {"PX_LLM_KEY": "wrong shape"}).key == ""


def test_clear(path):
    lc.save({"provider": "openrouter", "key": KEY}, path, {})
    assert lc.clear(path, {})["configured"] is False
    assert not path.exists()
    lc.clear(path, {})


def test_env_path(tmp_path):
    assert lc.config_path({"PX_LLM_CONFIG": str(tmp_path / "x.json")}) == tmp_path / "x.json"
    assert lc.config_path({}).name == "llm_config.json"

from px_env import load_env_file, parse_env_text


def test_parses_comments_quotes_export_and_inline_comments():
    t = """
# comment
PROJECTX_USERNAME=alice
export PX_ACCOUNT_ID=4242   # inline comment
PX_NO_NEW_AFTER="15:45"
PX_X='a # not a comment'
PX_URL=http://a/b#frag
bad line
1BAD=x
"""
    assert parse_env_text(t) == {"PROJECTX_USERNAME": "alice", "PX_ACCOUNT_ID": "4242", "PX_NO_NEW_AFTER": "15:45",
                                 "PX_X": "a # not a comment", "PX_URL": "http://a/b#frag"}


def test_existing_environment_wins_and_blank_values_are_not_set_and_only_names_are_returned(tmp_path):
    f = tmp_path / ".env.projectx"
    f.write_text("PX_A=from_file\nPX_B=from_file\nPX_C=\n")
    env = {"PX_A": "from_shell"}
    keys = load_env_file(f, env)
    assert keys == ["PX_B"] and env == {"PX_A": "from_shell", "PX_B": "from_file"}


def test_missing_file_is_fine_and_px_env_file_is_honoured(tmp_path, monkeypatch):
    assert load_env_file(tmp_path / "nope", {}) == []
    f = tmp_path / "custom.env"
    f.write_text("PX_Z=1\n")
    env = {"PX_ENV_FILE": str(f)}
    assert load_env_file(None, env) == ["PX_Z"] and env["PX_Z"] == "1"


def test_a_blank_value_followed_by_a_comment_is_blank_not_the_comment_text():
    t = "PX_NO_NEW_AFTER=               # HH:MM America/Chicago, at or before 17:00. Blank refuses opens.\nPX_OK=1  # trailing"
    assert parse_env_text(t) == {"PX_NO_NEW_AFTER": "", "PX_OK": "1"}


def test_no_env_file_opt_out_loads_nothing(tmp_path):
    f = tmp_path / ".env.projectx"
    f.write_text("PX_SECRET_THING=1\n")
    env = {"PX_ENV_FILE": str(f), "PX_NO_ENV_FILE": "1"}
    assert load_env_file(environ=env) == [] and "PX_SECRET_THING" not in env

"""scripts/verify_audit.py: a standalone hash-chain check of the audit files. Not part of the desk."""
import ast
import importlib.util
import json
import pathlib
import stat

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "verify_audit.py"
spec = importlib.util.spec_from_file_location("verify_audit", SCRIPT)
va = importlib.util.module_from_spec(spec)
spec.loader.exec_module(va)


def put(path, lines, tail=""):
    path.write_bytes(("".join(f"{x}\n" for x in lines) + tail).encode())


def run(tmp_path, *extra, files=("a.jsonl",)):
    argv = ["--head-file", str(tmp_path / "heads" / "h.json")]
    for f in files:
        argv += ["--file", str(tmp_path / f)]
    return va.main(argv + list(extra))


def test_no_baseline_is_exit_2_and_save_then_verify_is_ok(tmp_path, capsys):
    put(tmp_path / "a.jsonl", ["one", "two", "three"])
    assert run(tmp_path) == 2 and "no baseline" in capsys.readouterr().out
    assert run(tmp_path, "--save") == 0
    assert run(tmp_path) == 0 and "unchanged" in capsys.readouterr().out


def test_appended_lines_are_ok_and_reported_as_grew(tmp_path, capsys):
    f = tmp_path / "a.jsonl"
    put(f, ["one", "two"])
    run(tmp_path, "--save")
    put(f, ["one", "two", "three", "four"])
    capsys.readouterr()
    assert run(tmp_path) == 0 and "grew (2 line(s) appended)" in capsys.readouterr().out


def test_an_edited_earlier_line_is_exit_1_with_its_line_number(tmp_path, capsys):
    f = tmp_path / "a.jsonl"
    put(f, ["one", "two", "three"])
    run(tmp_path, "--save")
    put(f, ["one", "TWO", "three", "four"])             # an edit AND an append: the edit still wins
    capsys.readouterr()
    assert run(tmp_path) == 1 and "edited (line 2 differs" in capsys.readouterr().out


def test_removed_lines_and_a_deleted_file_are_shrunk(tmp_path, capsys):
    f = tmp_path / "a.jsonl"
    put(f, ["one", "two", "three"])
    run(tmp_path, "--save")
    put(f, ["one", "two"])
    capsys.readouterr()
    assert run(tmp_path) == 1 and "shrunk (3 lines saved, 2 now)" in capsys.readouterr().out
    f.unlink()
    assert run(tmp_path) == 1 and "shrunk" in capsys.readouterr().out


def test_a_partial_last_line_is_not_counted_and_does_not_break_the_chain(tmp_path):
    f = tmp_path / "a.jsonl"
    put(f, ["one", "two"], tail='{"half":')
    run(tmp_path, "--save")
    put(f, ["one", "two", "three"])                     # the partial line was completed as a new line
    assert run(tmp_path) == 0


def test_save_will_not_launder_an_edit_without_force(tmp_path, capsys):
    f = tmp_path / "a.jsonl"
    put(f, ["one", "two"])
    run(tmp_path, "--save")
    put(f, ["one", "evil"])
    assert run(tmp_path, "--save") == 1 and "NOT replaced" in capsys.readouterr().out
    assert run(tmp_path) == 1                           # the baseline is still the old one
    assert run(tmp_path, "--save", "--force") == 0
    assert run(tmp_path) == 0


def test_head_file_is_private_atomic_and_holds_hashes_not_content(tmp_path):
    put(tmp_path / "a.jsonl", ["SECRET-LINE-CONTENT"])
    run(tmp_path, "--save")
    h = tmp_path / "heads" / "h.json"
    assert stat.S_IMODE(h.stat().st_mode) == 0o600
    assert "SECRET-LINE-CONTENT" not in h.read_text() and not list(h.parent.glob("*.tmp"))
    d = json.loads(h.read_text())
    assert next(iter(d.values()))["lines"] == 1


def test_several_files_one_broken_is_exit_1_and_a_missing_default_file_is_skipped(tmp_path, capsys):
    put(tmp_path / "a.jsonl", ["x"])
    put(tmp_path / "b.jsonl", ["y"])
    run(tmp_path, "--save", files=("a.jsonl", "b.jsonl"))
    put(tmp_path / "b.jsonl", ["z"])
    assert run(tmp_path, files=("a.jsonl", "b.jsonl")) == 1
    out = capsys.readouterr().out
    assert "a.jsonl: unchanged" in out and "b.jsonl: edited" in out
    assert va.main(["--head-file", str(tmp_path / "none.json")]) in (0, 2)      # the real default files: absent ones are not an error


def test_the_empty_file_chain_is_stable(tmp_path):
    (tmp_path / "e.jsonl").write_bytes(b"")
    assert va.chain(tmp_path / "e.jsonl")[:2] == (0, va.hashlib.sha256(b"").hexdigest())


def test_the_script_imports_no_desk_module_and_never_uses_a_write_attribute():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add((n.module or "").split(".")[0])
        assert not (isinstance(n, ast.Attribute) and n.attr == "write"), "bare .write( access"
    assert not (mods & {"projectx_desk", "app", "guard", "client", "hub"}), mods


def test_no_desk_module_imports_or_names_the_script():
    for p in (ROOT / "projectx_desk").rglob("*.py"):
        assert "verify_audit" not in p.read_text(encoding="utf-8"), p

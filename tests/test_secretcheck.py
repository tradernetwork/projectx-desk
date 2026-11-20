import ast
import pathlib

from projectx_desk import secretcheck as sc
from projectx_desk.mcp import weak_token_reason

HEX = "0123456789abcdef" * 4


def test_hex_token_passes_and_mcp_reexport_agrees():
    assert sc.weak_secret_reason(HEX) is None and weak_token_reason(HEX) is None
    for v in (None, "", "short", "your_" + "x1y2z3" * 8, "a" * 64, "token" + HEX):
        assert weak_token_reason(v) == sc.weak_secret_reason(v)
        assert sc.weak_secret_reason(v)


def test_reason_never_quotes_the_value():
    v = "your_super_private_value_0123456789abcdef"
    assert v not in sc.weak_secret_reason(v) and v not in sc.weak_secret_reason(v, strict=False)


def test_non_strict_only_refuses_template_words():
    assert sc.weak_secret_reason("td_token_9f8e", strict=False) is None
    assert sc.weak_secret_reason("secret", strict=False) is None
    assert sc.weak_secret_reason("x", strict=False) is None
    for w in ("your_key", "changeme", "change_me", "example", "placeholder"):
        assert sc.weak_secret_reason(w, strict=False)


def test_module_is_pure():
    tree = ast.parse((pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / "secretcheck.py").read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert mods <= {"typing"}

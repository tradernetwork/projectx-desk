"""scripts/deskagent.py: screenshots the trader attaches are re-checked on the agent side and saved private, under a random name."""
import base64
import os
import pathlib
import stat
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import deskagent as d  # noqa: E402


def png(w=64, h=64):
    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"0" * 9).decode()


def test_saved_private_random_named_and_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "UPLOADS", tmp_path / "up")
    (p,) = d.save_images([{"data": png(), "name": "../../etc/passwd", "ext": "sh"}])
    assert p.parent == tmp_path / "up" and p.suffix == ".png" and len(p.stem) == 32
    assert stat.S_IMODE(p.stat().st_mode) == 0o600 and stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    os.utime(p, (1, 1))
    d.prune_uploads()
    assert not p.exists()


@pytest.mark.parametrize("bad", [
    [{"data": base64.b64encode(b"#!/bin/sh\n" + b"0" * 40).decode()}],
    [{"data": png(9000, 10)}], [{"data": png(7000, 7000)}], [{"data": png(0, 5)}],
    [{"data": png()}] * 4,
    "x", [{"nodata": 1}],
])
def test_bad_uploads_are_refused_and_nothing_is_written(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(d, "UPLOADS", tmp_path / "up")
    with pytest.raises((ValueError, KeyError, TypeError)):
        d.save_images(bad)
    assert not list((tmp_path / "up").glob("*"))


def test_the_mcp_token_file_lives_outside_the_checkout_and_the_deny_list_covers_grep():
    assert d.ROOT not in d.MCP_FILE.parents
    for rule in ("Grep(./data/*.json)", "Grep(./.env*)", "Read(./data/*.json)", "Read(./.env*)"):
        assert rule in d.DENIED

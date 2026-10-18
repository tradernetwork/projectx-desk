"""One desk per checkout: the second process-level lock on the same file refuses, and the first releases cleanly."""
import pytest

from projectx_desk import lock


def test_second_desk_is_refused_and_release_allows_a_new_one(tmp_path):
    p = tmp_path / "desk.lock"
    first = lock.acquire(p)
    with pytest.raises(SystemExit) as e:
        lock.acquire(p)
    assert "another projectx-desk" in str(e.value)
    first.close()                                   # the kernel drops the lock when the holder goes away
    lock.acquire(p).close()


def test_unopenable_lock_file_refuses_to_start(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(SystemExit):
        lock.acquire(blocker / "desk.lock")          # parent is a file: cannot prove we are alone


def test_lock_path_honours_the_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_LOCK_FILE", str(tmp_path / "x.lock"))
    assert lock.lock_path() == tmp_path / "x.lock"

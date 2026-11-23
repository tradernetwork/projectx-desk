"""Round 39: the LOSS_ROOM digest line, derived from the guard's own daily limit and room. NA when unreadable, never 0."""
import pytest

from projectx_desk import digest
from tests.test_px_digest import state


@pytest.mark.parametrize("r,want", [
    ({"daily_limit": 1000, "daily_room": 1000}, "LOSS_ROOM: BELOW_80PCT daily_used_pct=0"),
    ({"daily_limit": 1000, "daily_room": 201}, "LOSS_ROOM: BELOW_80PCT daily_used_pct=80"),      # 79.9 percent: rounds to 80, still below
    ({"daily_limit": 1000, "daily_room": 200}, "LOSS_ROOM: AT_OR_ABOVE_80PCT daily_used_pct=80"),
    ({"daily_limit": 1000, "daily_room": -50}, "LOSS_ROOM: AT_OR_ABOVE_80PCT daily_used_pct=105"),
    ({"daily_limit": 1000, "daily_room": 1500}, "LOSS_ROOM: BELOW_80PCT daily_used_pct=0"),       # a gain never reads as negative use
    ({"daily_limit": None, "daily_room": None, "daily_used_pct": 90}, "LOSS_ROOM: AT_OR_ABOVE_80PCT daily_used_pct=90"),   # stream mode
    ({"daily_limit": None, "daily_room": None, "daily_used_pct": 30}, "LOSS_ROOM: BELOW_80PCT daily_used_pct=30"),
])
def test_loss_room_line_uses_the_guards_numbers(r, want):
    assert digest.loss_room_line(r) == want


@pytest.mark.parametrize("r", [
    {}, {"daily_limit": 0, "daily_room": 0}, {"daily_limit": -5, "daily_room": 1}, {"daily_limit": 1000, "daily_room": None},
    {"daily_limit": float("nan"), "daily_room": 1}, {"daily_limit": 1000, "daily_room": float("inf")}, {"daily_limit": True, "daily_room": 1},
    {"daily_limit": "1000", "daily_room": "10"}, {"daily_used_pct": -3}, {"daily_limit": None, "daily_room": None, "daily_used_pct": None},
])
def test_loss_room_line_is_na_when_unreadable_never_zero(r):
    line = digest.loss_room_line(r)
    assert line.startswith("LOSS_ROOM: NA") and "=0" not in line


def test_the_line_is_in_the_digest_and_has_no_advice():
    s = state()
    s["risk"] = {**s["risk"], "daily_limit": 1000.0, "daily_room": 150.0}
    out = digest.format_digest(s)
    line = [x for x in out.splitlines() if x.startswith("LOSS_ROOM:")]
    assert line == ["LOSS_ROOM: AT_OR_ABOVE_80PCT daily_used_pct=85"]
    assert "$" not in line[0] and "should" not in line[0].lower()


def test_stream_mode_loss_room_goes_through_redact_stream_without_reading_unknown_as_zero_or_rounding_up():
    from projectx_desk import digest as D

    def line(room, lim=1000):
        st = D.redact_stream({"risk": {"daily_limit": lim, "daily_room": room}, "size": {}})
        return D.loss_room_line(st["risk"])
    assert "NA" in line(None) and "BELOW_80PCT" not in line(None)     # unreadable: NA, not daily_used_pct=0
    assert "AT_OR_ABOVE_80PCT" in line(0)                             # limit fully used
    assert "BELOW_80PCT" in line(250)                                 # 75 percent used must not round up to 80
    assert "AT_OR_ABOVE_80PCT" in line(200)                           # exactly 80 percent used

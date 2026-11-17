"""Number grounding: a port of explainGround.test.ts plus futures cases (commas, 7704.5, 1.5R, contract codes)."""
import time

from projectx_desk.ground import ground_reply

D = "\n".join([
    "CHART: symbol=AAPL tf=15m bars=834 last=187.32",
    "RANGE: hi=190.5 lo=180 chg=+1.23% (last 30 bars)",
    "VOL: est_buy_share=0.41 (estimate from bar closes, not tape)",
    "OVERLAYS: fib=key fvg=off ma=50,200 atr=off",
    "LEVELS: apex=1250,1255 flip=185 em_lo=180.1 em_hi=194.5 em_pct=0.021",
    "IV: rank=72",
    "STRIKE: right=put strike=95 dte=9 em_units=1.25 wall=97 earnings_inside=false verdict=exposed",
])
F = "\n".join([
    "MES MESZ6 last=7704.5 stop=7698.25 risk=1R=12 ticks",
    "ACCOUNT_ROOM: day_room=1,250.00 equity=29,412.75",
])


def g(reply, digest=D):
    return ground_reply(reply, digest)


def test_rounding_matches():
    assert g("It last traded at $187.3 today.") == []
    assert g("Last is 187.32 right now.") == []
    assert g("About $187 on the last bar.") == []


def test_missing_number_flagged():
    assert g("The price is $210 and the flip is 185.") == ["$210"]


def test_percent_vs_decimal():
    assert g("The expected move is 2.1% of spot.") == []
    assert g("That is 0.021 of spot.") == []
    assert g("Up 1.2% over the window.") == []
    assert g("IV rank is 72%.") == []
    assert g("The expected move is 9.5% of spot.") == ["9.5%"]


def test_thousands_separators():
    assert g("There is a wall near 1,250.") == []
    assert g("There is a wall near $1,250 and 1255.") == []
    assert g("A level at 1,300 shows up.") == ["1,300"]


def test_digest_lists_do_not_fuse():
    assert g("Apex at 1255 and 1250.") == []
    assert g("Apex at 2505.") == ["2505"]


def test_ranges_checked_per_number():
    assert g("Band 180-194.5 is the range.") == []
    assert g("Band 180-230 is the range.") == ["230"]
    assert g("Band 180\u2013194.5 is the range.") == []


def test_ignored_tokens():
    t = "In 2026 on 2026-12-18 or Dec 18, at 14:32, step 1. then 2) next, the 3rd item, 15m bars, Q3, IV30, a 200-day line."
    assert g(t) == []
    assert g("1. First point\n2. Second point\n10. Tenth") == []


def test_small_ints_not_claims_but_dollars_and_percent_are():
    assert g("There are 3 ideas here and 7 things.") == []
    assert g("It costs $3 per share.") == ["$3"]
    assert g("That is 4%.") == ["4%"]


def test_empty_inputs():
    assert g("It looks like a pullback to the middle of the range.") == []
    assert g("") == [] and g(None) == []
    assert g("Price is $5.", "") == ["$5"]


def test_used_line_not_graded():
    assert g("Fine.\nUsed: LEVELS, IV, 0.382") == []


def test_tolerance():
    assert g("Roughly 188.0 here.") == []
    assert g("Roughly 192 here.") == ["192"]


def test_sign_ignored():
    assert g("Down -1.2% on the window.") == []


def test_futures_cases():
    assert g("Equity is 29,412.75 and the day room 1,250.", F) == []
    assert g("Equity is 29412.75.", F) == []
    assert g("Equity is 30,500.75.", F) == ["30,500.75"]
    assert g("MES last 7704.5 with the stop at 7698.25.", F) == []
    assert g("MESZ6 is at 7704.5, 1R is 12 ticks, target 1.5R.", F) == []
    assert g("Target is 8000.5.", F) == ["8000.5"]
    assert g("A 12.5R winner on MESZ6.", F) == []        # unit glued, contract code with digit


def test_adversarial_input_finishes_fast():
    t0 = time.time()
    g("1" * 5000 + " " + "$9." * 2000)
    g("1" * 100000 + "a")
    g("1,234," * 50000)
    assert time.time() - t0 < 3

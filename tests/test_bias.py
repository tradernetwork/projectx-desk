from projectx_desk.bias import bias_view, grade_for

BEAR = {"5m": "BEAR", "15m": "BEAR", "1h": "BEAR"}
# short bias, price one tick under EMA21 (a pullback in a downtrend), calm, trending, tape agrees
GOOD = dict(last=99.5, vwap=100, ema21=99.6, atr=1.0, atr_pctile=50, adx=30, stacks=BEAR,
            flow={"w": {15: {"buy": 400, "sell": 900, "complete": True}}}, rsi=45, ct_minute=10 * 60)


def view(**kw):
    return bias_view(**{**GOOD, **kw})


def test_clean_pullback_in_trend_is_an_a():
    b = view()
    assert (b["direction"], b["grade"], b["entry"]) == ("SHORT", "A", "PULLBACK") and b["score"] == 4.5 and not b["caution"]


def test_missing_tape_cannot_raise_the_grade():
    with_flow, without = view(), view(flow=None)
    assert without["score"] == with_flow["score"] - 0.5 and "FLOW_NA" in without["flags"]


def test_chasing_an_extended_move_is_capped_at_c_even_with_everything_agreeing():
    b = view(last=95.0)            # 4.6 ATR below the 21 EMA, short
    assert b["entry"] == "EXTENDED" and b["grade"] == "C" and "EXTENDED" in b["flags"] and b["caution"]


def test_extended_plus_stretched_rsi_is_reversal_risk_and_capped_at_d():
    b = view(last=95.0, rsi=18)
    assert "REVERSAL_RISK" in b["flags"] and b["grade"] == "D"


def test_stretched_rsi_alone_caps_at_c():
    b = view(rsi=27)
    assert "STRETCHED" in b["flags"] and b["grade"] == "C"


def test_long_uses_inverted_rsi():
    b = bias_view(100.5, 100, 100.4, 1.0, 50, 30, {"5m": "BULL", "15m": "BULL", "1h": "BULL"}, None, rsi=82, ct_minute=600)
    assert b["direction"] == "LONG" and "STRETCHED" in b["flags"]


def test_price_through_the_ema_against_the_bias_is_capped():
    b = view(last=101.5)           # short, but price is 1.9 ATR above the EMA: trend broken
    assert b["entry"] == "THROUGH_EMA" and b["grade"] == "C"


def test_extreme_volatility_caps_at_b_with_no_cliff_below_it():
    assert view(atr_pctile=95)["grade"] == "B" and "VOL_EXTREME" in view(atr_pctile=95)["flags"]
    assert view(atr_pctile=90)["grade"] == "A" and "VOL_HIGH" in view(atr_pctile=90)["flags"]


def test_ranging_and_the_first_fifteen_minutes_of_the_open_cap_at_c():
    assert view(adx=12)["grade"] == "C" and "RANGING" in view(adx=12)["flags"]
    assert view(ct_minute=8 * 60 + 40)["grade"] == "C" and "OPEN_CHOP" in view(ct_minute=8 * 60 + 40)["flags"]
    assert view(ct_minute=8 * 60 + 45)["grade"] == "A"


def test_too_few_reads_is_na_never_a_guess():
    assert bias_view(100, None, None, 1, 50, 30, {"5m": "BULL", "15m": "NA", "1h": "NA"}, None)["direction"] == "NA"
    assert bias_view(None, None, None, None, None, None, None, None)["grade"] == "NA"


def test_split_reads_are_neutral_f():
    b = bias_view(100, 100, 99, 1, 50, 30, {"5m": "BULL", "15m": "BEAR", "1h": "MIXED"}, None)
    assert (b["direction"], b["grade"]) == ("NEUTRAL", "F")


def test_incomplete_flow_window_is_ignored():
    inc = {"w": {15: {"buy": 900, "sell": 100, "complete": False}}}
    assert "flow" not in view(flow=inc)["votes"]


def test_nonfinite_inputs_do_not_vote_or_crash():
    b = view(last=float("nan"), vwap=float("inf"), rsi=float("nan"))
    assert "vwap" not in b["votes"] and b["entry"] is None and "ENTRY_NA" in b["flags"]


def test_bands():
    assert [grade_for(x) for x in (3.5, 2.5, 1.5, 0.0)] == ["A", "B", "C", "D"]


def test_bias_snapshot_is_numbers_and_enums_only():
    import asyncio, types
    from projectx_desk import app as appmod
    from projectx_desk import digest

    async def fake_ind(app, cid):
        return {"atr": 5.0, "adx": 30.0, "atr_pctile": 50, "vwap": 100.0, "ema": {"state": "BEAR", "21": 99.0}}

    live = types.SimpleNamespace(quotes={"CON.F.US.MES.Z26": {"lastPrice": 95.0}}, flow=None)
    app = types.SimpleNamespace(state=types.SimpleNamespace(live=live, respect_bars={}))
    old_i, old_s = digest.indicators, appmod.px_flow.summary
    digest.indicators = fake_ind
    appmod.px_flow.summary = lambda *a, **k: None
    try:
        e = asyncio.run(appmod._bias_snapshot(app, "CON.F.US.MES.Z26"))
    finally:
        digest.indicators, appmod.px_flow.summary = old_i, old_s
    assert e["tf"] == "bias" and e["contract"] == "CON.F.US.MES.Z26" and e["bar_ts"] % 300000 == 0
    assert e["direction"] in ("LONG", "SHORT", "NEUTRAL", "NA") and set(e) >= {"price", "atr", "grade", "votes", "flags"}

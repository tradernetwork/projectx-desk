from projectx_desk.bias import MAX_SCORE, bias_view, grade_for

BEAR = {"5m": "BEAR", "15m": "BEAR", "1h": "BEAR"}
# short bias, price just under the 21 EMA (a pullback in a downtrend), calm, trending, tape agrees
GOOD = dict(last=99.5, vwap=100, ema21=99.6, atr=1.0, atr_pctile=50, adx=30, stacks=BEAR,
            flow={"w": {15: {"buy": 400, "sell": 900, "complete": True}}}, rsi=45, ct_minute=10 * 60)


def view(**kw):
    return bias_view(**{**GOOD, **kw})


def test_clean_pullback_in_trend_is_an_a_and_the_max_is_printed():
    b = view()
    assert (b["direction"], b["grade"], b["entry"]) == ("SHORT", "A", "PULLBACK")
    assert b["score"] == MAX_SCORE == b["max"] == 3.5 and not b["caution"] and b["rule"] == "v2.2"


def test_missing_tape_cannot_raise_the_grade():
    with_flow, without = view(), view(flow=None)
    assert without["score"] == with_flow["score"] - 0.5 and "FLOW_NA" in without["flags"]


def test_entry_location_cannot_buy_an_a_when_a_higher_timeframe_dissents():
    b = view(stacks={"5m": "BEAR", "15m": "BEAR", "1h": "BULL"})      # 5m, 15m, VWAP, tape agree, price at the 21 EMA
    assert b["score"] >= 3.0 and "DISSENT" in b["flags"] and b["grade"] == "B"


def test_chasing_an_extended_move_is_capped_at_c_even_with_everything_agreeing():
    b = view(last=95.0)            # 4.6 ATR below the 21 EMA, short
    assert b["entry"] == "EXTENDED" and b["grade"] == "C" and "EXTENDED" in b["flags"] and b["caution"]


def test_extended_with_a_stretched_rsi_is_flagged_and_costs_points_but_has_no_hard_d():
    plain, hot = view(last=95.0), view(last=95.0, rsi=18)
    assert "EXTENDED_RSI" in hot["flags"] and hot["score"] < plain["score"] and hot["grade"] in ("C", "D")


def test_stretched_rsi_alone_is_graded_not_capped():
    mild, hard = view(rsi=28), view(rsi=21)
    assert "STRETCHED" in mild["flags"] and hard["score"] < mild["score"] < view()["score"]


def test_long_uses_inverted_rsi():
    b = bias_view(100.5, 100, 100.4, 1.0, 50, 30, {"5m": "BULL", "15m": "BULL", "1h": "BULL"}, None, rsi=82, ct_minute=600)
    assert b["direction"] == "LONG" and "STRETCHED" in b["flags"]


def test_price_through_the_ema_against_the_bias_is_capped_at_c():
    b = view(last=101.5)           # short, but price is 1.9 ATR above the EMA: trend broken
    assert b["entry"] == "THROUGH_EMA" and b["grade"] == "C"


def test_numeric_inputs_never_flip_a_letter_on_a_wobble():
    for key, lo, hi, step in (("atr_pctile", 60, 100, 0.1), ("adx", 8, 30, 0.1), ("rsi", 15, 45, 0.1)):
        prev = None
        x = lo
        while x <= hi:
            sc = view(**{key: x})["score"]
            if prev is not None:
                assert abs(sc - prev) <= 0.1, (key, x, sc, prev)
            prev, x = sc, round(x + step, 4)


def test_volatility_flags_are_informational_and_the_penalty_is_graded():
    assert "VOL_EXTREME" in view(atr_pctile=95)["flags"] and "VOL_HIGH" in view(atr_pctile=85)["flags"]
    assert view(atr_pctile=95)["score"] == view(atr_pctile=100)["score"] == round(view()["score"] - 0.75, 2)


def test_ranging_costs_points_and_the_first_fifteen_minutes_of_the_open_cap_at_c():
    r = view(adx=12)
    assert "RANGING" in r["flags"] and r["score"] < view()["score"]
    assert view(ct_minute=8 * 60 + 40)["grade"] == "C" and "OPEN_CHOP" in view(ct_minute=8 * 60 + 40)["flags"]
    assert view(ct_minute=8 * 60 + 45)["grade"] == "A"


def test_too_few_reads_is_na_never_a_guess():
    assert bias_view(100, None, None, 1, 50, 30, {"5m": "BULL", "15m": "NA", "1h": "NA"}, None)["direction"] == "NA"
    assert bias_view(None, None, None, None, None, None, None, None)["grade"] == "NA"


def test_split_reads_are_neutral_with_no_letter():
    b = bias_view(100, 100, 99, 1, 50, 30, {"5m": "BULL", "15m": "BEAR", "1h": "MIXED"}, None)
    assert (b["direction"], b["grade"]) == ("NEUTRAL", "-")


def test_incomplete_flow_window_is_ignored():
    inc = {"w": {15: {"buy": 900, "sell": 100, "complete": False}}}
    assert "flow" not in view(flow=inc)["votes"]


def test_nonfinite_inputs_do_not_vote_or_crash():
    b = view(last=float("nan"), vwap=float("inf"), rsi=float("nan"))
    assert "vwap" not in b["votes"] and b["entry"] is None and "ENTRY_NA" in b["flags"]


def test_bands():
    assert [grade_for(x) for x in (3.0, 2.25, 1.5, 0.0)] == ["A", "B", "C", "D"]


def test_bias_snapshot_is_numbers_and_enums_only():
    import asyncio, time, types
    from projectx_desk import app as appmod
    from projectx_desk import digest

    closed = int(time.time() // 300) * 300_000 - 300_000          # the bar that just closed (the loop logs it seconds later)

    async def fake_ind(app, cid):
        return {"atr": 5.0, "adx": 30.0, "atr_pctile": 50, "vwap": 100.0, "rsi": 45.0, "ema": {"state": "BEAR", "21": 99.0},
                "closed_ts": closed, "closed_close": 95.0}

    live = types.SimpleNamespace(quotes={"CON.F.US.MES.Z26": {"lastPrice": 95.0}}, flow=None)
    app = types.SimpleNamespace(state=types.SimpleNamespace(live=live, respect_bars={}))
    old_i, old_s = digest.indicators, appmod.px_flow.summary
    digest.indicators = fake_ind
    appmod.px_flow.summary = lambda *a, **k: None
    try:
        e = asyncio.run(appmod._bias_snapshot(app, "CON.F.US.MES.Z26"))
    finally:
        digest.indicators, appmod.px_flow.summary = old_i, old_s
    assert e["tf"] == "bias" and e["contract"] == "CON.F.US.MES.Z26" and e["bar_ts"] == closed and e["price"] == 95.0
    assert e["rule"] == "v2.2" and e["rsi"] == 45.0 and "entry" in e
    assert e["direction"] in ("LONG", "SHORT", "NEUTRAL", "NA") and set(e) >= {"price", "atr", "grade", "votes", "flags"}

    async def stale_ind(app, cid):                                 # the Friday bar on a Sunday: nothing is logged under it
        return {**(await fake_ind(app, cid)), "closed_ts": closed - 176_700_000}
    digest.indicators, appmod.px_flow.summary = stale_ind, lambda *a, **k: None
    try:
        assert asyncio.run(appmod._bias_snapshot(app, "CON.F.US.MES.Z26")) is None
    finally:
        digest.indicators, appmod.px_flow.summary = old_i, old_s


def test_bias_log_loop_writes_one_line_per_tracked_contract_per_closed_bar(tmp_path, monkeypatch):
    import asyncio, json, types
    from projectx_desk import app as appmod
    f = tmp_path / "bias.jsonl"
    monkeypatch.setenv("PX_BIAS_LOG_FILE", str(f))
    entry = {"ts": 1, "contract": "CON.F.US.MES.Z26", "tf": "bias", "bar_ts": 1792768200000, "price": 95.0, "direction": "SHORT", "grade": "B"}

    async def snap(app, cid):
        return dict(entry, contract=cid)
    monkeypatch.setattr(appmod, "_bias_snapshot", snap)
    monkeypatch.setattr(appmod.px_digest, "invalidate_indicators", lambda *a, **k: None)
    calls = []

    async def fake_sleep(s):
        calls.append(s)
        if len(calls) >= 3:                       # boundary wait, MES gap, MNQ gap: stop before a second cycle
            raise asyncio.CancelledError
    monkeypatch.setattr(appmod.asyncio, "sleep", fake_sleep)
    app = types.SimpleNamespace(state=types.SimpleNamespace(tracked={"MES": "CON.F.US.MES.Z26", "MNQ": "CON.F.US.MNQ.Z26", "MCL": None}))
    try:
        asyncio.run(appmod._bias_log_loop(app))
    except asyncio.CancelledError:
        pass
    lines = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()]
    assert [x["contract"] for x in lines] == ["CON.F.US.MES.Z26", "CON.F.US.MNQ.Z26"]


def test_ema21_turning_against_the_lean_caps_a_clean_a_at_b_and_a_matching_slope_does_not():
    assert view()["grade"] == "A"
    turned = view(slope21="UP")                       # short lean, the 21 EMA curling up
    assert "EMA_TURN" in turned["flags"] and turned["grade"] == "B" and turned["caution"]
    assert view(slope21="DOWN")["grade"] == "A" and "EMA_TURN" not in view(slope21="DOWN")["flags"]
    assert view(slope21=None)["grade"] == "A" and view(slope21="FLAT")["grade"] == "A"       # unreadable or flat: no claim
    long_ = view(stacks={"5m": "BULL", "15m": "BULL", "1h": "BULL"}, last=100.5, ema21=100.4, flow={"w": {15: {"buy": 900, "sell": 400, "complete": True}}})
    assert long_["direction"] == "LONG" and "EMA_TURN" in view(**{"stacks": {"5m": "BULL", "15m": "BULL", "1h": "BULL"}, "last": 100.5, "ema21": 100.4,
                                                                  "flow": {"w": {15: {"buy": 900, "sell": 400, "complete": True}}}, "slope21": "DOWN"})["flags"]

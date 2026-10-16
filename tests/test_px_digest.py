from projectx_desk.digest import DISCLAIMER, LLMS_TXT, format_digest, redact_stream


def state(**over):
    s = {"v": 1, "asof": "10:00:00CT",
         "mode": {"trading": False, "mcp_place": False, "halt": False, "account_kind": "SIM", "bracket_mode": "POSITION"},
         "feed": {"market": "UP", "user": "UP", "market_age_s": 0.4, "user_age_s": 1.0, "seeded": True},
         "contract": {"root": "MES", "tick": 0.25, "tick_value": 1.25, "active": True},
         "quote": {"last": 5832.25, "bid": 5832.0, "ask": 5832.25, "age_s": 0.4},
         "session": {"vwap": 5829.5, "hi": 5841.0, "lo": 5822.5, "volume": 12000},
         "ind": {"atr": 4.0, "adx": 18.2, "tf": "5m"},
         "size": {"status": "OK", "side": "long", "entry": 5832.25, "stop": 5826.25, "stop_ticks": 24, "stop_points": 6.0,
                  "budget_usd": 50.0, "risk_pct": 0.03, "stop_atr_k": 1.5, "contracts": 1, "risk_usd": 30.0, "rpc": 30.0,
                  "pct_budget": 60.0, "binding": "risk", "refused": False, "reason": None, "r1": 30.0, "r2": 60.0, "r3": 90.0, "adx": 18.2},
         "ticket": None,
         "risk": {"daily_limit": 1000.0, "daily_room": 1000.0, "dd_buffer": 2000.0, "remaining": 1000.0, "binding": "DAILY",
                  "orders_today": 0, "orders_max": 10, "open_contracts": 0, "max_contracts": 3, "pnl_day": 0.0},
         "gex": {"status": "UNAVAILABLE"}, "positions": [], "orders": [], "fills_today": 0, "disclaimer": DISCLAIMER}
    s.update(over)
    return s


def test_digest_is_fixed_key_terse_and_reads_aloud():
    t = format_digest(state())
    lines = t.splitlines()
    assert lines[0] == "PXDESK v1 asof=10:00:00CT"
    for key in ("MODE:", "FEED:", "CONTRACT:", "QUOTE:", "VWAP:", "SESSION:", "IND:", "SIZE_INPUT:", "SIZE_IDEA:",
                "SIZE_RESULT:", "SIZE_R:", "TICKET: NONE", "RISK_DAILY:", "RISK_DD:", "RISK_OPEN:", "GEX:", "POS: NONE",
                "ORD: NONE", "FILLS_TODAY:", "ACTIONS:", "RULE:", "NOTE:"):
        assert any(l.startswith(key) for l in lines), key
    assert "VWAP: 5829.5 vs_last=+2.75pt side=ABOVE" in t
    assert "SIZE_RESULT: contracts=1 risk_usd=30 pct_budget=60 binds=RISK" in t
    assert "trading=OFF" in t and "engine=READY" in t


def test_refused_sizing_is_stated_plainly_never_rounded_up():
    s = state()
    s["size"] = {**s["size"], "contracts": 0, "refused": True, "reason": "GRANULARITY", "risk_usd": 0.0}
    t = format_digest(s)
    assert "contracts=0 REFUSED reason=GRANULARITY" in t


def test_unknowns_print_na_not_blank_or_zero():
    s = state(quote={"last": None, "bid": None, "ask": None, "age_s": None}, session={"vwap": None, "hi": None, "lo": None, "volume": None},
              ind={"atr": None, "adx": None, "tf": None}, size={"status": "NA"})
    s["risk"] = {**s["risk"], "pnl_day": None, "daily_room": None, "remaining": None, "binding": None}
    t = format_digest(s)
    assert "VWAP: NA" in t and "SIZE: NA" in t and "last=NA" in t and "room=NA" in t


def test_halt_blocks_staging_and_ticket_shows_human_next_step():
    s = state()
    s["mode"] = {**s["mode"], "halt": True}
    assert "engine=HALTED" in format_digest(s) and "stage=BLOCKED" in format_digest(s)
    s2 = state(ticket={"id8": "ab12cd34", "ttl_s": 90, "readback": {"size": 1, "stop_plan": "SEPARATE_AFTER_FILL"},
                       "checks_failed": []})
    t = format_digest(s2)
    assert "TICKET: id=ab12cd34 ttl_s=90" in t and "place=NEEDS_HUMAN" in t


def test_stream_mode_drops_dollars_and_absolute_pnl():
    r = redact_stream(state(ticket={"id8": "ab12cd34", "ttl_s": 9, "readback": {"size": 1, "risk_usd": 30.0, "stop_price": 5826.25},
                                    "checks_failed": []}))
    t = format_digest(r)
    assert "STREAM=ON" in t
    for gone in ("pnl_day", "budget_usd", "risk_usd", "limit=1000", "room=1000", "buffer=2000", "1R=30"):
        assert gone not in t, gone
    assert "stop_price=5826.25" in t        # prices stay: Codex needs them to read the ticket back
    assert "budget=HIDDEN" in t


def test_llms_txt_is_unambiguous_about_roles():
    for needle in ("NEVER place", "The operator places", "rounded DOWN", "GRANULARITY", "ignore", "NA = unknown"):
        assert needle in LLMS_TXT


def test_kpi_and_trade_lines_render_with_small_sample_flag():
    s = state(kpi={"n": 12, "wins": 9, "losses": 3, "win_rate": 75.0, "expectancy": -3.64, "profit_factor": 0.4,
                   "breakeven_win_rate": 88.9, "payoff": 0.14, "gross_win": 39.3, "gross_loss": 97.6, "avg_win": 4.4, "avg_loss": 32.5, "net": -58.2},
              trade={"state": "PLACED", "acked": True, "side": "LONG", "size": 1, "filled": 1, "entry_ref": 5001.75,
                     "stop_price": 4996.75, "stop_mode": "SEPARATE_AFTER_FILL", "stop_state": "ACKED", "risk_usd": 25.0, "id8": "ab12cd34"})
    t = format_digest(s)
    assert "KPI: n=12 payoff=0.14 win_rate=75% breakeven_win_rate=88.9% profit_factor=0.4 expectancy=-3.64 SMALL_SAMPLE" in t
    assert "TRADE: state=PLACED acked=YES side=LONG size=1 filled=1 entry_ref=5001.75 stop=4996.75 stop_state=ACKED" in t
    r = format_digest(redact_stream(s))
    assert "expectancy" not in r and "win_rate=75%" in r


def test_kpi_na_and_zero_are_distinct():
    assert "KPI: NA" in format_digest(state(kpi={"n": None}))
    assert "KPI: n=0" in format_digest(state(kpi={"n": 0}))
    assert "TRADE: NONE" in format_digest(state(trade=None))


def test_level_distances_use_the_live_quote_not_the_last_bar_close():
    from projectx_desk.digest import _levels_vs_live
    lv = [{"name": "EMA21", "price": 100.0, "dist_pt": -3.0, "pos": "BELOW"}]
    out = _levels_vs_live(lv, {"lastPrice": 112.5})
    assert out[0]["dist_pt"] == 12.5 and out[0]["pos"] == "ABOVE" and lv[0]["dist_pt"] == -3.0   # input untouched
    assert _levels_vs_live(lv, None) == lv and _levels_vs_live(lv, {"lastPrice": None}) == lv

"""Trend overlay wiring. The detection is tested in test_trend.py (node); the drawing is checked by eye on the mock desk."""
import pathlib
import re

STATIC = pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / "static"
JS = (STATIC / "desk.js").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def test_generated_overlays_are_never_saved_as_the_traders_own_drawings():
    groups = re.search(r"const NONUSER = \[([^\]]*)\]", JS).group(1)
    for g in ("desk", "react", "respect", "autolvl", "orb", "tmap", "trend"):      # tmap and trend would otherwise reappear as ghost drawings on reload
        assert f"'{g}'" in groups, g


def test_trend_is_loaded_toggled_and_driven_from_the_poll_loop():
    assert '<script src="/static/trend.js"></script>' in HTML.split("</head>")[0] and 'id="tr-toggle"' in HTML
    body = JS.split("function syncDesk(){")[1].split("\n")[0]
    assert "try{drawTrend(false)}catch(e){err(e)}" in body                       # try/catch, not typeof: the consts it reads are defined further down
    block = JS.split("Automatic trendlines and channels")[1].split("function syncDesk")[0]
    assert "PXTrend.fit(data)" in block and "groupId: 'trend'" in block
    assert "method: 'POST'" not in block and "/api/" not in block                  # display only: no network, no order path


def test_every_drawn_line_says_how_many_touches_it_has():
    block = JS.split("Automatic trendlines and channels")[1].split("function syncDesk")[0]
    assert block.count("touches") >= 4

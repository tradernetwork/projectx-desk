"""The tab text carries the last price and root; alert flashers fall back to it and are never overwritten mid-flash."""
import pathlib
import re

JS = (pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / "static" / "desk.js").read_text()


def test_price_updates_the_tab_title_without_clobbering_an_alert_label():
    assert "titleBase = e.textContent + ' ' + curRoot(); setTitle();" in JS
    guard = re.search(r"function setTitle\(\)\{\s*if\(!/\^\[\\u25B2\\u25BC\\u25B6\]/\.test\(document\.title\)\) document\.title = titleBase;", JS)
    assert guard, "setTitle must skip while a flashing label (starts with an arrow) is showing"


def test_flashers_restore_the_price_title_not_a_hardcoded_name():
    assert "'PXDESK'" in JS.split("let titleBase = ")[1].split(";")[0]          # only the initial value
    assert JS.count("document.title = 'PXDESK'") == 0
    assert JS.count("document.title = titleBase") >= 5


def test_1m_trendlines_are_off_by_default_and_migrated_once():
    assert "LS.get('px.trendTfs', ['5m', '15m'])" in JS
    assert "LS.get('px.trendTfs.v2', false) || t !== '1m'" in JS and "LS.set('px.trendTfs.v2', true)" in JS
    html = (pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / "static" / "index.html").read_text()
    assert 'data-tf="1m" aria-pressed="false"' in html and 'data-tf="5m" aria-pressed="true"' in html


def test_every_walkthrough_target_exists_in_the_page():
    static = pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / "static"
    html, tour = (static / "index.html").read_text(), (static / "tour.js").read_text()
    for sel in re.findall(r"\['#([a-z0-9-]+)'", tour):
        assert f'id="{sel}"' in html, sel
    assert "'#tmap'" in tour and "'#tr-tfs'" in tour and "'#chat-fab'" in tour

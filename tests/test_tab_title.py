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

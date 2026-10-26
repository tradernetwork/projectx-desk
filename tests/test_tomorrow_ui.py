"""Tomorrow's Map UI wiring. The drawing itself is checked by eye on the mock desk; these pin the parts that must stay hooked up."""
import pathlib
import re

STATIC = pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / "static"
JS = (STATIC / "desk.js").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def test_panel_and_toggle_exist_and_the_poll_loop_drives_the_map():
    assert 'id="tmap"' in HTML and 'id="tm-body"' in HTML and 'id="tm-toggle"' in HTML
    assert "function syncDesk(){if(typeof tmapSync === 'function') tmapSync();" in JS         # every AI-state poll syncs the map
    assert "'/api/tomorrow?contract=' + encodeURIComponent(cid)" in JS


def test_the_map_is_display_only_no_post_and_no_order_path():
    block = JS.split("Tomorrow's map: the roads out of price")[1].split("function syncDesk")[0]
    assert "method: 'POST'" not in block and "/api/place" not in block and "/api/preview" not in block and "csrf" not in block.lower()


def test_offscreen_waypoints_are_pinned_to_the_edge_not_lost_and_passed_ones_are_marked():
    block = JS.split("Tomorrow's map: the roads out of price")[1].split("function syncDesk")[0]
    assert "clampY" in block and "offSide" in block and "(through)" in block
    assert re.search(r"road\('UP'\)\.waypoints.*road\('DOWN'\)\.waypoints", block)


def test_every_road_is_drawn_from_server_text_never_rebuilt_in_the_browser():
    block = JS.split("function renderTmap()")[1].split("async function tmapFetch")[0]
    assert "rd.cond" in block and "rd.rule" in block                                           # the sentences are the server's, printed as given
    assert "innerHTML" not in block                                                           # text nodes only

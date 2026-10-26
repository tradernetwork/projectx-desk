"""Tomorrow's Map UI wiring. The drawing itself is checked by eye on the mock desk; these pin the parts that must stay hooked up."""
import pathlib
import re

STATIC = pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / "static"
JS = (STATIC / "desk.js").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def test_panel_and_toggle_exist_and_the_poll_loop_drives_the_map():
    assert 'id="tmap"' in HTML and 'id="tm-body"' in HTML and 'id="tm-toggle"' in HTML
    assert "if(typeof tmapSync === 'function') tmapSync();" in JS.split("function syncDesk(){")[1].split("}")[0]         # every AI-state poll syncs the map
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


def test_the_right_now_box_says_watch_and_never_tells_you_to_trade():
    watch = JS.split("const TM_WATCH = {")[1].split("};")[0].lower()
    for advice in ("go long", "go short", "you should", "enter ", "buy it", "sell it", "take the trade", "place "):
        assert advice not in watch, advice
    assert "RIGHT NOW" in JS and "something to WATCH, not a signal" in JS and "Nothing is being tested right now: just watch." in JS
    assert "const nowEl = $('tm-now')" in JS                      # the box follows price on every poll without rebuilding the panel


def test_gex_wall_labels_use_words_for_size_and_say_what_the_wall_does():
    block = JS.split("function deskLines()")[1].split("const cl = CALL.live")[0]
    assert "'BIG'" in block and "'MED'" in block and "'SMALL'" in block and "speeds moves" in block and "stalls moves" in block
    assert "Math.round(lv.rel * 100)" not in block                # the bare percentage nobody could read

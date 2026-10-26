"""Layout mode (Auto / Landscape / Portrait) is pure front end, so pin the pieces that must stay wired together."""
import pathlib
import re

STATIC = pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "desk.css").read_text(encoding="utf-8")
JS = (STATIC / "layout.js").read_text(encoding="utf-8")


def test_layout_script_loads_in_head_before_first_paint_and_control_is_in_the_strip():
    head = HTML.split("</head>")[0]
    assert 'src="/static/layout.js"' in head
    strip = HTML.split('<footer class="strip"')[1]
    assert 'id="layout-seg"' in strip and all(f'data-v="{m}"' in strip for m in ("auto", "landscape", "portrait"))


def test_layout_js_resolves_auto_from_orientation_and_survives_blocked_storage():
    assert "(orientation: portrait)" in JS and "'auto'" in JS and "'landscape'" in JS and "'portrait'" in JS
    assert JS.count("try{") >= 2          # localStorage read and write are both guarded (private windows throw)


def test_portrait_grid_gives_the_footer_its_own_row_and_the_chart_the_full_width():
    block = CSS.split("layout mode (static/layout.js")[1]
    assert re.search(r'\[data-layout="portrait"\] \.chartwrap\{grid-column:1/-1', block)
    assert re.search(r'\[data-layout="portrait"\] \.strip\{grid-row:5\}', block)   # without it the strip overlaps the bottom band
    assert "@media (min-width:1100px)" in block                                       # below 1100px both modes use the stacked layout

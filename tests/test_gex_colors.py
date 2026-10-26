"""GEX lines are side-coded like the ember walls (they were flat purple for days and nobody noticed). Pin the pieces that carry it."""
import pathlib
import re

STATIC = pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / "static"
JS = (STATIC / "desk.js").read_text(encoding="utf-8")
CSS = (STATIC / "desk.css").read_text(encoding="utf-8")


def test_walls_are_coloured_by_side_and_pin_and_flip_stay_purple():
    block = JS.split("function deskLines()")[1].split("const cl = CALL.live")[0]
    assert "E.hot" in block and "E.sup" in block and "lv.price > lastP" in block
    assert re.search(r"GEX_COL = \{flip: '#b45bff', pin: '#a63cff'\}", JS)      # only the reference levels are purple
    assert "wall_above: '#" not in JS and "wall_below: '#" not in JS


def test_size_drives_weight_and_a_shaded_band_sits_under_the_lines():
    assert "rel >= 0.66 ? 4 : rel >= 0.33 ? 3 : 2" in JS
    assert "function gexBands()" in JS and "zones = [...gexBands(), ...tradeZones()]" in JS      # bands first: under the lines


def test_legend_swatch_shows_pin_wall_above_wall_below():
    assert re.search(r"\.lg\.gex::before\{[^}]*#a63cff[^}]*#ff8000[^}]*#00e070", CSS)

"""Prescriptive-language lint: both polarities, a port of explainLint.test.ts plus the desk verbs."""
import time

from projectx_desk.lint import MAX_MATCHES, NOTICE, lint_reply


def test_flags_prescriptive_wording():
    for t in ["You should buy the dip.", "I recommend selling the put.", "I suggest you hold.", "Consider buying calls here.",
              "You might want to sell now.", "Buy at 187.", "Let's sell into strength.", "You need to close it.", "Go long above the flip.",
              "Take profits here.", "Risk 2% on this.", "Cut your size.", "Use a position size of 3."]:
        assert lint_reply(t), t


def test_flags_desk_verbs():
    for t in ["You should flatten now.", "Flatten before the close.", "Scale in on the pullback.", "Let's add to the winner.",
              "Move your stop to breakeven.", "Widen the stop.", "Tighten it up.", "Stage a ticket at 7704.5.", "Trail it from here.",
              "Cut the loss now.", "Cut the position.", "Consider tightening the stop.", "You could trail the stop."]:
        assert lint_reply(t), t


def test_descriptions_pass():
    for t in ["The delta is 0.30.", "Sell-side analysts cover the name.", "Buy-side flow is estimated at 41%.", "Buyers and sellers are balanced.",
              "Est buy share is 0.41.", "The put was sold short by someone, which a short put describes.", "IV rank is 72, which is high.",
              "Price closed above the 21 EMA.", "1R is 12 ticks.", "The stop is at 7698.25, 6.25 points from entry.",
              "The desk flattened at 14:55 CT.", "Spreads tend to widen after the print.", "The trail stop is working at 7701.",
              "Position is flat, no working orders.", "The 5m EMA21 slope is UP and VWAP is below price.",
              "Last 30 bars range 7690 to 7712; the gap to the stop is 1.5 ATR.", "The ticket expired after 300 seconds."]:
        assert lint_reply(t) == [], t


def test_matches_returned_and_capped():
    assert lint_reply("You should buy it.")
    assert lint_reply("clean text") == []
    assert lint_reply("") == [] and lint_reply(None) == []
    assert len(lint_reply("You should go. " * 50)) == MAX_MATCHES


def test_notice_is_plain():
    assert len(NOTICE) > 20 and "\u2014" not in NOTICE and "\u2013" not in NOTICE


def test_adversarial_input_finishes_fast():
    t0 = time.time()
    lint_reply("you " * 5000 + "should " + "I " * 3000)
    lint_reply("\n " * 100000 + "x" * 100000)
    assert time.time() - t0 < 2

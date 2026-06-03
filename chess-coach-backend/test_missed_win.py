"""Tests for the 'Missed Win' classification in classify_move.

Background (BurakCan778 vs yiinny, move 12): Bd6 declined a forced mate
(Bb4+ #2) but left Black still up a full piece (+465cp, ~84% win). The
cp-delta arm called it a Blunder only because a forced mate is stored as the
sentinel 100000 — subtracting it yields a ~-100000 "drop" unrelated to how much
the position actually worsened. In practical (win%) terms it was an Inaccuracy.

A move that declines a winning chance but leaves the player STILL clearly
winning is its own category — 'Missed Win' (cf. Chess.com's 'Miss') — not a
Blunder. Its severity should reflect the honest practical damage (the win% arm),
so it never inflates the player's blunder count.
"""
from helper_functions import classify_move

# Forced mate is stored as mate_score=100000; mate-in-2 ≈ 99998.
MATE = 99998


def test_missed_mate_still_winning_is_missed_win():
    """Move 12: mate available, declined, still +465cp (~84%). A forced mate is a
    winning shot, so the caller passes best_is_winning_shot=True."""
    r = classify_move(eval_before_cp=MATE, eval_after_cp=453, best_eval_cp=MATE, best_is_winning_shot=True)
    assert r["missed_win"] is True
    assert r["label"] == "Missed Win"
    assert r["missed_opportunity"] is True
    # Severity reflects practical damage, NOT the sentinel-inflated cp arm.
    assert r["severity"] == "Inaccuracy"
    assert r["severity"] != "Blunder"
    # The raw arm outputs are preserved for transparency (surface the disagreement).
    assert r["cp_severity"] == "Blunder"
    assert r["win_severity"] == "Inaccuracy"


def test_missed_win_does_not_count_as_blunder():
    """The whole point: a Missed Win must not inflate blunder stats."""
    r = classify_move(eval_before_cp=MATE, eval_after_cp=465, best_eval_cp=MATE, best_is_winning_shot=True)
    assert r["severity"] != "Blunder"


def test_declining_mate_into_equality_stays_blunder():
    """Mate available, but the move played drops to ~equal (+100cp, ~59%).
    You really did throw the win away — that stays a Blunder + Miss."""
    r = classify_move(eval_before_cp=MATE, eval_after_cp=100, best_eval_cp=MATE, best_is_winning_shot=True)
    assert r["missed_win"] is False
    assert r["severity"] == "Blunder"
    assert "Miss" in r["label"]
    assert "Blunder" in r["label"]


def test_plain_blunder_unaffected():
    """A normal hung piece from an equal position (no winning chance missed)
    is still a Blunder and is NOT a missed win."""
    r = classify_move(eval_before_cp=20, eval_after_cp=-400, best_eval_cp=20)
    assert r["missed_win"] is False
    assert r["severity"] == "Blunder"
    assert r["label"] == "Blunder"


def test_good_move_unaffected():
    r = classify_move(eval_before_cp=30, eval_after_cp=20, best_eval_cp=30)
    assert r["missed_win"] is False
    assert r["label"] == "Good"

"""Redefine 'Miss': fires only when the engine's best move was a CONCRETE
winning shot (forced mate or a material-winning capture) that was passed up.

See docs/superpowers/specs/2026-06-04-miss-redefinition-design.md.
"""
import chess
from helper_functions import best_is_winning_shot, classify_move

MATE = 99998


def b(fen):
    return chess.Board(fen)


# ---- best_is_winning_shot ----
def test_material_winning_capture_is_shot():
    # Move 29: Bxe6 captures the undefended queen on e6.
    pos = b("5rk1/pp5p/2p1Qpp1/5b2/8/q1P2P2/5RKP/3R4 b - - 1 29")
    assert best_is_winning_shot(pos, "Bxe6", 700) is True


def test_quiet_defensive_best_is_not_shot():
    # Move 19: Qc5 merely defends the c4 knight — no material won, no mate.
    pos = b("r3k2r/pp3p1p/2pqp1p1/5b2/2n5/P1P2PQ1/7P/R1B1KB1R b KQkq - 0 19")
    assert best_is_winning_shot(pos, "Qc5", 410) is False


def test_even_trade_capture_is_not_shot():
    # Move 14: Qxe3+ captures the queen but e3 is defended by Bc1 — an even trade.
    pos = b("r3k2r/pp3ppp/1np1p3/4nb2/1bPq4/2N1Q3/PP4PP/R1B1KBNR b KQkq - 3 14")
    assert best_is_winning_shot(pos, "Qxe3+", 583) is False


def test_forced_mate_is_shot():
    pos = b("rn2kb1r/pp3ppp/4p3/4Nb2/2Pq2P1/8/PP2QP1P/R3KB1R b KQkq - 0 12")
    assert best_is_winning_shot(pos, "Bb4+", MATE) is True


def test_free_pawn_capture_is_not_shot():
    # Winning a single undefended pawn is below the material threshold.
    pos = b("4k3/8/8/8/8/1p6/8/3QK3 w - - 0 1")  # Qxb3 nets just a pawn
    assert best_is_winning_shot(pos, "Qxb3", 150) is False


def test_check_that_wins_nothing_is_not_shot():
    # A non-capturing check is not a winning shot.
    pos = b("r3k2r/pp3p1p/2pq2p1/4pb2/2B5/P1P2PQ1/7P/R1B2RK1 b - - 2 21")
    assert best_is_winning_shot(pos, "Qc5+", 374) is False


# ---- classify_move with the flag ----
def test_no_miss_when_best_is_not_a_shot():
    # Move 19 shape: big drop, but best move (Qc5) isn't a shot -> plain Blunder.
    r = classify_move(410, 66, best_eval_cp=410, best_is_winning_shot=False)
    assert r["missed_opportunity"] is False
    assert "Miss" not in r["label"]


def test_miss_when_shot_passed_up_and_now_losing():
    r = classify_move(700, -200, best_eval_cp=700, best_is_winning_shot=True)
    assert r["missed_opportunity"] is True
    assert "Miss" in r["label"] and "Blunder" in r["label"]


def test_missed_win_when_shot_and_still_winning():
    r = classify_move(MATE, 465, best_eval_cp=MATE, best_is_winning_shot=True)
    assert r["label"] == "Missed Win"
    assert r["missed_win"] is True


def test_no_flag_means_no_miss():
    r = classify_move(410, 66, best_eval_cp=410, best_is_winning_shot=None)
    assert r["missed_opportunity"] is False

"""Regression tests for the discovered-attack detector in _compute_chess_facts.

Bug (BurakCan778 vs yiinny, move 12): the detector flagged ANY player piece
attacked by ANY non-mover after the opponent's reply, with no check that the
attack was NEW or that the mover unblocked the line. It reported "Bg2 reveals
an attack on your bishop on f5 and pawn on f7" — but the g4 pawn already
attacked f5 and the e5 knight already attacked f7 before Bg2, and the g2 bishop
touches neither square.

A real discovered attack requires:
  1. the attack to be NEW (did not exist before the opponent's move), and
  2. a genuine discovery — a sliding rear piece (rook/bishop/queen) whose ray to
     the target passes through the square the opponent's piece just vacated.
"""
from gemini_client import _compute_chess_facts


def test_quiet_move_is_not_a_discovered_attack():
    """Bg2 is a quiet developing move. g4->f5 and Ne5->f7 are static attacks
    that pre-date Bg2, so they must NOT be reported as a discovered attack."""
    facts = _compute_chess_facts(
        fen_before="rn2kb1r/pp3ppp/4p3/4Nb2/2Pq2P1/8/PP2QP1P/R3KB1R b KQkq - 0 12",
        player_san="Bd6",
        pv_played_san="Bd6 Bg2 Bxe5 gxf5",
        best_san="Bb4+",
    )
    assert "reveals an attack" not in (facts["opponent_reply_desc"] or ""), (
        f"False discovered attack reported: {facts['opponent_reply_desc']!r}"
    )
    assert facts["motif"] != "discovered_attack"


def test_best_move_desc_grounds_real_consequences_only():
    """A quiet best move's verified description must state only TRUE consequences,
    so the LLM can't invent (e.g. 'Qc5 attacks the queen on g3' — it doesn't).
    Qc5 here genuinely defends the c4 knight; it does not reach g3."""
    facts = _compute_chess_facts(
        fen_before="r3k2r/pp3p1p/2pqp1p1/5b2/2n5/P1P2PQ1/7P/R1B1KB1R b KQkq - 0 19",
        player_san="e5",
        pv_played_san="e5 Bxc4",
        best_san="Qc5",
    )
    desc = facts["best_move_desc"]
    assert "c4" in desc and "defends" in desc, desc
    assert "g3" not in desc, desc


def test_real_discovered_attack_is_still_detected():
    """Genuine discovery must still fire. Black knight on e5 screens the black
    rook on e8 from the white queen on e1. After White's waiting move (Kh1),
    Black plays Nc6: the knight vacates e5, revealing Re8's attack on Qe1 — an
    attack that did not exist while the knight blocked the file."""
    facts = _compute_chess_facts(
        fen_before="4r1k1/8/8/4n3/8/8/8/4Q1K1 w - - 0 1",
        player_san="Kh1",
        pv_played_san="Kh1 Nc6",
        best_san="Kh1",
    )
    assert facts["motif"] == "discovered_attack", facts
    assert "reveals an attack on your queen on e1" in facts["opponent_reply_desc"], (
        facts["opponent_reply_desc"]
    )

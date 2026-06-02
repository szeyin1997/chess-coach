"""Guarantee: no illegal move can survive in a summarize result.

The summarizer renders summary / what_next / if_bad_fix.{missed_idea,best_move,
why_best}. Whatever the LLM produces, after validation:
  - if_bad_fix.best_move MUST be the engine's best move (legal by construction),
  - no rendered text field may cite a move that is illegal in every relevant
    position (piece moves AND pawn-move suggestions),
without nuking valid, legal explanations.
"""
import random
import chess
from gemini_client import (
    _first_illegal_move,
    _relevant_fens,
    _validate_summarize_result,
)

# Move-19 position from the grandmastergibbsy vs yiinny game (Black to move).
FEN19 = "r3k2r/pp3p1p/2pqp1p1/5b2/2n5/P1P2PQ1/7P/R1B1KB1R b KQkq - 0 19"
BEST = "Qc5"


def _ctx(fen_before=FEN19, played="e5", best=BEST):
    # Realistic PVs so legitimate deep references (e.g. Qxc4 after Qc5 Bxc4) are
    # in the legality universe — mirrors what the real pipeline feeds.
    fens = _relevant_fens(fen_before, played, best, "Qc5 Bxc4 Qxc4 Qe5", "e5 Bxc4")
    return {
        "fens": fens,
        "facts": {"best_move_desc": "Qc5 defends your knight on c4",
                  "opponent_reply_desc": "Bxc4 captures your knight on c4"},
        "best_san": best,
        "fallback_summary": "Engine best was Qc5: Qc5 defends your knight on c4.",
    }


# ---- detection ----
def test_detects_illegal_piece_move():
    # Black has only the c4 knight; no knight can reach f6 → Nf6 is impossible.
    assert _first_illegal_move("You should have played Nf6 to develop", _relevant_fens(FEN19, "e5", BEST, BEST))


def test_detects_illegal_pawn_suggestion():
    # No d-pawn exists, so 'play d5' is impossible.
    assert _first_illegal_move("Instead you should have played d5", _relevant_fens(FEN19, "e5", BEST, BEST))


def test_legal_moves_and_references_pass():
    fens = _relevant_fens(FEN19, "e5", BEST, "Qc5 Bxc4 Qxc4")
    assert _first_illegal_move("Qc5 defends c4; White can reply Bxc4.", fens) is None
    # 'the pawn on c6' is a reference to an occupied square, not a move — allowed.
    assert _first_illegal_move("Your pawn on c6 is weak.", fens) is None


# ---- end-to-end enforcement ----
def test_best_move_is_forced_to_engine_move():
    r = _validate_summarize_result(
        {"summary": "Qc5 keeps your knight defended.", "what_next": [],
         "if_bad_fix": {"best_move": "Nf6", "why_best": "Qc5 defends c4.", "missed_idea": "Qc5 defends c4."}},
        _ctx())
    assert r["if_bad_fix"]["best_move"] == BEST  # not the LLM's "Nf6"


def test_field_with_illegal_move_is_replaced_not_kept():
    r = _validate_summarize_result(
        {"summary": "You should have played Qe7+ to fork the king.", "what_next": [],
         "if_bad_fix": {"best_move": "Qc5", "why_best": "Qc5 defends c4.", "missed_idea": "Qc5 defends c4."}},
        _ctx())
    assert _first_illegal_move(r["summary"], _ctx()["fens"]) is None


def test_valid_explanation_is_preserved():
    good = "Qc5 keeps your knight on c4 defended; if White tries Bxc4 you recapture Qxc4."
    r = _validate_summarize_result(
        {"summary": good, "what_next": ["Defend attacked pieces before pushing pawns."],
         "if_bad_fix": {"best_move": "Qc5", "why_best": good, "missed_idea": "Qc5 defends c4."}},
        _ctx())
    assert r["summary"] == good  # untouched


# ---- fuzz: no illegal move ever survives, across many positions ----
def test_fuzz_no_illegal_move_survives():
    rng = random.Random(7)
    illegal_tokens = ["Qa8#", "Nh4", "Bb5+", "Rd8", "Ke2", "exd6", "g4"]
    checked = 0
    for _ in range(300):
        b = chess.Board()
        for _ in range(rng.randint(4, 30)):
            ms = list(b.legal_moves)
            if not ms or b.is_game_over():
                break
            b.push(rng.choice(ms))
        if b.is_game_over():
            continue
        legal = list(b.legal_moves)
        if not legal:
            continue
        best = b.san(rng.choice(legal))
        fen_before = b.fen()
        # Pick an illegal token for this position (not in legal SANs).
        legal_sans = {b.san(m) for m in legal}
        bad = next((t for t in illegal_tokens if t not in legal_sans), "Qa8#")
        ctx = {
            "fens": _relevant_fens(fen_before, best, best, best),
            "facts": {"best_move_desc": best, "opponent_reply_desc": None},
            "best_san": best,
            "fallback_summary": f"Engine best was {best}.",
        }
        r = _validate_summarize_result(
            {"summary": f"You should have played {bad} instead.",
             "what_next": [f"Consider {bad} next time."],
             "if_bad_fix": {"best_move": bad, "why_best": f"{bad} is winning.", "missed_idea": f"{bad}."}},
            ctx)
        # Guarantee: nothing rendered cites the illegal move; best_move is the engine move.
        assert r["if_bad_fix"]["best_move"] == best
        for field in [r.get("summary", ""), r["if_bad_fix"].get("why_best", ""), r["if_bad_fix"].get("missed_idea", "")]:
            assert _first_illegal_move(field, ctx["fens"]) is None, (bad, field)
        for tip in r.get("what_next", []):
            assert _first_illegal_move(tip, ctx["fens"]) is None, (bad, tip)
        checked += 1
    assert checked > 100  # sanity: the fuzz actually ran

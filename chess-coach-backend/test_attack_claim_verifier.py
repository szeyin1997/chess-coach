"""Hard verifier for attack/defense CLAIMS (the 'Qc5 attacks the queen on g3'
class — a legal move described with a false consequence).

Legality (is the move playable?) is already hard-guaranteed. This is the other
axis: is what we SAY about the move true? The verifier checks every
'attacks/defends ... on <square>' claim against the board in the position the
move creates, and strips claims that don't hold.
"""
from gemini_client import _first_false_attack_claim, _relevant_fens, _validate_summarize_result

FEN19 = "r3k2r/pp3p1p/2pqp1p1/5b2/2n5/P1P2PQ1/7P/R1B1KB1R b KQkq - 0 19"


def _ctx():
    return {
        "fens": _relevant_fens(FEN19, "e5", "Qc5", "Qc5 Bxc4 Qxc4"),
        "facts": {"player_color": "Black",
                  "best_move_desc": "Qc5 defends your knight on c4 and attacks the opponent's pawn on a3",
                  "opponent_reply_desc": "Bxc4 captures your knight on c4"},
        "best_san": "Qc5",
        "fallback_summary": "Engine best was Qc5: Qc5 defends your knight on c4.",
    }


# ---- detection ----
def test_flags_false_attack_on_g3():
    # After Qc5, Black does NOT attack g3 (queen on c5 can't reach it).
    assert _first_false_attack_claim("Qc5 would attack White's queen on g3.", _ctx())


def test_true_defense_of_c4_passes():
    # After Qc5, the queen on c5 does defend the c4 knight.
    assert _first_false_attack_claim("Qc5 defends your knight on c4.", _ctx()) is None


def test_true_attack_on_a3_passes():
    # After Qc5, the queen on c5 does attack the a3 pawn (c5-b4-a3).
    assert _first_false_attack_claim("Qc5 also attacks the opponent's pawn on a3.", _ctx()) is None


# ---- end-to-end: false claim is stripped from the rendered field ----
def test_validator_strips_false_attack_claim():
    r = _validate_summarize_result(
        {"summary": "Qc5 defends your knight on c4 and attacks White's queen on g3.",
         "what_next": [],
         "if_bad_fix": {"best_move": "Qc5", "why_best": "Qc5 defends your knight on c4.",
                        "missed_idea": "Qc5 defends your knight on c4."}},
        _ctx())
    assert _first_false_attack_claim(r["summary"], _ctx()) is None

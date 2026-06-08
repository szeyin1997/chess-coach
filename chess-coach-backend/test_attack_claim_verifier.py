"""Hard verifier for attack/defense CLAIMS (the 'Qc5 attacks the queen on g3'
class — a legal move described with a false consequence).

Legality (is the move playable?) is already hard-guaranteed. This is the other
axis: is what we SAY about the move true? The verifier checks every
'attacks/defends ... on <square>' claim against the board in the position the
move creates, and strips claims that don't hold.
"""
from gemini_client import _first_false_attack_claim, _relevant_fens, _validate_summarize_result

FEN19 = "r3k2r/pp3p1p/2pqp1p1/5b2/2n5/P1P2PQ1/7P/R1B1KB1R b KQkq - 0 19"

# Move 15: Rc1 (White), engine best Bxf5. Nc6 is best/near-best reply; Qxe3+ is another reply.
FEN15 = "rn2k2r/ppp2p2/6p1/2q1bbBp/2P5/1P1BPP2/P5PP/R2Q1RK1 w kq - 0 15"


def _ctx15():
    return {
        "fens": _relevant_fens(FEN15, "Rc1", "Bxf5", "Rc1 Nc6", "Rc1 Qxe3+"),
        "facts": {"player_color": "White"},
        "best_san": "Bxf5",
        "fallback_summary": "Engine best was Bxf5.",
    }


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


# ---- A+B: attacker-identity and impossible-geometry guards ----

def test_flags_false_knight_pin_on_e3():
    # B: knight can never pin. "Nc6 pins your pawn on e3" — knight on c6 doesn't
    # attack e3 AND knights structurally cannot pin. Should be rejected.
    assert _first_false_attack_claim(
        "Black plays Nc6, which pins your pawn on e3.", _ctx15()
    )


def test_flags_false_named_opponent_attack_on_own_piece():
    # A: Nc6 is named; the knight on c6 does NOT attack e3 (attacks a5/a7/b4/b8/d4/d8/e5/e7).
    # Verb is "attacking" (not pin), so B doesn't fire — A must catch it.
    assert _first_false_attack_claim(
        "Black plays Nc6, attacking your pawn on e3.", _ctx15()
    )


def test_true_named_opponent_attack_passes():
    # A pass path: Qxe3+ lands the queen on e3; queen on e3 DOES attack g1 (diagonal).
    # Must NOT be stripped — this is a true claim.
    assert _first_false_attack_claim(
        "Then Qxe3+, which attacks your king on g1.", _ctx15()
    ) is None


def test_unnamed_opponent_threat_still_skipped():
    # No SAN named → preserve the deep-tactic safety valve.
    # f1 isn't actually attacked, but without a named piece we don't verify — documents the tradeoff.
    assert _first_false_attack_claim(
        "This attacks your rook on f1.", _ctx15()
    ) is None


def test_knight_cannot_pin_even_if_attacks_square():
    # Isolates B independent of A: Ne5 (knight d3→e5) genuinely attacks c6,
    # so A's geometry check would PASS — but a knight can never pin, so B must reject.
    ctx = {
        "fens": _relevant_fens("4k3/8/2p5/8/8/3N4/8/4K3 w - - 0 1", "", "", "Ne5"),
        "facts": {"player_color": "White"},
        "best_san": "",
        "fallback_summary": "x",
    }
    assert _first_false_attack_claim(
        "White's Ne5 pins the opponent's pawn on c6.", ctx
    )


# ---- end-to-end: false claim is stripped from the rendered field ----
def test_validator_strips_false_attack_claim():
    r = _validate_summarize_result(
        {"summary": "Qc5 defends your knight on c4 and attacks White's queen on g3.",
         "what_next": [],
         "if_bad_fix": {"best_move": "Qc5", "why_best": "Qc5 defends your knight on c4.",
                        "missed_idea": "Qc5 defends your knight on c4."}},
        _ctx())
    assert _first_false_attack_claim(r["summary"], _ctx()) is None

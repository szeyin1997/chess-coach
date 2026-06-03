"""Hand-labelled golden set for CLASSIFICATION accuracy (`classify_move`).

Each case asserts the CHESS-CORRECT diagnosis — independent of what the code
currently outputs. The eval compares classify_move's output to these labels; a
disagreement means EITHER the label is wrong OR the code is wrong. Investigate;
do not assume the code is right (that's the whole point of ground truth).

Position is given as either:
  - `fen`   : a constructed position (FEN), or
  - `setup` : a SAN move sequence from the start position.
`move`   : the player's move to diagnose (SAN).
`rating` : feeds rating-aware classification.
`expect` : the correct {severity, missed_opportunity, missed_win}.
`why`    : the chess reasoning for the correct label (shown on a mismatch).

Start small and trustworthy. Add cases — especially real ones where the badge
has looked wrong to you. Quality of labels > quantity.
"""

GOLDEN = [
    {
        "id": "hang_queen",
        "setup": ["e4", "e5", "Nf3"], "move": "Qh4", "rating": 600,
        "expect": {"severity": "Blunder", "missed_opportunity": False, "missed_win": False},
        "why": "Qh4 hangs the queen outright to Nxh4 — a clear blunder at any level.",
    },
    {
        "id": "allow_scholars_mate",
        "setup": ["e4", "e5", "Bc4", "Bc5", "Qh5"], "move": "Nf6", "rating": 700,
        "expect": {"severity": "Blunder", "missed_opportunity": False, "missed_win": False},
        "why": "Nf6 ignores the Qxf7# threat and allows immediate checkmate.",
    },
    {
        "id": "develop_knight_good",
        "setup": ["e4", "e5"], "move": "Nf3", "rating": 800,
        "expect": {"severity": "Good", "missed_opportunity": False, "missed_win": False},
        "why": "Standard developing move; evaluation essentially unchanged.",
    },
    {
        "id": "ruy_bishop_good",
        "setup": ["e4", "e5", "Nf3", "Nc6"], "move": "Bb5", "rating": 1200,
        "expect": {"severity": "Good", "missed_opportunity": False, "missed_win": False},
        "why": "Main-line Ruy Lopez developing move — a strong, normal move.",
    },
    {
        # Playing the best move (a mate) must NOT be flagged as anything bad.
        "id": "take_the_mate",
        "fen": "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1", "move": "Ra8#", "rating": 900,
        "expect": {"severity": "Good", "missed_opportunity": False, "missed_win": False},
        "why": "Ra8# is the fastest win (mate in 1). The best move is never a mistake.",
    },
    {
        # The edge case the missed_win logic exists for.
        "id": "declined_mate_still_winning",
        "fen": "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1", "move": "Kf1", "rating": 900,
        "expect": {"severity": "Good", "missed_opportunity": True, "missed_win": True},
        "why": ("Ra8# was available; Kf1 declines the mate but White is still completely "
                "winning (up a rook). Declining a mate while staying ~100% is a MISSED WIN, "
                "not a Blunder."),
    },
]

# chess_principles.py
# A curated library of chess principles, each tagged so the coach can select
# only the relevant ones per move rather than dumping everything into the prompt.
#
# Tags used for retrieval:
#   piece type : "pawn", "knight", "bishop", "rook", "queen", "king"
#   theme      : "exchange", "development", "tempo", "king_safety", "pawn_structure",
#                "piece_activity", "tactics", "initiative", "endgame", "opening"
#   phase      : "opening", "middlegame", "endgame"

from typing import Optional

# ── Tactical pattern library (keyed by motif name) ────────────────────────────
# These are injected verbatim into the Gemini prompt when a motif is detected
# by python-chess, so Gemini never has to figure out the pattern itself.

TACTICAL_PATTERNS: dict[str, dict] = {
    "fork": {
        "name": "Fork",
        "description": (
            "A single piece moves to a square where it simultaneously attacks two or more "
            "opponent pieces. Since only one piece can move per turn, at least one of the "
            "attacked pieces will be captured on the next move. Queens, knights, and pawns "
            "are the most common forking pieces."
        ),
    },
    "pin": {
        "name": "Pin",
        "description": (
            "A piece is attacked along a rank, file, or diagonal and cannot (or should not) "
            "move because doing so would expose a more valuable piece behind it. An absolute "
            "pin means the piece is shielding the king and cannot legally move."
        ),
    },
    "skewer": {
        "name": "Skewer",
        "description": (
            "A high-value piece is attacked and forced to move, exposing a lower-value piece "
            "behind it on the same line — the reverse of a pin. After the front piece moves, "
            "the piece behind it is captured."
        ),
    },
    "hanging_piece": {
        "name": "Hanging Piece",
        "description": (
            "A piece is left without a defender and can be captured for free on the next move. "
            "Before completing any move, always verify that every piece you are leaving behind "
            "has at least one defender."
        ),
    },
    "discovered_attack": {
        "name": "Discovered Attack",
        "description": (
            "Moving one piece unmasks an attack from a piece behind it on the same rank, file, "
            "or diagonal. The moving piece can simultaneously make its own threat, creating "
            "two threats at once that are very difficult to meet."
        ),
    },
    "discovered_check": {
        "name": "Discovered Check",
        "description": (
            "A piece moves to reveal a check from a piece behind it. Because the opponent must "
            "respond to the check, the moving piece can make an additional threat — for example, "
            "capturing material — that goes unanswered."
        ),
    },
    "double_check": {
        "name": "Double Check",
        "description": (
            "Two pieces deliver check simultaneously as a result of a discovered check. The king "
            "must move — it cannot block or capture both checking pieces. Double checks are among "
            "the most forcing and decisive tactics in chess."
        ),
    },
    "back_rank": {
        "name": "Back Rank Weakness",
        "description": (
            "The king is trapped on the back rank by its own pawns and has no escape squares. "
            "A rook or queen can deliver checkmate or win decisive material by exploiting this. "
            "Creating a luft (escape square) with h3/g3 or h6/g6 prevents this vulnerability."
        ),
    },
    "trapped_piece": {
        "name": "Trapped Piece",
        "description": (
            "A piece has no safe squares to retreat to and will be captured. This often happens "
            "when a piece ventures too far into enemy territory without support, or when its "
            "retreat squares are blocked or controlled by enemy pawns."
        ),
    },
    "deflection": {
        "name": "Deflection",
        "description": (
            "A move forces an opponent's piece away from a key defensive duty — such as guarding "
            "a piece, a square, or a rank. Once deflected, the target the piece was protecting "
            "becomes vulnerable to capture or attack."
        ),
    },
    "decoy": {
        "name": "Decoy / Attraction",
        "description": (
            "A sacrifice or forcing move lures an opponent's piece to a specific square where it "
            "becomes a target for a follow-up tactic. The opponent is forced to accept, then "
            "falls into the combination."
        ),
    },
    "overloaded": {
        "name": "Overloaded Piece",
        "description": (
            "A single piece is performing two defensive duties simultaneously — for example, "
            "guarding two pieces or a piece and a key square. Attacking both targets at once "
            "forces the overloaded piece to abandon one of its duties."
        ),
    },
    "zwischenzug": {
        "name": "Zwischenzug (In-Between Move)",
        "description": (
            "Instead of the expected response (such as recapturing), an intermediate move is "
            "played first that poses an immediate threat. The opponent must deal with this new "
            "threat, often changing the result of the subsequent exchange."
        ),
    },
    "x_ray": {
        "name": "X-Ray Attack",
        "description": (
            "A long-range piece (bishop, rook, or queen) exerts pressure through an enemy piece "
            "to attack a target behind it. The threat remains active even if the front piece "
            "captures the attacker, because the rear piece recaptures."
        ),
    },
    "back_rank": {
        "name": "Back Rank Mate",
        "description": (
            "The king is trapped on its back rank by its own pawns with no escape squares. "
            "A queen or rook delivers checkmate along the back rank, often supported by a "
            "second piece. This is one of the most common mating patterns at club level. "
            "Creating a 'luft' (escape square) with h3/g3 or h6/g6 prevents it. "
            "Critically: always scan for back-rank mate threats before making active moves — "
            "a checkmate threat must be dealt with first, even before winning material."
        ),
    },
    "checkmate_pattern": {
        "name": "Checkmate Pattern",
        "description": (
            "The opponent's move delivers checkmate — the king has no legal moves and is in check. "
            "The player's move either walked into a mating net or failed to address an "
            "existing checkmate threat. Always look for the opponent's threats before "
            "making your own move, especially checks and forcing sequences."
        ),
    },
}


def get_tactical_pattern(key: str) -> Optional[dict]:
    """Return the tactical pattern dict for a given motif key, or None."""
    return TACTICAL_PATTERNS.get(key)


PRINCIPLES = [
    # ── Development ───────────────────────────────────────────────────────────
    {
        "name": "Develop pieces early",
        "tags": {"development", "opening", "tempo"},
        "text": (
            "In the opening, every move should bring a new piece into the game or "
            "improve its position. Moves that don't develop (shuffling already-developed "
            "pieces, pushing pawns without purpose) waste time and let the opponent build "
            "a stronger position for free."
        ),
    },
    {
        "name": "Don't move the same piece twice in the opening",
        "tags": {"development", "opening", "tempo"},
        "text": (
            "Moving the same piece twice before you've finished developing all your pieces "
            "costs a tempo. Each wasted move is a free move gifted to the opponent to "
            "bring out another piece or seize space."
        ),
    },
    {
        "name": "Castle early to connect rooks and protect the king",
        "tags": {"development", "opening", "king_safety"},
        "text": (
            "Castling does two things: it tucks the king away from the centre where it is "
            "vulnerable, and it connects the rooks so they can support each other. Delaying "
            "castling too long leaves the king exposed to attacks in the middle of the board."
        ),
    },

    # ── Tempo ─────────────────────────────────────────────────────────────────
    {
        "name": "Gaining tempo",
        "tags": {"tempo", "development", "initiative"},
        "text": (
            "A move gains tempo when it forces the opponent to react (e.g. a threat they "
            "must meet), letting you make your next move 'for free'. Moves that grant the "
            "opponent a free developing or improving move are said to lose tempo."
        ),
    },
    {
        "name": "Developing with tempo",
        "tags": {"tempo", "development", "exchange"},
        "text": (
            "An exchange that forces a developing response is especially damaging. "
            "If recapturing a piece also develops the opponent's piece (e.g. recapture "
            "brings out their bishop), you have essentially paid for their development."
        ),
    },

    # ── Exchanges ─────────────────────────────────────────────────────────────
    {
        "name": "Fair vs unfair exchanges",
        "tags": {"exchange", "bishop", "knight", "piece_activity"},
        "text": (
            "An exchange is 'fair' only if both pieces are equally active. Trading your "
            "active, well-placed piece for the opponent's passive or poorly-placed piece "
            "is an unfair exchange — you give more than you receive even if the material "
            "values are equal. Always ask: is their piece as useful as mine?"
        ),
    },
    {
        "name": "The bishop pair advantage",
        "tags": {"exchange", "bishop", "piece_activity", "middlegame"},
        "text": (
            "Having both bishops is a long-term positional advantage, especially in open "
            "or semi-open positions where diagonals are clear. Voluntarily trading a bishop "
            "for a knight surrenders this advantage unless there is a concrete tactical "
            "reason to do so."
        ),
    },
    {
        "name": "Bishops are stronger than knights in open positions",
        "tags": {"exchange", "bishop", "knight", "pawn_structure"},
        "text": (
            "Knights need outposts (stable squares not attacked by enemy pawns) to be "
            "effective. In open positions with few pawns, bishops can control long diagonals "
            "and are generally more valuable. In closed positions with locked pawn chains, "
            "knights can outperform bishops."
        ),
    },
    {
        "name": "Don't trade active pieces for passive ones",
        "tags": {"exchange", "piece_activity"},
        "text": (
            "Before any capture or trade, evaluate both pieces' roles. An active rook on "
            "an open file, a centralised knight on an outpost, or a bishop controlling a "
            "long diagonal is worth more than its nominal value. Trading it away for an "
            "idle piece is losing quality even if the point count looks equal."
        ),
    },

    # ── Piece activity ────────────────────────────────────────────────────────
    {
        "name": "Centralise your pieces",
        "tags": {"piece_activity", "development", "middlegame"},
        "text": (
            "A piece in the centre controls more squares than one on the edge or corner. "
            "A centralised knight on e4/d4 attacks up to 8 squares; the same knight on "
            "a1 attacks only 2. Centralisation is one of the simplest ways to improve "
            "a piece without spending material."
        ),
    },
    {
        "name": "Rooks belong on open files",
        "tags": {"piece_activity", "rook", "middlegame"},
        "text": (
            "A rook on a closed file does almost nothing. Place rooks on open files "
            "(no pawns of either colour) or semi-open files (no your own pawn). Doubling "
            "rooks on an open file creates powerful pressure the opponent must constantly "
            "deal with."
        ),
    },
    {
        "name": "Knights need outposts",
        "tags": {"piece_activity", "knight", "pawn_structure"},
        "text": (
            "A knight is most powerful when it sits on an outpost: a square it can't be "
            "kicked from by an enemy pawn. Deep centralised outposts (d5/e5 for White, "
            "d4/e4 for Black) give a knight enduring influence over the board."
        ),
    },

    # ── King safety ───────────────────────────────────────────────────────────
    {
        "name": "Don't open lines toward your own king",
        "tags": {"king_safety", "pawn_structure"},
        "text": (
            "Pawn moves in front of your castled king (especially g3/h3 or g6/h6 "
            "prematurely) create weaknesses that attackers can exploit. Open files and "
            "diagonals pointing at your king are highways for rooks and bishops."
        ),
    },
    {
        "name": "Removing king defenders is dangerous",
        "tags": {"king_safety", "exchange"},
        "text": (
            "Trading off pieces that shield your king — the f-pawn, the g-knight, a "
            "bishop covering key diagonals — can be catastrophic. Always consider whether "
            "the piece you are trading is a key defender before the swap."
        ),
    },
    {
        "name": "The king in the centre is a target",
        "tags": {"king_safety", "opening", "development"},
        "text": (
            "An uncastled king in the opening is constantly at risk of being attacked "
            "by open files, diagonals, and piece sacrifices. The opponent can open the "
            "centre at will to expose it. Prioritise castling over material gains when "
            "your king is uncastled."
        ),
    },

    # ── Pawn structure ────────────────────────────────────────────────────────
    {
        "name": "Avoid creating weak pawns",
        "tags": {"pawn_structure", "pawn"},
        "text": (
            "Doubled pawns (two pawns on the same file), isolated pawns (no friendly "
            "pawns on adjacent files), and backward pawns (can't be defended by other "
            "pawns) are permanent weaknesses. They require constant piece attention and "
            "give the opponent targets to attack."
        ),
    },
    {
        "name": "Passed pawns must be pushed",
        "tags": {"pawn_structure", "pawn", "endgame"},
        "text": (
            "A passed pawn (no enemy pawns can block or capture it) is a long-term "
            "winning advantage, especially in endgames. It ties down enemy pieces to "
            "stop its advance. Push passed pawns actively; do not let them sit idle."
        ),
    },

    # ── Tactics ───────────────────────────────────────────────────────────────
    {
        "name": "Check for hanging pieces before moving",
        "tags": {"tactics"},
        "text": (
            "Before every move, scan the board: are any of your pieces undefended "
            "(hanging)? A hanging piece can be taken for free. Also check whether your "
            "intended move leaves a piece undefended after the sequence."
        ),
    },
    {
        "name": "Forks attack two pieces at once",
        "tags": {"tactics", "knight", "pawn"},
        "text": (
            "A fork is a single move that attacks two (or more) enemy pieces simultaneously. "
            "Knights are the best forking pieces because their L-shaped movement is hard to "
            "anticipate. Always look for fork opportunities — especially knight forks on "
            "king and queen."
        ),
    },
    {
        "name": "Pins restrict movement",
        "tags": {"tactics", "bishop", "rook", "queen"},
        "text": (
            "A pin attacks a piece that cannot move without exposing a more valuable piece "
            "behind it. An absolute pin (piece in front of the king) means the piece "
            "legally cannot move. Exploit pins by adding attackers to the pinned piece."
        ),
    },
    {
        "name": "Discovered attacks are hard to meet",
        "tags": {"tactics"},
        "text": (
            "A discovered attack occurs when you move one piece to reveal an attack by "
            "another piece behind it. The moving piece can itself make a threat, creating "
            "two threats at once — very difficult to defend against."
        ),
    },

    # ── Initiative ────────────────────────────────────────────────────────────
    {
        "name": "Keep making threats",
        "tags": {"initiative", "middlegame"},
        "text": (
            "The player making threats controls the game. When you make a threat, your "
            "opponent must spend their move responding rather than improving their own "
            "position. A series of threats can snowball into a winning attack."
        ),
    },
    {
        "name": "Don't give up the initiative without compensation",
        "tags": {"initiative", "exchange"},
        "text": (
            "If you have the initiative (your opponent is reacting to you), giving it up "
            "by making a quiet non-threatening move or an unnecessary exchange lets them "
            "regroup and neutralise your advantage. Keep the pressure on unless you gain "
            "concrete material or positional compensation."
        ),
    },
]


# ── Retrieval ──────────────────────────────────────────────────────────────────

def get_relevant_principles(tags: set[str], max_principles: int = 4) -> list[dict]:
    """Return up to `max_principles` principles that share at least one tag with `tags`.

    Principles are ranked by number of matching tags (most relevant first).
    """
    scored = []
    for p in PRINCIPLES:
        overlap = len(p["tags"] & tags)
        if overlap > 0:
            scored.append((overlap, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:max_principles]]


def infer_tags(data: dict) -> set[str]:
    """Infer relevant principle tags from engine move data.

    Uses the SAN string, eval delta, and PV content as signals.
    """
    tags: set[str] = set()
    san = data.get("san", "")
    pv_played = data.get("pv_played_san", "") or ""
    pv_best = data.get("pv_best_san", "") or ""
    eval_before = data.get("eval_before_cp") or 0
    eval_after = data.get("eval_after_cp") or 0
    delta = (eval_after or 0) - (eval_before or 0)

    # Piece type from SAN (uppercase first char is piece; lowercase = pawn)
    first = san[0] if san else ""
    if first == "B":
        tags.add("bishop")
    elif first == "N":
        tags.add("knight")
    elif first == "R":
        tags.add("rook")
    elif first == "Q":
        tags.add("queen")
    elif first == "K":
        tags.add("king")
        tags.add("king_safety")
    else:
        tags.add("pawn")

    # Capture
    if "x" in san:
        tags.add("exchange")

    # Tactics signals: check or large eval drop
    if "+" in san or "#" in san:
        tags.add("tactics")
    if abs(delta) >= 150:
        tags.add("tactics")

    # Hanging piece or blunder-level drop
    if delta <= -200:
        tags.add("piece_activity")

    # Development heuristic: if PV lines contain castling notation
    if "O-O" in pv_played or "O-O" in pv_best:
        tags.add("king_safety")
        tags.add("development")

    # If no specific tags found, fall back to general themes
    if not tags - {"pawn"}:
        tags.update({"development", "piece_activity", "tempo"})

    return tags


def principles_for_move(data: dict, max_principles: int = 4) -> str:
    """Return a formatted string of relevant principles to inject into the prompt."""
    tags = infer_tags(data)
    principles = get_relevant_principles(tags, max_principles=max_principles)
    if not principles:
        return ""
    lines = ["RELEVANT CHESS PRINCIPLES (cite these in your explanation):"]
    for p in principles:
        lines.append(f"- {p['name']}: {p['text']}")
    return "\n".join(lines)

import os
import json
import time
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_RATE_LIMIT_KEYWORDS = ("429", "rate limit", "quota", "resource exhausted", "resourceexhausted", "too many requests")
_DAILY_QUOTA_KEYWORDS = ("per_day", "perday", "daily", "free_tier_requests", "requests_per_day")
_TRANSIENT_KEYWORDS  = ("503", "unavailable", "overloaded", "internal error", "500", "deadline", "timeout")

_DAILY_QUOTA_MSG = (
    "Daily Gemini quota reached on all configured API keys. "
    "Coach explanations will resume after midnight UTC when the quota resets. "
    "To remove this limit, add billing to your Google AI account at "
    "https://aistudio.google.com — typical daily usage costs a few cents."
)
_TRANSIENT_MSG = (
    "Gemini is temporarily unavailable (503/overloaded). "
    "This is an upstream issue on Google's side — please try again in a few seconds."
)

try:
    from google import genai  # type: ignore
except Exception:  # pragma: no cover
    genai = None

from chess_principles import principles_for_move

# List of {"label": str, "client": <Client>, "exhausted": bool}.
# Lazy-init on first use. Once a key returns daily-quota in this process, it's
# marked exhausted and skipped until the process restarts.
_clients: Optional[list] = None


def _init_clients() -> list:
    """Build the client list once, lazily. Primary key first, fallback after.

    Recognizes:
      - GEMINI_API_KEY           (primary, required)
      - GEMINI_API_KEY_FALLBACK  (optional spare key for daily-quota failover)
    """
    global _clients
    if _clients is not None:
        return _clients
    if genai is None:
        raise RuntimeError("google-genai SDK not installed. Add 'google-genai' to requirements.txt and install.")

    entries = []
    primary_key = os.getenv("GEMINI_API_KEY")
    if primary_key:
        entries.append({"label": "primary", "client": genai.Client(api_key=primary_key), "exhausted": False})
    fallback_key = os.getenv("GEMINI_API_KEY_FALLBACK")
    if fallback_key and fallback_key != primary_key:
        entries.append({"label": "fallback", "client": genai.Client(api_key=fallback_key), "exhausted": False})

    if not entries:
        raise RuntimeError("GEMINI_API_KEY not set in environment")

    logger.info("Gemini configured with %d key(s): %s",
                len(entries), ", ".join(e["label"] for e in entries))
    _clients = entries
    return _clients


def get_gemini() -> Any:
    """Return the primary Gemini client. Used by health checks and direct callers.

    For request-time calls, prefer routing through _with_retry which handles
    failover across all configured keys.
    """
    return _init_clients()[0]["client"]


def _with_retry(fn, retries: int = 4, base_delay: float = 2.0):
    """Call fn(client), with automatic failover across configured Gemini keys.

    fn receives a Gemini client and runs one request. Behavior:
      - On daily-quota error: mark the current key exhausted (skipped for the
        rest of this process), try the next configured key.
      - On rate-limit / transient (503): retry the SAME key with exponential
        backoff (rate limits get a 2.5x slower backoff than transients).
      - When all keys are exhausted: raise a clean user-facing message.
    """
    entries = _init_clients()
    available = [e for e in entries if not e["exhausted"]]

    if not available:
        raise RuntimeError(_DAILY_QUOTA_MSG)

    last_exc: Optional[Exception] = None
    for ki, entry in enumerate(available):
        is_last_key = (ki == len(available) - 1)
        for attempt in range(retries):
            try:
                return fn(entry["client"])
            except Exception as e:
                last_exc = e
                err = str(e).lower()
                is_rate_limit  = any(kw in err for kw in _RATE_LIMIT_KEYWORDS)
                is_daily_quota = any(kw in err for kw in _DAILY_QUOTA_KEYWORDS)
                is_transient   = any(kw in err for kw in _TRANSIENT_KEYWORDS)

                if is_daily_quota:
                    logger.warning("Daily quota reached on %s key — marking exhausted, "
                                   "%s", entry["label"],
                                   "failing over to next key" if not is_last_key else "no more keys to try")
                    entry["exhausted"] = True
                    if is_last_key:
                        raise RuntimeError(_DAILY_QUOTA_MSG)
                    break  # try the next key

                if (is_rate_limit or is_transient) and attempt < retries - 1:
                    # Rate limits need longer waits; transient errors clear faster.
                    wait = (base_delay * (2 ** attempt)) * (2.5 if is_rate_limit else 1.0)
                    kind = "rate limit" if is_rate_limit else "transient (503/overloaded)"
                    logger.warning("Gemini %s on %s, retrying in %.1fs (attempt %d/%d)",
                                   kind, entry["label"], wait, attempt + 1, retries)
                    time.sleep(wait)
                elif is_transient:
                    raise RuntimeError(_TRANSIENT_MSG)
                else:
                    raise

    # Defensive: shouldn't reach here because every branch above either returns or raises.
    if last_exc:
        raise last_exc
    raise RuntimeError("Gemini call failed with no recorded exception")


_PIECE_NAMES = {
    "p": "pawn", "n": "knight", "b": "bishop",
    "r": "rook", "q": "queen", "k": "king",
}

# SAN regex used by _validate_sans_legal. Only matches notation that is
# unambiguously a chess MOVE (piece prefix, capture, check/mate, or promotion);
# pure squares like "e4" in prose like "the pawn on e4" are intentionally
# ignored so we don't over-reject. See research notes in CLAUDE.md.
import re as _re
_SAN_RE = _re.compile(
    r"\b(?:O-O-O|O-O"
    r"|[KQRBN][a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?"
    r"|[a-h][1-8]?x[a-h][1-8](?:=[QRBN])?[+#]?"
    r"|[a-h][1-8]=[QRBN][+#]?"
    r"|[a-h][1-8][+#])\b"
)


def _legal_sans(fen: str) -> list:
    """Every legal move in the position as SAN. Feeding this list to the LLM
    bounds its move universe — it cannot cite a move that isn't on this list."""
    import chess as _chess
    try:
        b = _chess.Board(fen)
        return sorted(b.san(m) for m in b.legal_moves)
    except Exception:
        return []


def _current_attacks(fen: str) -> list:
    """List every existing attack relationship in the position. Pre-loading this
    prevents the LLM from inventing attack relationships that don't exist."""
    import chess as _chess
    NAMES = {
        _chess.PAWN: "pawn", _chess.KNIGHT: "knight", _chess.BISHOP: "bishop",
        _chess.ROOK: "rook", _chess.QUEEN: "queen", _chess.KING: "king",
    }
    try:
        b = _chess.Board(fen)
    except Exception:
        return []
    out = []
    for sq in _chess.SQUARES:
        target = b.piece_at(sq)
        if not target:
            continue
        attackers = list(b.attackers(not target.color, sq))
        if not attackers:
            continue
        t_color = "White" if target.color == _chess.WHITE else "Black"
        a_color = "Black" if target.color == _chess.WHITE else "White"
        atk_descs = ", ".join(
            f"{NAMES[b.piece_at(a).piece_type]} on {_chess.square_name(a)}"
            for a in attackers
        )
        defenders = list(b.attackers(target.color, sq))
        def_descs = ", ".join(
            f"{NAMES[b.piece_at(d).piece_type]} on {_chess.square_name(d)}"
            for d in defenders
        ) or "undefended"
        out.append(
            f"{a_color}'s {atk_descs} attacks {t_color}'s {NAMES[target.piece_type]} "
            f"on {_chess.square_name(sq)} (defenders: {def_descs})"
        )
    return out


# Hand-wave phrases that the LLM has historically used to dodge the
# "name the piece + square" requirement. Each is forbidden on its own;
# _is_vague returns True (text is vague) when any pattern matches.
# Keep narrow — we want to catch lazy paraphrase, not legitimate prose.
_VAGUE_PATTERNS = [
    _re.compile(p, _re.IGNORECASE) for p in (
        # Quantifier hand-waves
        r"\bmultiple pieces\b",
        r"\bseveral pieces\b",
        r"\bimportant pieces\b",
        r"\bkey pieces\b",
        r"\bmultiple threats\b",
        r"\bcreate[sd]? (?:a |multiple |several )?threats?\b",
        # "captures your <piece>" without nearby square notation. Matches when
        # there's NO algebraic square within the next ~6 words.
        r"\bcaptur(?:e|es|ing) (?:your |the |a )?(?:pawn|knight|bishop|rook|queen)\b(?![^.]{0,40}\b[a-h][1-8]\b)",
        # "attacks your <piece>" without nearby square
        r"\battack(?:s|ing)? (?:your |the |a )?(?:pawn|knight|bishop|rook|queen|king)\b(?![^.]{0,40}\b[a-h][1-8]\b)",
        # "forks <pieces>" without naming squares — covers singular nouns
        r"\bfork(?:s|ed|ing)? (?:your |the )?(?:pawn|knight|bishop|rook|queen|king)\b(?![^.]{0,60}\b[a-h][1-8]\b)",
        # "forking your pieces" / "fork the pieces" — plural collective without naming any
        r"\bfork(?:s|ed|ing)? (?:your |the |both )?pieces\b",
        # "devastating/dangerous fork" + no square nearby
        r"\b(?:devastating|dangerous|strong|crushing) (?:fork|attack|threat)\b(?![^.]{0,60}\b[a-h][1-8]\b)",
    )
]


def _is_vague(text: str) -> bool:
    """Return True if the LLM output uses hand-wave phrasing that the prompt
    explicitly forbids. Catches paraphrases that the SAN validator can't see
    because they don't cite a specific (illegal) move — e.g. 'captures your
    bishop and creates a devastating fork on multiple pieces' has no SAN at
    all but is still a violation."""
    if not text:
        return False
    for pat in _VAGUE_PATTERNS:
        m = pat.search(text)
        if m:
            logger.warning("rejecting vague LLM output: %r matched %r", m.group(0), pat.pattern)
            return True
    return False


_SQUARE_RE = _re.compile(r"\b[a-h][1-8]\b")


def _has_specifics(text: str) -> bool:
    """Return True if the text has ≥2 distinct concrete references — squares
    (e4, h8) or SAN moves (Nf3, Bxe7). One reference isn't enough: a sentence
    like 'Your move Be2 was a blunder, significantly worsening your position'
    technically cites Be2, but only restates what the user already sees on
    the badge — no actual content about what happens or what to play instead.

    Two references typically means: the played move + the engine's reply, or
    a piece + the square it sits on, or the played move + the punishment
    square. All useful explanations have at least two."""
    if not text:
        return False
    tokens = set(_SQUARE_RE.findall(text))
    tokens.update(_SAN_RE.findall(text))
    return len(tokens) >= 2


def _validate_sans_legal(text: str, fens: list) -> bool:
    """Scan text for chess-move-shaped tokens; return False if any token is
    NOT legal in at least one of the supplied FENs. Used as a post-hoc filter
    to catch LLM hallucinations like 'White pawn captures Black bishop on e7'
    when no pawn move can reach e7 in the actual position.

    Permissive by design: ambiguity (no FENs supplied, regex finds nothing,
    or token is legal in any provided FEN) returns True so we don't over-reject."""
    import chess as _chess
    tokens = _SAN_RE.findall(text or "")
    if not tokens:
        return True
    boards = []
    for f in fens:
        if not f:
            continue
        try:
            boards.append(_chess.Board(f))
        except Exception:
            pass
    if not boards:
        return True
    for tok in tokens:
        ok = False
        for b in boards:
            try:
                b.parse_san(tok)
                ok = True
                break
            except Exception:
                continue
        if not ok:
            logger.warning("rejecting LLM output citing illegal SAN %r", tok)
            return False
    return True


def _compute_chess_facts(fen_before: str, player_san: str, pv_played_san: str, best_san: str) -> dict:
    """Use python-chess to derive verified facts about a move. No LLM involved."""
    import chess as _chess

    NAMES = {
        _chess.PAWN: "pawn", _chess.KNIGHT: "knight", _chess.BISHOP: "bishop",
        _chess.ROOK: "rook", _chess.QUEEN: "queen", _chess.KING: "king",
    }
    PIECE_VALUE = {
        _chess.PAWN: 1, _chess.KNIGHT: 3, _chess.BISHOP: 3,
        _chess.ROOK: 5, _chess.QUEEN: 9, _chess.KING: 100,
    }

    def _hanging_squares(b: "_chess.Board", color: bool) -> set:
        """Return the set of squares where `color`'s pieces are actually losing
        material under opponent capture (simplified SEE — catches the common cases).

        A piece is hanging if any of:
          1. Undefended AND attacked.
          2. Defended, but cheapest attacker is cheaper than the piece (e.g. pawn
             attacks queen — the recapture still loses material).
          3. Attackers outnumber defenders (overwhelmed exchange).

        Notably NOT hanging: pawn attacked by knight but defended by king (pawn
        is the cheapest unit on the board; capturing loses the attacker for the
        pawn — equal trade at worst). This is what was incorrectly flagged before.
        """
        result = set()
        for sq in _chess.SQUARES:
            piece = b.piece_at(sq)
            if not piece or piece.color != color:
                continue
            attackers = list(b.attackers(not color, sq))
            if not attackers:
                continue
            defenders = list(b.attackers(color, sq))
            piece_val = PIECE_VALUE[piece.piece_type]
            cheapest_atk = min(PIECE_VALUE[b.piece_at(a).piece_type] for a in attackers)
            if not defenders:
                result.add(sq); continue
            if cheapest_atk < piece_val:
                result.add(sq); continue
            if len(attackers) > len(defenders):
                result.add(sq); continue
        return result

    board = _chess.Board(fen_before)
    player_color = board.turn  # WHITE or BLACK

    # Snapshot what was already hanging BEFORE the move. We only want to flag
    # pieces the player's move actually caused to become hanging — not stuff
    # that was already under attack regardless. Without this diff, the LLM
    # writes "your move put X under attack" when X was attacked the whole time.
    hanging_before = _hanging_squares(board, player_color)

    # --- Apply player's move ---
    player_move = board.parse_san(player_san)
    board.push(player_move)

    # --- Pieces NEWLY left hanging by this move (after-set minus before-set) ---
    hanging_after = _hanging_squares(board, player_color)
    newly_hanging_sqs = hanging_after - hanging_before
    hanging = [
        f"{NAMES[board.piece_at(sq).piece_type]} on {_chess.square_name(sq)}"
        for sq in sorted(newly_hanging_sqs)
        if board.piece_at(sq) is not None
    ]

    # --- What does the opponent's first PV reply actually do? ---
    opponent_reply_san = None
    opponent_reply_desc = None
    motif = None  # primary tactical pattern

    if pv_played_san:
        tokens = pv_played_san.strip().split()
        if len(tokens) >= 2:
            opponent_reply_san = tokens[1]
            try:
                opp_move = board.parse_san(opponent_reply_san)
                effects = []

                # Direct capture?
                direct_capture_desc = None
                if board.is_capture(opp_move):
                    captured = board.piece_at(opp_move.to_square)
                    if captured:
                        direct_capture_desc = (
                            f"captures your {NAMES[captured.piece_type]} "
                            f"on {_chess.square_name(opp_move.to_square)}"
                        )
                        effects.append(direct_capture_desc)

                board_copy = board.copy()
                board_copy.push(opp_move)
                is_check = board_copy.is_check()
                is_double_check = is_check and board_copy.is_check() and len(list(board_copy.checkers())) >= 2

                # Fork detection: which of the player's pieces does the moved piece attack?
                attacked_by_mover = []
                for sq in board_copy.attacks(opp_move.to_square):
                    piece = board_copy.piece_at(sq)
                    if piece and piece.color == player_color:
                        attacked_by_mover.append(
                            f"{NAMES[piece.piece_type]} on {_chess.square_name(sq)}"
                        )

                # Pin detection: after opponent's move, is a player piece newly pinned?
                pinned_pieces = []
                for sq in _chess.SQUARES:
                    piece = board_copy.piece_at(sq)
                    if piece and piece.color == player_color and piece.piece_type != _chess.KING:
                        if board_copy.is_pinned(player_color, sq):
                            pinned_pieces.append(f"{NAMES[piece.piece_type]} on {_chess.square_name(sq)}")

                # Discovered attack: the opponent's move VACATED a square, opening a
                # sliding piece's line onto one of the player's pieces. Two checks,
                # both required — without them any piece that merely happened to be
                # attacked by a static enemy piece got mislabelled (the Bg2 false
                # positive: g4 already hit f5 and Ne5 already hit f7 before Bg2):
                #   1. NEW — the attacker did not already hit the target before the move.
                #   2. GENUINELY DISCOVERED — the attacker is a slider (rook/bishop/
                #      queen) whose ray to the target runs through the square the mover
                #      just left (knights/pawns/kings can't be "unblocked").
                # Mirrors the before/after diff the hanging-piece detector uses above.
                SLIDERS = (_chess.BISHOP, _chess.ROOK, _chess.QUEEN)
                discovered_targets = []
                for sq in _chess.SQUARES:
                    piece = board_copy.piece_at(sq)
                    if not (piece and piece.color == player_color):
                        continue
                    attackers_after = board_copy.attackers(not player_color, sq)
                    attackers_before = board.attackers(not player_color, sq)
                    for a in attackers_after:
                        if a == opp_move.to_square:
                            continue  # the mover itself — a direct attack, not discovered
                        if a in attackers_before:
                            continue  # attack already existed — not newly revealed
                        atk_piece = board_copy.piece_at(a)
                        if not atk_piece or atk_piece.piece_type not in SLIDERS:
                            continue
                        if opp_move.from_square in _chess.SquareSet(_chess.between(a, sq)):
                            discovered_targets.append(
                                f"{NAMES[piece.piece_type]} on {_chess.square_name(sq)}"
                            )
                            break

                # --- Assign primary motif (priority order) ---
                is_checkmate = board_copy.is_checkmate()
                if is_checkmate:
                    # Classify the checkmate type
                    king_sq = board_copy.king(player_color)
                    king_rank = _chess.square_rank(king_sq)
                    on_back_rank = (
                        (player_color == _chess.BLACK and king_rank == 7) or
                        (player_color == _chess.WHITE and king_rank == 0)
                    )
                    motif = "back_rank" if on_back_rank else "checkmate_pattern"
                    effects.append(
                        f"delivers checkmate — your king on "
                        f"{_chess.square_name(king_sq)} has no escape"
                    )
                elif is_double_check:
                    motif = "double_check"
                    effects.append("gives double check")
                elif len(attacked_by_mover) >= 2:
                    motif = "fork"
                    effects.append(f"forks your {' and '.join(attacked_by_mover)}")
                elif is_check and len(tokens) >= 3:
                    # Check that wins material on the next move
                    try:
                        follow_up = board_copy.parse_san(tokens[2])
                        if board_copy.is_capture(follow_up):
                            cap2 = board_copy.piece_at(follow_up.to_square)
                            if cap2:
                                motif = "fork"  # check + material win = functional fork
                                effects.append(
                                    f"gives check and then wins your "
                                    f"{NAMES[cap2.piece_type]} on "
                                    f"{_chess.square_name(follow_up.to_square)} after the king must move"
                                )
                        else:
                            effects.append("gives check")
                    except Exception:
                        effects.append("gives check")
                elif is_check:
                    effects.append("gives check")
                elif pinned_pieces and not direct_capture_desc:
                    motif = "pin"
                    effects.append(f"pins your {' and '.join(pinned_pieces)}")
                elif discovered_targets and not direct_capture_desc:
                    motif = "discovered_attack"
                    effects.append(f"reveals an attack on your {' and '.join(discovered_targets[:2])}")
                elif direct_capture_desc:
                    motif = "hanging_piece"

                # When the reply creates no concrete tactic, state that plainly —
                # don't invent a value judgment. The old fallback ("seizes a strong
                # position") asserted opponent strength with zero engine basis and
                # was often flatly wrong (e.g. a quiet move while the opponent is
                # still losing by a piece). A quiet move is a verified fact: we
                # checked captures/checks/forks/pins/discoveries and found none.
                opponent_reply_desc = (
                    f"{opponent_reply_san} " + " and ".join(effects)
                    if effects else f"{opponent_reply_san} is a quiet move with no immediate tactic"
                )
            except Exception:
                opponent_reply_desc = opponent_reply_san

    # Fallback motif: if no reply motif but player left pieces hanging
    if motif is None and hanging:
        motif = "hanging_piece"

    # --- What does the best move defend/create? ---
    best_move_desc = None
    board_best = _chess.Board(fen_before)
    try:
        bm = board_best.parse_san(best_san)
        parts = []
        if board_best.is_capture(bm):
            cap = board_best.piece_at(bm.to_square)
            if cap:
                parts.append(f"captures the opponent's {NAMES[cap.piece_type]} on {_chess.square_name(bm.to_square)}")
        board_best.push(bm)
        if board_best.is_check():
            parts.append("gives check")
        best_move_desc = (f"{best_san} " + " and ".join(parts)) if parts else best_san
    except Exception:
        best_move_desc = best_san

    return {
        "player_color": "White" if player_color == _chess.WHITE else "Black",
        "opponent_color": "Black" if player_color == _chess.WHITE else "White",
        "hanging_pieces": hanging,
        "opponent_reply_san": opponent_reply_san,
        "opponent_reply_desc": opponent_reply_desc,
        "best_move_desc": best_move_desc,
        "motif": motif,
    }


def summarize_move(data: Dict[str, Any], model: Optional[str] = None, temperature: float = 0.15) -> Dict[str, Any]:
    """Explain a single move.

    Thin wrapper over summarize_move_batch so the SINGLE and BATCH paths share
    ONE prompt, ONE rule set, ONE schema, and ONE validator. These used to be
    two hand-maintained prompt strings that drifted: the batch version grew
    stricter anti-hand-wave rules and BAD/GOOD examples that the single version
    never got, so the same move read differently depending on which endpoint
    produced it. See CLAUDE.md "no band-aids" — unify, don't duplicate.

    All chess facts (hanging pieces, opponent reply, captures) are still
    pre-computed by python-chess; Gemini only verbalizes them.
    """
    results = summarize_move_batch([data], model=model, temperature=temperature)
    return results[0] if results else {}


def _level_guidance(rating: Optional[int]) -> str:
    """Coaching-level calibration for the explanation prompt, by player rating.

    Research basis (see RESEARCH.md): sub-800 players improve most from piece
    safety and simple one-move tactics — NOT positional nuance, opening theory, or
    deep endgame technique, which are noise at their level. Advice pitched above
    the player just doesn't land. We therefore tell the LLM how deep to pitch
    EVERY advice field.

    On CCT: a full Checks/Captures/Threats scan EVERY move is too slow for a
    beginner in real games — they flag (run out of time) or abandon it. So the
    beginner habit we teach is the cheap version — Heisman's one-question safety
    check on the move they've ALREADY chosen ("after this move, can my opponent
    take something of mine for free, or hit me with a check/threat I can't meet?")
    — plus playing slower time controls so there's time to ask it. Reserve the
    full board scan for improvers+, where it's affordable.

    rating=None returns "" (generic advice, unchanged behavior)."""
    if rating is None:
        return ""
    if rating < 800:
        return (
            "PLAYER LEVEL: beginner (rating ~{r}). Pitch ALL advice to this level:\n"
            "    - The core habit is a FAST safety check on the move they already want to "
            "play — not a slow scan of the whole board. Frame it as one question: 'After "
            "this move, can my opponent capture something of mine for free, or give a check "
            "or threat I can't answer?' This takes a couple of seconds on ONE move.\n"
            "    - If time pressure is the real issue, it's fine to advise playing slower "
            "time controls (Rapid 10-15+ min, not Blitz) so there's time for that check.\n"
            "    - Most losses here are from not noticing a piece was hanging or a threat "
            "was coming, NOT from bad calculation. Keep tactics to ONE move.\n"
            "    - AVOID opening theory, long-term positional concepts (prophylaxis, weak "
            "squares, the bishop pair) and deep endgame technique — they don't help at this level.\n"
            "    - Plain language; if you use a chess term, explain it in a few words.\n"
            "    - Do NOT tell the player to 'scan every check, capture and threat on every "
            "move' — that's too slow for a real game and they won't do it."
        ).format(r=rating)
    if rating < 1400:
        return (
            "PLAYER LEVEL: improver (rating ~{r}). Pitch advice to this level:\n"
            "    - Two-move tactics and short calculation, basic plans, piece activity and "
            "simple endgames are fair game.\n"
            "    - Reinforce a safety check on candidate moves (can the opponent reply with "
            "a check/capture/threat you can't meet?) — the biggest leak here. A full CCT "
            "board scan is affordable now, especially on forcing or sharp positions.\n"
            "    - Light positional ideas are fine; avoid advanced strategy and deep theory."
        ).format(r=rating)
    return (
        "PLAYER LEVEL: intermediate+ (rating ~{r}). You may use positional concepts "
        "(prophylaxis, weak squares, pawn structure, the bishop pair), deeper calculation "
        "and endgame technique, and assume standard chess vocabulary."
    ).format(r=rating)


def _summarize_item_block(data: Dict[str, Any]) -> Dict[str, Any]:
    """Build the per-item prompt section + return validation context for one move.

    Returns a dict with:
      - section: the verified-facts block to splice into a prompt
      - fens: list of FENs to validate against (for post-hoc SAN check)
      - facts: the _compute_chess_facts result (used for fallbacks)
      - best_san: the engine's best move
      - fallback_summary: mechanical sentence used when LLM output is rejected
      - classification: classify_move's full dict
    """
    from helper_functions import classify_move
    import chess as _chess

    eval_before = data.get('eval_before_cp', 0) or 0
    eval_after  = data.get('eval_after_cp',  0) or 0
    best_eval   = data.get('best_eval_cp')
    delta_cp    = eval_after - eval_before

    # Compute classification at this endpoint's depth (19) — needed for win %
    # display fallback. We then override the framing-driving fields with the
    # UI's badge-level classification (depth=8 from /analyze-chessdotcom) when
    # the frontend supplied them, so the FRAMING the LLM explains matches the
    # badge the user clicked. (The LLM no longer emits its own verdict — the
    # engine severity badge is the sole judgment.) See CLAUDE.md "no label_delta".
    classification = classify_move(eval_before, eval_after, best_eval, rating=data.get('rating'))
    ui_severity = data.get('severity')
    if ui_severity is not None:
        classification['label']              = data.get('label') or ui_severity
        classification['severity']           = ui_severity
        classification['missed_opportunity'] = bool(data.get('missed_opportunity'))
        classification['missed_win']         = bool(data.get('missed_win'))
        # If UI didn't tell us created_problem explicitly, infer from severity:
        # anything worse than Good created a problem unless it was a pure Miss.
        if data.get('created_problem') is not None:
            classification['created_problem'] = bool(data['created_problem'])
        else:
            classification['created_problem'] = ui_severity != 'Good'

    # Display values: prefer the UI's cp_delta and cp_before/cp_after if
    # provided so the severity line shown to the LLM matches what the badge
    # was computed from. Falls back to depth=19 numbers otherwise.
    display_delta = data.get('cp_delta') if data.get('cp_delta') is not None else int(delta_cp)
    if data.get('cp_before') is not None and data.get('cp_after') is not None:
        try:
            from helper_functions import win_percent
            win_b = win_percent(int(data['cp_before']))
            win_a = win_percent(int(data['cp_after']))
        except Exception:
            win_b, win_a = classification['win_before'], classification['win_after']
    else:
        win_b, win_a = classification['win_before'], classification['win_after']

    severity = "{lbl} (cp delta {dcp:+d}; winning chance {wb:.0f}% → {wa:.0f}%)".format(
        lbl=classification["label"],
        dcp=int(display_delta),
        wb=win_b,
        wa=win_a,
    )

    if classification.get("missed_win"):
        framing = (
            "FRAMING: This is a MISSED WIN — a forced or clearly winning line was "
            "available and the player declined it, but they are STILL clearly winning. "
            "Lead with the win they missed and the exact line that wins. Do NOT say the "
            "opponent gained anything, 'seized' a position, or that the player's chances "
            "collapsed — they did not. The point is the faster/forced win that was on the "
            "board, not damage done."
        )
    elif classification["missed_opportunity"] and classification["created_problem"]:
        framing = (
            "FRAMING: This move BOTH missed a winning opportunity AND worsened the position. "
            "Lead with the missed idea, then how their move backfired."
        )
    elif classification["missed_opportunity"]:
        framing = (
            "FRAMING: This is a MISS — a much stronger winning move was available. "
            "Focus on the missed opportunity, NOT on punishment."
        )
    elif classification["created_problem"]:
        framing = (
            f"FRAMING: This move is a {classification['severity']}. Explain WHY it's bad — "
            "what did the opponent's reply exploit, what should the player have played instead."
        )
    else:
        framing = "FRAMING: This move barely changed the position. Don't overstate."

    try:
        facts = _compute_chess_facts(
            fen_before    = data.get('fen', ''),
            player_san    = data.get('san', ''),
            pv_played_san = data.get('pv_played_san', ''),
            best_san      = data.get('best_san', ''),
        )
    except Exception as e:
        raise RuntimeError(f"Chess analysis error: {e}")

    from chess_principles import get_tactical_pattern
    pattern = get_tactical_pattern(facts.get("motif")) if facts.get("motif") else None
    pattern_block = (
        f"TACTICAL PATTERN: {pattern['name']} — {pattern['description']}"
        if pattern else ""
    )

    # Relevant coaching principles, selected by tag from chess_principles. This
    # was computed-but-discarded in the old single-call path (never interpolated
    # into the prompt) — now wired into the shared block so both paths get it.
    principles_block = principles_for_move(data)

    # Rating-aware advice calibration: tells the LLM how deep to pitch the
    # coaching (vocabulary, which concepts to use/avoid) for this player's level.
    level_block = _level_guidance(data.get('rating'))

    hanging_line = (
        "Pieces newly hanging after the move: " + ", ".join(facts["hanging_pieces"])
        if facts["hanging_pieces"] else
        "This move did not leave any of the player's pieces newly hanging"
    )
    reply_line = (
        f"Opponent's best reply (verified): {facts['opponent_reply_desc']}"
        if facts["opponent_reply_desc"] else ""
    )
    best_line_str = f"Engine's best alternative (verified): {facts['best_move_desc']}"

    fen_before = data.get('fen', '')
    legal_before = ", ".join(_legal_sans(fen_before)) or "(none)"
    attacks_list = _current_attacks(fen_before)
    attacks_block = ("\n    " + "\n    ".join(attacks_list)) if attacks_list else " (no piece is currently attacked)"

    section = (
        f"  {framing}\n"
        + (f"  {level_block}\n" if level_block else "")
        + (f"  {pattern_block}\n" if pattern_block else "")
        + (f"  {principles_block}\n" if principles_block else "")
        + f"  Player: {facts['player_color']}\n"
        + f"  Move played: {data.get('san','?')}  [{severity}]\n"
        + f"  {hanging_line}\n"
        + (f"  {reply_line}\n" if reply_line else "")
        + f"  {best_line_str}\n"
        + f"  Engine best: {data.get('best_san','?')}  (eval {data.get('best_eval_cp','?')} cp)\n"
        + f"  PV after best: {data.get('pv_best_san','(none)')}\n"
        + f"  All legal moves in this position: {legal_before}\n"
        + f"  All existing attacks:{attacks_block}"
    )

    fallback_summary = (
        f"Engine best was {data.get('best_san','?')}: {facts.get('best_move_desc') or data.get('best_san','?')}. "
        + (f"Opponent's punishment: {facts.get('opponent_reply_desc')}." if facts.get('opponent_reply_desc') else "")
    ).strip()

    fens = [fen_before]
    try:
        b = _chess.Board(fen_before)
        b.push(b.parse_san(data.get('san', '')))
        fens.append(b.fen())
    except Exception:
        pass

    return {
        "section": section,
        "fens": fens,
        "facts": facts,
        "best_san": data.get('best_san', '?'),
        "fallback_summary": fallback_summary,
        "classification": classification,
    }


def _validate_summarize_result(result: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Post-hoc SAN validation on a summary result. Any text field citing
    illegal SANs falls back to a mechanical sentence."""
    fens = ctx["fens"]
    facts = ctx["facts"]
    best_san = ctx["best_san"]
    fallback = ctx["fallback_summary"]

    # A text field is rejected if ANY of:
    #   (a) it cites a SAN that isn't legal in any provided FEN
    #   (b) it uses a forbidden hand-wave phrase ("captures your bishop" with
    #       no square, "fork on multiple pieces", etc.)
    #   (c) for tactical fields (summary, missed_idea, why_best), it has no
    #       concrete specifics at all — no squares, no SAN moves. Catches the
    #       LLM dodging by going fully generic ("decisive advantage") instead.
    def _ok_basic(s: str) -> bool:
        return _validate_sans_legal(s, fens) and not _is_vague(s)
    def _ok_concrete(s: str) -> bool:
        return _ok_basic(s) and _has_specifics(s)

    # --- Enforce the output TYPE contract before content validation. ---
    # The LLM call sets response_mime_type=json but has NO response_schema, so
    # field types are NOT guaranteed. The frontend assumes summary:str,
    # what_next:list[str], if_bad_fix.{missed_idea,best_move,why_best}:str. An
    # off-shape value (what_next as a string, best_move as an object) would reach
    # React and throw during render ("map is not a function" / "objects are not
    # valid as a React child"), blanking the whole page (there is no ErrorBoundary).
    # Coerce here — the one shared validator — so no endpoint can emit a bad shape.
    # NOTE: the content helpers (_ok_basic/_ok_concrete) run regex and raise on
    # non-strings, so every call below is guarded by an isinstance(str) check.

    # summary -> a non-empty, concrete string (else the mechanical fallback).
    #   Covers: missing/empty summary, dropped {} padding, off-type (dict/list),
    #   and strings that fail content validation.
    summary = result.get('summary')
    if not isinstance(summary, str) or not summary.strip() or not _ok_concrete(summary):
        result['summary'] = fallback

    # if_bad_fix -> a dict of strings, or dropped entirely (the UI optional-chains it).
    fix = result.get('if_bad_fix')
    if not isinstance(fix, dict):
        result.pop('if_bad_fix', None)
    else:
        # Drop any non-string field so the UI never renders an object as a child.
        for k in ('missed_idea', 'best_move', 'why_best'):
            if k in fix and not isinstance(fix[k], str):
                fix.pop(k, None)
        # Build a more useful missed_idea fallback than just "best_move_desc"
        # (which is a one-liner like "Ng3"). Pair it with the engine's
        # punishment so the user sees both the cost and the alternative.
        mi_fallback = (
            (f"{facts.get('best_move_desc') or best_san}. " if facts.get('best_move_desc') else "")
            + (f"Punishment after your move: {facts.get('opponent_reply_desc')}." if facts.get('opponent_reply_desc') else "")
        ).strip() or (facts.get('best_move_desc') or best_san)
        for k, fb in (("missed_idea", mi_fallback), ("why_best", facts.get('best_move_desc') or best_san)):
            if fix.get(k) and not _ok_concrete(fix[k]):
                fix[k] = fb

    # what_next -> list[str], each item passing the basic content filter. A bare
    # string becomes a one-item list; non-string items are dropped.
    wn = result.get('what_next')
    if isinstance(wn, str):
        wn = [wn] if wn.strip() else []
    elif isinstance(wn, list):
        wn = [w for w in wn if isinstance(w, str) and w.strip()]
    else:
        wn = []
    result['what_next'] = [w for w in wn if _ok_basic(w)]

    return result


def summarize_move_batch(items: list, model: Optional[str] = None, temperature: float = 0.15) -> list:
    """Pack N flagged moves into ONE Gemini call. THE single explanation path.

    summarize_move() is a thin wrapper that calls this with one item, so this
    prompt/rule-set/schema/validator is the only one in the codebase — there is
    no longer a second hand-synced copy to drift from.

    Used by /summarize-batch — also saves free-tier quota dramatically when the
    user clicks through several mistakes in the same game (N calls collapse to 1).

    Temperature defaults LOW (0.15): this is a fact-grounded explanation task,
    not creative writing. Higher temps made re-clicking the same move return
    differently-worded explanations — the "feels inconsistent" symptom.

    Input: list of data dicts (same shape as summarize_move expects).
    Output: list of summary dicts (SAME LENGTH, SAME ORDER as input).
    """
    if not items:
        return []
    model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    item_blocks = []
    sections = []
    for i, data in enumerate(items, 1):
        try:
            ctx = _summarize_item_block(data)
            item_blocks.append(ctx)
            sections.append(f"#{i}:\n{ctx['section']}")
        except Exception as e:
            logger.warning("summarize_move_batch: item %d failed prep: %s", i, e)
            item_blocks.append(None)
            sections.append(f"#{i}:\n  (data unavailable — skip this item)")

    # The strict rules below exist because earlier versions let vague tactical
    # claims through ("a dangerous fork threatening two of your pawns" — which
    # fork? which pawns?). This is now the ONLY explanation prompt in the
    # codebase; summarize_move() routes through here with a single item.
    prompt = (
        "You are an encouraging chess coach. All chess facts below were computed "
        "by a chess engine and python-chess — they are CORRECT. Do not "
        "recalculate or second-guess them.\n\n"
        "For EACH numbered position below, write a coaching explanation that "
        "respects that position's FRAMING. Each position has its own verified "
        "facts (move played, opponent reply, engine best move, hanging pieces, "
        "legal moves, current attacks) — use only THOSE facts for THAT position.\n\n"
        "When a position states a PLAYER LEVEL, pitch EVERY field (summary, "
        "what_next, if_bad_fix) to that level — its vocabulary, which concepts to "
        "use, and which to avoid. Advice above the player's level is a failure.\n\n"
        "YOUR TASK — for each position, produce these fields:\n"
        '1. "summary": 1-2 sentences explaining THIS move.\n'
        "   - If FRAMING says MISS: explain the WINNING idea the player missed, "
        "written as concrete chess (squares, pieces, threats), NOT generic "
        "platitudes.\n"
        "   - If FRAMING says the move is bad: explain what the opponent's reply "
        "exploits.\n"
        '2. "what_next": 1-2 actionable tips. May reference general principles.\n'
        '3. "if_bad_fix": explain why the engine\'s best move is better.\n'
        "   Set to null if the move barely changed winning chances.\n\n"
        "STRICT RULES (violations make the answer wrong):\n"
        "- Any chess move you cite by name (e.g. Nf3, Bxe7, Qd4+) MUST appear "
        "in that position's 'All legal moves' list. If a move isn't on that "
        "list, it does not exist and you cannot mention it.\n"
        "- Any attack you cite MUST appear in that position's 'All existing "
        "attacks' list, or be a direct consequence of the move played / engine "
        "best move named in that position's facts.\n"
        "- Never claim a piece attacks a piece of its OWN color (impossible).\n"
        "- PRESERVE VERIFIED SPECIFICS. The 'Opponent's best reply' and 'Engine's "
        "best alternative' lines already contain the exact squares and piece "
        "names. Your explanation MUST repeat those specifics — never paraphrase "
        "them into vague phrases. The verified fact is the FLOOR for detail, "
        "not the ceiling.\n"
        "- NAME EVERY PIECE AND SQUARE. When you mention a captured piece, an "
        "attacked piece, or a fork target, you MUST include both the piece type "
        "AND the square it sits on. 'captures your bishop' is FORBIDDEN — write "
        "'captures your bishop on e5'. 'attacks your queen' is FORBIDDEN — write "
        "'attacks your queen on d8'.\n"
        "- NO HAND-WAVE QUANTIFIERS. Phrases like 'multiple pieces', 'several "
        "pieces', 'your pieces', 'important pieces', 'key pieces', 'multiple "
        "threats' are FORBIDDEN — name each piece with its square, or rewrite "
        "to avoid the claim. 'Forks multiple pieces' must become 'forks your "
        "queen on d7 and rook on a8'.\n"
        "- NO VAGUE TACTICAL CLAIMS. If you say 'a fork', 'a pin', 'a double "
        "attack', 'a threat', 'a discovered attack', or 'a skewer', you MUST "
        "name the specific pieces and squares involved. 'A dangerous fork' "
        "without naming the fork is forbidden.\n"
        "- Don't dramatise. If the only thing happening is a 30-cp eval drop, "
        "say so — don't invent tactics to explain it.\n\n"
        "BAD vs GOOD examples (apply to every field):\n"
        "  ✗ 'Your move Be5 allows White to capture your bishop and create a fork.'\n"
        "  ✓ 'Your move Be5 lets White play Nxe5, capturing your bishop on e5 "
        "and forking your queen on d8 and rook on a8.'\n"
        "  ✗ 'Why Nf3 is better: it develops your knight and avoids tactics.'\n"
        "  ✓ 'Why Nf3 is better: it develops your knight to f3, defends the "
        "pawn on e5, and stops Black's queen from reaching h4.'\n\n"
        "POSITIONS:\n"
        + "\n\n".join(sections)
        + "\n\nReturn analyses in the SAME ORDER as the numbered positions. "
        "Respond with JSON ONLY (no markdown fences):\n"
        "{\n"
        '  "analyses": [\n'
        '    {"summary": "...", "what_next": [...], '
        '"if_bad_fix": {"missed_idea": "...", "best_move": "...", '
        '"why_best": "..."} }\n'
        "  ]\n"
        "}"
    )

    try:
        resp = _with_retry(lambda c: c.models.generate_content(
            model=model,
            contents=prompt,
            config={"temperature": float(temperature), "response_mime_type": "application/json"},
        ))
        text = getattr(resp, "text", None) or "{}"
        result = json.loads(text)
        analyses = result.get("analyses", [])
        while len(analyses) < len(items):
            analyses.append({})
        analyses = analyses[:len(items)]
        for i, analysis in enumerate(analyses):
            ctx = item_blocks[i]
            if ctx is None:
                continue
            try:
                analyses[i] = _validate_summarize_result(analysis, ctx)
            except Exception as e:
                logger.warning("summarize_move_batch: item %d validation failed: %s", i, e)
        return analyses
    except Exception as e:
        raise RuntimeError(f"Gemini error: {e}")


def analyze_game(move_history: list, model: Optional[str] = None, temperature: float = 0.3) -> Dict[str, Any]:
    """Analyze a completed game and identify the player's main weakness theme.

    move_history: list of dicts with keys: san, label, cp_delta
    Returns: { weakness_summary, themes, explanation }
    """
    model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    # Build a compact move table for the prompt
    move_lines = []
    for i, m in enumerate(move_history, 1):
        move_lines.append(f"  {i}. {m.get('san','?')}  [{m.get('label','?')}]  Δ{m.get('cp_delta', 0):+d} cp")
    moves_text = "\n".join(move_lines) if move_lines else "  (no moves)"

    prompt = """\
You are a chess coach reviewing a completed game to identify the player's biggest weakness.

GAME MOVES (player is White; label = Good/Inaccuracy/Mistake/Blunder; Δcp = centipawn change):
{moves}

AVAILABLE WEAKNESS THEMES (Lichess puzzle tags):
  mateIn1      - missed forced checkmate in 1 move
  mateIn2      - missed forced checkmate in 2 moves
  mateIn3      - missed forced checkmate in 3 moves
  mateIn4      - missed forced checkmate in 4 moves
  mateIn5      - missed forced checkmate in 5+ moves
  fork         - missed opportunities to attack two pieces at once
  hangingPiece - left pieces undefended or missed capturing free pieces
  pin          - missed or failed to exploit pins
  skewer       - missed skewer tactics
  discoveredAttack - missed discovered attack combinations
  crushing     - missed winning tactical combinations
  defensiveMove - failed to find key defensive resources

TASK:
1. Look at the pattern of mistakes/blunders. What recurring tactical or strategic theme do they suggest?
2. Pick the 1-2 MOST relevant themes from the list above that best describe the player's gap.
3. Write a short, encouraging weakness_summary (2-3 sentences) explaining what the player tends to miss.
4. Write a brief explanation (1-2 sentences) of specific examples from the game.

Respond ONLY with JSON (no markdown fences):
{{
  "weakness_summary": "...",
  "themes": ["theme1", "theme2"],
  "explanation": "..."
}}""".format(moves=moves_text)

    try:
        resp = _with_retry(lambda c: c.models.generate_content(
            model=model,
            contents=prompt,
            config={
                "temperature": float(temperature),
                "response_mime_type": "application/json",
            },
        ))
        text = getattr(resp, "text", None) or ""
        result = json.loads(text or "{}")
        # Validate themes are from our known set
        known = {
            "mateIn1","mateIn2","mateIn3","mateIn4","mateIn5",
            "fork","hangingPiece","pin","skewer",
            "discoveredAttack","crushing","defensiveMove",
        }
        result["themes"] = [t for t in result.get("themes", []) if t in known]
        if not result["themes"]:
            result["themes"] = ["crushing"]  # safe default
        return result
    except Exception as e:
        raise RuntimeError(f"Gemini error: {e}")


def analyze_multiple_games(games: list, model: Optional[str] = None, temperature: float = 0.3) -> Dict[str, Any]:
    """Analyze a player's last N games to identify recurring weakness patterns.

    games: list of { game_info: {white, black, result, time_class, url, opening},
                     moves: [{san, label, cp_delta, move_number}] }
    Returns: { common_weaknesses: [{id, label, description, game_count, occurrences}] }
    """
    model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    # Build compact game summaries — only include non-Good moves to keep prompt tight.
    # Filter on severity (base label only, no composites like "Mistake + Miss"), and
    # also include any move flagged as a missed opportunity (the "Miss"/"+ Miss" cases).
    def _is_significant(m: dict) -> bool:
        sev = m.get("severity", "")
        if sev in ("Mistake", "Blunder"):
            return True
        if m.get("missed_opportunity"):
            return True
        # Backward-compat: older move dicts may only have a composite label string.
        lbl = m.get("label", "")
        return "Mistake" in lbl or "Blunder" in lbl or "Miss" in lbl

    def _format_move(m: dict) -> str:
        base = f"  Move {m['move_number']}: {m['san']} [{m['label']}, {m['cp_delta']:+d}cp]"
        # Tactical enrichment: surface WHAT went wrong, not just the move name.
        # motif is a coarse category (hanging_piece / fork / pin / back_rank / etc.);
        # tactical_summary is a one-liner describing the opponent's punishment.
        bits = []
        if m.get("motif"):
            bits.append(f"motif={m['motif']}")
        if m.get("tactical_summary"):
            bits.append(m["tactical_summary"])
        return base + ("  — " + "; ".join(bits) if bits else "")

    game_blocks = []
    for i, g in enumerate(games):
        info = g.get("game_info", {})
        opponent = info.get("black") if info.get("user_color") == "white" else info.get("white")
        result = info.get("result", "?")
        opening = info.get("opening", "")
        bad_moves = [m for m in g.get("moves", []) if _is_significant(m)]
        if not bad_moves:
            move_text = "  (no significant mistakes)"
        else:
            move_text = "\n".join(_format_move(m) for m in bad_moves)
        opening_str = f" — {opening}" if opening else ""
        game_blocks.append(f"Game {i} vs {opponent} ({result}{opening_str}):\n{move_text}")

    games_text = "\n\n".join(game_blocks)

    prompt = """\
You are a chess coach analyzing {n} recent games to identify a player's most recurring weaknesses.

GAMES (only inaccuracies/mistakes/blunders shown; Δcp = centipawn loss; motif/notes describe what actually went wrong):
{games}

TASK:
Identify 2-3 recurring weakness patterns across these games.

CRUCIAL — categorize by the MOTIF and TACTICAL SUMMARY, not by the move name itself.
A castling move (O-O) that loses a hanging knight is a HANGING-PIECE problem, not a
king-safety problem. A king move that walks into a fork is a TACTICAL-AWARENESS problem,
not a king-safety problem. King-safety only applies when the move objectively exposes
the king to attack (open files toward king, missing pawn shield, etc.) — confirmed by
the motif or tactical_summary, not inferred from the piece moved.

For each weakness:
- Give it a short label (e.g. "Hanging Pieces", "Missed Tactics Under Pressure")
- Write 1-2 sentences describing WHEN and WHY this keeps happening
- List which games (by index) and which move numbers best illustrate it

Respond ONLY with JSON (no markdown fences):
{{
  "common_weaknesses": [
    {{
      "id": "snake_case_id",
      "label": "Short Label",
      "description": "1-2 sentences describing the pattern",
      "occurrences": [
        {{"game_index": 0, "move_numbers": [5, 12]}},
        {{"game_index": 3, "move_numbers": [8]}}
      ]
    }}
  ]
}}""".format(n=len(games), games=games_text)

    try:
        resp = _with_retry(lambda c: c.models.generate_content(
            model=model,
            contents=prompt,
            config={
                "temperature": float(temperature),
                "response_mime_type": "application/json",
            },
        ))
        text = getattr(resp, "text", None) or "{}"
        result = json.loads(text)
        # Attach game_count derived from occurrences
        for w in result.get("common_weaknesses", []):
            w["game_count"] = len(w.get("occurrences", []))
        return result
    except Exception as e:
        raise RuntimeError(f"Gemini error: {e}")


def generate_player_summary(
    games: list,
    stats: Dict[str, Any],
    rating: Optional[int],
    time_class: Optional[str],
    weaknesses: list,
    model: Optional[str] = None,
    temperature: float = 0.3,
) -> Dict[str, Any]:
    """Generate a high-level player profile: top improvement lever + strengths + recommendations.

    Returns:
      { improvement_lever, strengths, recommendations }
    """
    model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    rating_str = f"{rating} ({time_class})" if rating else "unknown"
    wins   = sum(1 for g in games if g.get("game_info", {}).get("result") == "win")
    losses = len(games) - wins

    # Phase where errors cluster most
    err_open = stats.get("errors_opening", 0)
    err_mid  = stats.get("errors_middlegame", 0)
    err_end  = stats.get("errors_endgame", 0)
    miss_count     = stats.get("miss_count", 0)
    active_blunders = stats.get("active_blunders", 0)
    total_errors   = err_open + err_mid + err_end
    phase_lines = (
        f"  Opening (moves 1-15) : {err_open} errors ({round(err_open/total_errors*100) if total_errors else 0}%)\n"
        f"  Middlegame (16-35)   : {err_mid} errors ({round(err_mid/total_errors*100) if total_errors else 0}%)\n"
        f"  Endgame (36+)        : {err_end} errors ({round(err_end/total_errors*100) if total_errors else 0}%)"
    )

    # Format specific weaknesses with their game counts (most frequent first)
    sorted_weaknesses = sorted(weaknesses[:3], key=lambda w: w.get("game_count", 0), reverse=True)
    weakness_lines = "\n".join(
        f"  {i+1}. [{w['game_count']}/{len(games)} games] {w['label']}: {w['description']}"
        for i, w in enumerate(sorted_weaknesses)
    ) or "  (none identified)"

    # Offense vs defense distinction. This is the single most important signal
    # for picking the right recommendations:
    #   - active_blunders = moves where THIS PLAYER created problems (defensive failure
    #     — they walked into a tactic, hung a piece, allowed a fork)
    #   - miss_count       = moves where the PLAYER had a winning move and missed it
    #     (offensive failure — they didn't find their own tactic)
    # Puzzle-solving training (Lichess, etc.) primarily teaches OFFENSIVE pattern
    # recognition: "you are the side delivering the fork — find the move." That helps
    # when the player misses their own wins. It does NOT directly fix falling for
    # opponent's tactics — that needs habit-building (calculate opponent's reply,
    # CCT scan: Checks/Captures/Threats) and dedicated defensive puzzles.
    if active_blunders > miss_count * 1.5:
        primary_failure = "DEFENSIVE — the player keeps walking into opponent's tactics (forks, pins, hanging pieces). Recommendations must teach defensive habits (anticipating opponent's reply), not just tactical pattern recognition."
    elif miss_count > active_blunders * 1.5:
        primary_failure = "OFFENSIVE — the player misses their own winning tactics. Recommendations should target pattern recognition through tactical puzzles."
    else:
        primary_failure = "MIXED — both sides matter. Recommend a balance of defensive habit-building AND offensive puzzle training."

    # Time-pressure vs recognition split of serious errors. This decides whether
    # the right fix is "manage your clock / play slower" or "train recognition" —
    # giving "do a safety check" to someone who blundered with 4 seconds left, or
    # "play slower" to someone who blundered with 4 minutes left, is backwards.
    # Only errors that carried clock data are counted (errors_clock_known).
    err_tp    = stats.get("errors_time_pressure", 0)
    err_wt    = stats.get("errors_with_time", 0)
    err_known = stats.get("errors_clock_known", 0)
    if err_known == 0:
        time_profile = ("UNKNOWN — these games carried no clock data, so do NOT assume "
                        "time pressure either way. Give the recognition/safety-check advice "
                        "by default and don't mention time management unless asked.")
    elif err_tp > err_wt * 1.5:
        time_profile = (f"TIME-PRESSURE-DRIVEN — {err_tp} of {err_known} clocked serious errors "
                        "happened in time pressure. The lever is TIME MANAGEMENT and slower time "
                        "controls (Rapid 10-15+ min, not Blitz/Bullet), plus a FAST one-question "
                        "safety check. Do NOT prescribe 'scan every check/capture/threat every "
                        "move' — there is literally no time for it in their games.")
    elif err_wt > err_tp * 1.5:
        time_profile = (f"RECOGNITION-DRIVEN — {err_wt} of {err_known} clocked serious errors "
                        "happened with time to spare. Time is NOT the problem; the player simply "
                        "isn't checking. The lever is the one-question safety-check habit before "
                        "committing a move, plus tactical puzzles so they spot threats faster.")
    else:
        time_profile = (f"MIXED time profile ({err_tp} in time pressure, {err_wt} with time, of "
                        f"{err_known} clocked errors). Address both: a fast safety-check habit AND "
                        "sensible time management / time-control choice.")

    prompt = """\
You are an experienced chess coach writing a targeted improvement report for one specific player.

PLAYER DATA (last {n} games, {time_class}, rating {rating}):
  Result         : {wins}W / {losses}L
  Accuracy       : {accuracy}% good moves
  Active blunders: {active_blunders} (moves where the PLAYER created problems — defensive failure)
  Missed wins    : {miss_count} (had a winning move but didn't play it — offensive failure)
  Blunders/game  : {blunders_per_game}
  Mistakes       : {mistakes}

PRIMARY FAILURE MODE: {primary_failure}

TIME PROFILE OF ERRORS: {time_profile}

WHERE ERRORS OCCUR (by game phase):
{phase_lines}

SPECIFIC RECURRING WEAKNESSES (ranked by frequency across {n} games):
{weaknesses}

TASK — respond with JSON only (no markdown fences):

1. "improvement_lever": 2–3 sentences. The single most impactful thing to fix.
   RULES:
   - Base it on the #1 weakness above, the phase where errors cluster most, AND the primary failure mode.
   - Be specific to THIS player's data — name the pattern, when it happens, and what to do instead.
   - If the primary failure mode is DEFENSIVE: the lever must be a defensive habit
     (e.g. "before every move, scan opponent's checks/captures/threats"), NOT
     "find more tactics." A player who keeps getting forked doesn't need to find
     more forks — they need to spot when one is about to land on them.
   - At 500–800: the issue is usually one-move oversights and missing opponent's
     immediate threats. At 800–1200: missing 2-move combinations and calculation depth.
   - Write as: "In your games, [specific recurring situation]. When this happens, [what goes wrong].
     Next time, [specific habit to build]."

2. "strengths": list of 2–3 short strings (≤ 12 words each).
   Compare to typical {rating}-rated {time_class} players. Base on actual data only.

3. "recommendations": list of 2–3 concrete next actions tied to THIS player's specific
   weaknesses, phase data, AND primary failure mode. Each recommendation:
   - Names a habit, study target, or drill — not a vague platitude
   - Matches the failure mode:
     * DEFENSIVE failure → habit drills, NOT offensive puzzles. Lichess does NOT have
       "defend against forks" puzzles — that theme does not exist. Do not invent
       puzzle themes. The ONLY genuinely defensive Lichess theme is "defensiveMove"
       (find the only saving move when already under threat). For DEFENSIVE failure,
       MUST include the safety-check habit (verbatim or close paraphrase):
         "Before you commit a move, ask one question: 'Is it safe?' — after this move,
          can my opponent take something of mine for free, or hit me with a check or
          threat I can't meet? Check the move you're about to play, every time."
       This is a FAST check on the move you've chosen, NOT a slow scan of every
       check/capture/threat on the board — that's too slow for a real game.
       Then let the TIME PROFILE decide the second habit:
         - TIME-PRESSURE-DRIVEN → recommend playing slower time controls (Rapid 10-15+
           min, not Blitz/Bullet) and basic time management (spend time on forcing/
           sharp moves, play obvious moves quickly) so there's time for the safety check.
         - RECOGNITION-DRIVEN or UNKNOWN → recommend "sit on your hands" (when you see a
           move you like, pause and check the opponent's reply first) PLUS tactical
           puzzles so threat-spotting becomes fast and automatic.
       You MAY also recommend "defensiveMove" puzzles on Lichess, but NEVER
       fork/pin/discovered-attack puzzles for a defensive-failure player.
     * OFFENSIVE failure → tactical puzzle training on the missed patterns
       ("Solve 10 fork puzzles daily on Lichess" is fine here — these themes exist).
     * MIXED → include the safety-check habit AND one offensive puzzle drill, and add
       time-management advice if the TIME PROFILE says time pressure is involved.
   - References the actual weakness pattern from this player's data
   - Is something the player can do THIS WEEK
   - ≤ 35 words each (CCT and sit-on-hands recommendations may be longer to keep the wording intact)

{{
  "improvement_lever": "...",
  "strengths": ["...", "...", "..."],
  "recommendations": ["...", "...", "..."]
}}""".format(
        n=len(games),
        time_class=time_class or "unknown time control",
        rating=rating_str,
        wins=wins,
        losses=losses,
        accuracy=stats.get("accuracy_pct", 0),
        active_blunders=active_blunders,
        miss_count=miss_count,
        blunders_per_game=stats.get("blunders_per_game", 0),
        mistakes=stats.get("mistakes", 0),
        phase_lines=phase_lines,
        weaknesses=weakness_lines,
        primary_failure=primary_failure,
        time_profile=time_profile,
    )

    try:
        resp = _with_retry(lambda c: c.models.generate_content(
            model=model,
            contents=prompt,
            config={"temperature": float(temperature), "response_mime_type": "application/json"},
        ))
        text = getattr(resp, "text", None) or "{}"
        return json.loads(text)
    except Exception as e:
        raise RuntimeError(f"Gemini error: {e}")


def _describe_move(fen: str, san: str) -> dict:
    """Pre-compute verified facts about ONE move in a position. Feeds the LLM
    grounded ground-truth so it doesn't hallucinate colors, captures, or attack
    squares. Same idea as _compute_chess_facts but scoped to a single move.

    Returns a dict with `summary` (one-line human-readable description) plus
    raw fields. `summary` is what we put into the prompt as a verified fact.
    """
    import chess as _chess
    NAMES = {
        _chess.PAWN: "pawn", _chess.KNIGHT: "knight", _chess.BISHOP: "bishop",
        _chess.ROOK: "rook", _chess.QUEEN: "queen", _chess.KING: "king",
    }
    try:
        board = _chess.Board(fen)
        mover_color = "White" if board.turn == _chess.WHITE else "Black"
        opp_color   = "Black" if board.turn == _chess.WHITE else "White"
        move = board.parse_san(san)
        from_sq = _chess.square_name(move.from_square)
        to_sq   = _chess.square_name(move.to_square)
        piece = board.piece_at(move.from_square)
        piece_name = NAMES.get(piece.piece_type, "?") if piece else "?"

        captured_desc = None
        if board.is_capture(move):
            # Handle en-passant separately — square is empty pre-push.
            if board.is_en_passant(move):
                captured_desc = f"{opp_color}'s pawn (en passant)"
            else:
                cap = board.piece_at(move.to_square)
                if cap:
                    captured_desc = f"{opp_color}'s {NAMES[cap.piece_type]} on {to_sq}"

        after = board.copy(); after.push(move)
        gives_check = after.is_check()
        is_mate     = after.is_checkmate()

        # Squares the moved piece now attacks, with the piece sitting on each.
        # This is what enables (and constrains) any "fork" claim.
        attacks_now = []
        for sq in after.attacks(move.to_square):
            tgt = after.piece_at(sq)
            if tgt and tgt.color != board.turn:
                # board.turn is still the mover here because we haven't called push on `board`
                # (we pushed on `after`). The mover is `mover_color`; targets are opp_color.
                attacks_now.append(f"{opp_color}'s {NAMES[tgt.piece_type]} on {_chess.square_name(sq)}")

        # Build the human summary in a single deterministic sentence.
        parts = [f"{san} = {mover_color} {piece_name} {from_sq}→{to_sq}"]
        if captured_desc:
            parts.append(f"captures {captured_desc}")
        if is_mate:
            parts.append("delivering checkmate")
        elif gives_check:
            parts.append("with check")
        # Non-king targets after the move — what the piece now threatens.
        non_king_attacks = [a for a in attacks_now if " king " not in (" " + a + " ").lower()]
        if non_king_attacks:
            parts.append(f"and now attacks {', '.join(non_king_attacks)}")
        summary = "; ".join(parts)

        return {
            "summary": summary,
            "mover_color": mover_color,
            "piece": piece_name,
            "from": from_sq,
            "to": to_sq,
            "captures": captured_desc,
            "gives_check": gives_check,
            "is_mate": is_mate,
            "attacks_now": attacks_now,
        }
    except Exception as e:
        # If parsing fails for any reason, return the SAN as-is so the LLM at
        # least sees something — but log so we can diagnose.
        logger.warning("describe_move failed for %s in %s: %s", san, fen, e)
        return {"summary": f"{san} (could not parse)", "error": str(e)}


def explain_drill_batch(items: list, model: Optional[str] = None, temperature: float = 0.4) -> list:
    """Generate threat + correction explanations for a batch of drill questions in ONE call.

    Designed to keep Gemini quota usage minimal — instead of N calls for N questions,
    pack them all into a single prompt asking for N JSON outputs in order.

    Input: list of dicts, each:
      { fen_after, played_san, opponent_best_san, correction_fen, correction_best_san, motif }

    Output: list of dicts (SAME LENGTH, SAME ORDER as input), each:
      { threat_explanation: str, correction_explanation: str }
    """
    if not items:
        return []
    # Drill batch defaults to Pro 3.1 — it's the most faithful at "use ONLY the
    # verified facts" instruction-following. Pro's lower daily quota is OK here
    # because explain_drill_batch runs once per Drill session and results cache
    # forever in localStorage. Override with GEMINI_MODEL_DRILL to test others.
    model = model or os.getenv("GEMINI_MODEL_DRILL", "gemini-3.1-pro-preview")

    # Pre-compute python-chess-verified facts for each item so the LLM never has
    # to derive piece colors, capture targets, or attack squares from the FEN.
    # In addition to the single-move facts we already had, also feed:
    #   - the FULL list of legal moves in each relevant position (move universe bound)
    #   - every CURRENT attack relationship (so the LLM doesn't invent forks/pins)
    sections = []
    for i, it in enumerate(items, 1):
        fen_after = it.get('fen_after', '')
        correction_fen = it.get('correction_fen', '')
        threat_facts = _describe_move(fen_after, it.get('opponent_best_san', ''))
        correction_facts = None
        if correction_fen and it.get('correction_best_san'):
            correction_facts = _describe_move(correction_fen, it['correction_best_san'])

        # Also describe what the PLAYER just played — context for understanding
        # what threat the correction is trying to neutralize.
        played_facts = None
        if correction_fen and it.get('played_san'):
            played_facts = _describe_move(correction_fen, it['played_san'])

        legal_after = ", ".join(_legal_sans(fen_after)) or "(none)"
        legal_correction = ", ".join(_legal_sans(correction_fen)) if correction_fen else ""
        attacks_after = _current_attacks(fen_after)
        attacks_correction = _current_attacks(correction_fen) if correction_fen else []

        section = (
            f"#{i}:\n"
            f"  Player previously played: {played_facts['summary'] if played_facts else it.get('played_san','?')}\n"
            f"  THREAT — Opponent's best reply (verified): {threat_facts['summary']}\n"
            f"  CORRECTION — Player's best alternative (verified): "
            f"{correction_facts['summary'] if correction_facts else '(none)'}\n"
            f"  Tactical motif (engine-detected): {it.get('motif') or 'none'}\n"
            f"  All legal moves in the THREAT position (opponent to move): {legal_after}\n"
            f"  All existing attacks in the THREAT position:\n    "
            + ("\n    ".join(attacks_after) if attacks_after else "(no piece is currently attacked)")
        )
        if correction_fen:
            section += (
                f"\n  All legal moves in the CORRECTION position (player to move): {legal_correction}\n"
                f"  All existing attacks in the CORRECTION position:\n    "
                + ("\n    ".join(attacks_correction) if attacks_correction else "(no piece is currently attacked)")
            )
        sections.append(section)

    prompt = (
        "You are a chess coach explaining drill positions. Every fact below was "
        "computed by python-chess from the actual board state and is CORRECT. "
        "Your job is to write engaging coaching prose grounded in those facts.\n\n"
        "STRICT RULES — violations make the explanation wrong:\n"
        "- Use ONLY the pieces, squares, captures, and attacks listed in the "
        "verified facts. The color of each piece is stated explicitly — never "
        "claim a piece attacks another piece of its OWN color (that's impossible).\n"
        "- Any chess move you reference by name (e.g. Nf3, Bxe7, Qd4+) MUST appear "
        "in the 'All legal moves' list for the relevant position. If a move "
        "isn't on that list, the move does not exist and you cannot mention it.\n"
        "- Any attack you reference MUST appear in the 'All existing attacks' "
        "list. Do not invent attack relationships between pieces.\n"
        "- If the verified facts don't mention a fork, double attack, pin, or "
        "particular target piece — don't invent one. A knight on d6 only reaches "
        "the 8 squares the verified facts say it attacks; don't claim it forks "
        "anything else.\n"
        "- Stick to what the verified facts state. If they say the knight "
        "captures a pawn on d6, don't promote that to 'captures the bishop' "
        "or 'forks the queen.'\n\n"
        "For each numbered position write TWO short explanations:\n\n"
        "1. threat_explanation (1-2 sentences): WHY the opponent's best reply "
        "works — what it captures, what it threatens next. Stay grounded in the "
        "THREAT verified fact.\n\n"
        "2. correction_explanation (1-2 sentences): WHY the player's best "
        "alternative move is better than what they played — what it captures, "
        "defends, or neutralizes about the threat. Stay grounded in the "
        "CORRECTION verified fact.\n\n"
        "POSITIONS:\n"
        + "\n\n".join(sections)
        + "\n\nReturn the explanations in the SAME ORDER as the numbered positions above. "
        "Respond with JSON ONLY (no markdown fences):\n"
        "{\n"
        '  "explanations": [\n'
        '    {"threat_explanation": "...", "correction_explanation": "..."},\n'
        "    ...\n"
        "  ]\n"
        "}"
    )

    try:
        resp = _with_retry(lambda c: c.models.generate_content(
            model=model,
            contents=prompt,
            config={"temperature": float(temperature), "response_mime_type": "application/json"},
        ))
        text = getattr(resp, "text", None) or "{}"
        result = json.loads(text)
        explanations = result.get("explanations", [])
        # Pad/truncate to match input length — defensive in case Gemini returns wrong count.
        while len(explanations) < len(items):
            explanations.append({"threat_explanation": "", "correction_explanation": ""})
        explanations = explanations[:len(items)]

        # Post-hoc fact-check: scan each explanation for chess-move-shaped tokens
        # and verify every move is actually legal in one of the relevant positions.
        # If any cited move is illegal/impossible, fall back to the mechanical
        # description for that field. We never reject silently — the user just
        # sees the verified description instead of LLM polish.
        for i, expl in enumerate(explanations):
            it = items[i]
            fen_after = it.get('fen_after')
            correction_fen = it.get('correction_fen')
            fens = [fen_after, correction_fen]
            if expl.get('threat_explanation') and not _validate_sans_legal(expl['threat_explanation'], fens):
                expl['threat_explanation'] = _describe_move(fen_after, it.get('opponent_best_san', ''))['summary']
            if expl.get('correction_explanation') and correction_fen and not _validate_sans_legal(expl['correction_explanation'], fens):
                expl['correction_explanation'] = _describe_move(correction_fen, it.get('correction_best_san', ''))['summary']

        return explanations
    except Exception as e:
        raise RuntimeError(f"Gemini error: {e}")


def clarify_move(summary_json: Dict[str, Any], question: str, model: Optional[str] = None, temperature: float = 0.4) -> str:
    """Ask Gemini to clarify the previously returned summary.

    Returns plain text suitable for inline display.
    """
    model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    prompt = (
        "You are an encouraging chess coach. A student has a follow-up question about your move explanation.\n"
        "Use the summary below as your context. You may draw on general chess knowledge to answer clearly,\n"
        "but do not invent new engine lines beyond the moves already mentioned in the summary.\n\n"
        f"PREVIOUS EXPLANATION:\n{json.dumps(summary_json, indent=2, ensure_ascii=False)}\n\n"
        f"STUDENT QUESTION: {question}\n\n"
        "Answer in 2-4 plain-English sentences. Be specific — name pieces, squares, and ideas."
    )

    try:
        resp = _with_retry(lambda c: c.models.generate_content(
            model=model,
            contents=prompt,
            config={
                "temperature": float(temperature),
                "response_mime_type": "text/plain",
            },
        ))
        text = getattr(resp, "text", None) or ""
        return text.strip()
    except Exception as e:
        raise RuntimeError(f"Gemini error: {e}")

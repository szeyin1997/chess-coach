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

                # Discovered attack: a piece OTHER than the mover now attacks a player piece
                discovered_targets = []
                for sq in _chess.SQUARES:
                    piece = board_copy.piece_at(sq)
                    if piece and piece.color == player_color:
                        if board_copy.is_attacked_by(not player_color, sq):
                            # Check if the attacker is NOT the piece that just moved
                            attackers = board_copy.attackers(not player_color, sq)
                            non_mover_attackers = [a for a in attackers if a != opp_move.to_square]
                            if non_mover_attackers:
                                discovered_targets.append(
                                    f"{NAMES[piece.piece_type]} on {_chess.square_name(sq)}"
                                )

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

                opponent_reply_desc = (
                    f"{opponent_reply_san} " + " and ".join(effects)
                    if effects else f"{opponent_reply_san} seizes a strong position"
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


def summarize_move(data: Dict[str, Any], model: Optional[str] = None, temperature: float = 0.4) -> Dict[str, Any]:
    """Call Gemini to explain a move.

    All chess facts (hanging pieces, opponent reply, captures) are pre-computed
    by python-chess. Gemini is only asked to explain the principle and coach.
    """
    model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    from helper_functions import classify_move

    eval_before = data.get('eval_before_cp', 0) or 0
    eval_after  = data.get('eval_after_cp',  0) or 0
    best_eval   = data.get('best_eval_cp')
    delta_cp    = eval_after - eval_before  # cp delta from player's POV

    # Single source of truth — same unified classifier every endpoint uses.
    classification = classify_move(eval_before, eval_after, best_eval)

    # Severity string given to Gemini. Includes both cp and win% so Gemini can
    # use whichever framing makes more sense for the position (drastic material
    # loss vs. squandered winning chances).
    severity = "{lbl} (cp delta {dcp:+d}; winning chance {wb:.0f}% → {wa:.0f}%)".format(
        lbl=classification["label"],
        dcp=int(delta_cp),
        wb=classification["win_before"],
        wa=classification["win_after"],
    )

    # Frame the explanation based on which signal(s) fired. Because classify_move
    # now takes the WORSE of cp-delta and win%-delta, created_problem fires whenever
    # the move actually worsened the position — including drastic cp drops in
    # already-losing positions (the bug that made /summarize say "good move" on
    # a Mistake). missed_opportunity is orthogonal — a clearly better move existed.
    if classification["missed_opportunity"] and classification["created_problem"]:
        framing = (
            "FRAMING: This move BOTH missed a winning opportunity AND worsened the position. "
            "Lead with the missed idea (what they could have done), then explain how their move backfired."
        )
    elif classification["missed_opportunity"]:
        framing = (
            "FRAMING: This is a MISS — the move itself wasn't terrible, but a much stronger "
            "winning move was available. Focus the explanation on the missed opportunity, NOT on punishment."
        )
    elif classification["created_problem"]:
        framing = (
            f"FRAMING: This move is a {classification['severity']}. Explain WHY it's bad: "
            "what did the opponent's best reply exploit, and what should the player have "
            "played instead. Do NOT call this a good move and do NOT deflect the lesson to "
            "an earlier move — the user clicked this move specifically and expects a concrete "
            "explanation of what went wrong here."
        )
    else:
        framing = (
            "FRAMING: This move barely changed the position. Don't overstate — if anything, "
            "the lesson is from an earlier move, not this one."
        )

    relevant_principles = principles_for_move(data)

    # Pre-compute all chess facts with python-chess — Gemini gets verified statements only
    try:
        facts = _compute_chess_facts(
            fen_before    = data.get('fen', ''),
            player_san    = data.get('san', ''),
            pv_played_san = data.get('pv_played_san', ''),
            best_san      = data.get('best_san', ''),
        )
    except Exception as e:
        raise RuntimeError(f"Chess analysis error: {e}")

    player_color   = facts["player_color"]
    opponent_color = facts["opponent_color"]
    motif          = facts.get("motif")

    # Look up the pre-written tactical pattern description
    from chess_principles import get_tactical_pattern
    pattern = get_tactical_pattern(motif) if motif else None
    pattern_block = (
        f"TACTICAL PATTERN DETECTED: {pattern['name']}\n"
        f"Definition: {pattern['description']}"
        if pattern else ""
    )

    # Phrased to make it clear these are pieces the MOVE created problems for,
    # not pieces that were attacked the whole time. Empty list means the move
    # itself didn't expose any of your pieces — don't say "X is under attack" if
    # that attack predates this move.
    hanging_line = (
        "  Pieces this move left genuinely hanging (newly attacked AND under-defended): "
        + ", ".join(facts["hanging_pieces"])
        if facts["hanging_pieces"] else
        "  This move did not leave any of the player's pieces newly hanging"
    )
    reply_line = (
        f"  Opponent's best reply (verified): {facts['opponent_reply_desc']}"
        if facts["opponent_reply_desc"] else ""
    )
    best_line = f"  Best move (verified): {facts['best_move_desc']}"

    prompt = """\
You are an encouraging chess coach. All chess facts below were computed by a chess engine
and python-chess — they are CORRECT. Do not recalculate or second-guess them.

{framing}

{pattern_block}

VERIFIED FACTS:
  Player        : {player_color}
  Move played   : {san}  [{severity}]
{hanging_line}
{reply_line}
{best_line}
  Engine best   : {best_san}  (eval {best_eval} cp)
  PV after best : {pv_best}

YOUR TASK — write the coaching explanation that respects the FRAMING above:
1. "summary": 1-2 sentences.
   - If FRAMING says MISS: lead with "You had a chance to..." — name the missed idea.
   - If FRAMING says created a problem: explain what the opponent's reply exploits.
   - If FRAMING says both: lead with what they could have done, then how their move backfired.
   - If FRAMING says barely changed: say so honestly — don't manufacture a lesson.
   - Use ONLY squares, pieces, and moves from the VERIFIED FACTS. Do not invent any.
2. "reasons": 1-2 bullets naming the chess principle violated (or missed opportunity).
3. "what_next": 1-2 actionable tips the player should apply next time.
4. "if_bad_fix": explain why {best_san} is better.
   Set to null if the move barely changed winning chances.

Respond ONLY with JSON (no markdown fences):
{{
  "verdict": "good|ok|bad",
  "summary": "...",
  "reasons": ["...", "..."],
  "what_next": ["...", "..."],
  "if_bad_fix": {{
     "missed_idea": "...",
     "best_move": "{best_san}",
     "why_best": "..."
  }}
}}""".format(
        framing=framing,
        pattern_block=pattern_block,
        player_color=player_color,
        san=data.get('san', '?'),
        severity=severity,
        hanging_line=hanging_line,
        reply_line=reply_line,
        best_line=best_line,
        best_san=data.get('best_san', '?'),
        best_eval=data.get('best_eval_cp', '?'),
        pv_best=data.get('pv_best_san', '(none)'),
    )

    try:
        resp = _with_retry(lambda c: c.models.generate_content(
            model=model,
            contents=prompt,
            config={"temperature": float(temperature), "response_mime_type": "application/json"},
        ))
        text = getattr(resp, "text", None) or getattr(resp, "candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text")
        return json.loads(text or "{}")
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
    """Generate a high-level player profile: top improvement lever + strengths vs peers.

    Returns:
      { improvement_lever, strengths, elo_context }
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

    prompt = """\
You are an experienced chess coach writing a targeted improvement report for one specific player.

PLAYER DATA (last {n} games, {time_class}, rating {rating}):
  Result         : {wins}W / {losses}L
  Accuracy       : {accuracy}% good moves
  Active blunders: {active_blunders} (moves that made position worse)
  Missed wins    : {miss_count} (had a winning move but didn't play it)
  Blunders/game  : {blunders_per_game}
  Mistakes       : {mistakes}

WHERE ERRORS OCCUR (by game phase):
{phase_lines}

SPECIFIC RECURRING WEAKNESSES (ranked by frequency across {n} games):
{weaknesses}

TASK — respond with JSON only (no markdown fences):

1. "improvement_lever": 2–3 sentences. The single most impactful thing to fix.
   RULES:
   - Base it on the #1 weakness above and the phase where errors cluster most.
   - Be specific to THIS player's data — name the pattern, when it happens, and what to do instead.
   - DO NOT write generic advice like "check for hanging pieces before every move" unless 'hanging pieces'
     is genuinely the #1 weakness AND the phase data supports it. If the #1 weakness is positional drift,
     missed tactics in the middlegame, or opening mistakes — say THAT specifically.
   - A 500-rated player and a 800-rated player will have different root causes even if both blunder.
     At 800, the issue is often calculation depth and missing 2-move combinations, not just one-move blunders.
   - Write as: "In your games, [specific recurring situation]. When this happens, [what goes wrong].
     Next time, [specific habit to build]."

2. "strengths": list of 2–3 short strings (≤ 12 words each).
   Compare to typical {rating}-rated {time_class} players. Base on actual data only.

3. "elo_context": 1–2 sentences comparing this player's blunder rate and accuracy to a typical
   {rating}-rated player. Be honest and calibrated.

{{
  "improvement_lever": "...",
  "strengths": ["...", "...", "..."],
  "elo_context": "..."
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

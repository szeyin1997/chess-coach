"""
Helper functions for the Chess Coach project.

This module centralizes engine setup and chess utilities so both the CLI
and the FastAPI backend can share identical behavior.

Notes
- Stockfish path, depths, and thresholds live in config.py.
- Keep functions pure where possible; callers manage board state and I/O.
"""

# coach.py
# A beginner-friendly “LLM-style” coach:
# - You play moves (like e4, Nf3, or e2e4)
# - The coach checks your move with Stockfish (truth tool)
# - It labels the move (Good/Inaccuracy/Mistake/Blunder) and offers a short hint
# - It plays back a human-ish reply (still not perfect), so you can keep playing

import sys
import math
import random
import chess
from typing import Optional
import chess.engine

from config import (
    STOCKFISH_PATH,
    ENGINE_DEPTH_ANALYZE,
    ENGINE_DEPTH_REPLY,
    BLUNDER_THRESHOLDS,
)

# ⚙️ Tunables (start with these) — defined in config.py

# “Depth” = how many plies (half-moves) Stockfish looks ahead.
# Higher depth → stronger, but slower.
# Depth 10 means Stockfish looks ~10 half-moves into the future (≈ 5 full turns).
# Used after your own move to judge your move's quality (ENGINE_DEPTH_ANALYZE)
# and to gauge/decide the coach's move (ENGINE_DEPTH_REPLY).

# cp = centipawn, where 100 = value of 1 pawn
# BLUNDER_THRESHOLDS represent loss thresholds as negative deltas (after - before):
# < 50 cp loss   → Good      (delta ≥ -50)
# 50–100 cp loss → Inaccuracy (delta ≥ -100)
# 100–300 cp loss→ Mistake    (delta ≥ -300)
# 300+ cp loss   → Blunder    (delta < -300)

# Win% at/above which the player is still considered "clearly winning" after
# their move. A missed winning chance that leaves the player here is a 'Missed
# Win', not a Blunder (~+230cp on the Lichess sigmoid). Below it, declining the
# win actually threw the game into the balance, so it stays a real mistake.
WINNING_AFTER_THRESHOLD = 70.0

# best_eval at/above this is a forced-mate SENTINEL (best_line scores mate as
# ~100000), not real material — no legitimate material edge approaches +90 pawns.
# Used to detect "a forced mate was available" independently of win%, which
# saturates near 100% and hides declined mates from the win-gap test.
MATE_CP_SENTINEL = 9000

# 'Miss' fires only when the best move was a CONCRETE winning shot the player
# passed up — a forced mate or a capture that wins material. These bound the
# capture case (see docs/superpowers/specs/2026-06-04-miss-redefinition-design.md):
MISS_MATERIAL_MIN = 2   # a winning capture must net >= a minor piece (a free pawn isn't a Miss)
MISS_MARGIN = 100       # the played move must be >=100cp worse than best (i.e. you didn't play the shot)

# Win% loss (percentage points, 0–100 scale) below which a Good move is labelled
# "Excellent" — mirrors Chess.com's Expected Points Model threshold of 0.02 EP.
# Moves at or above this threshold (but still Good severity) are labelled "Good".
EXCELLENT_WIN_LOSS_THRESHOLD = 2.0

_PIECE_VALUE = {
    chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
    chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 100,
}


def best_is_winning_shot(board: "chess.Board", best_san: Optional[str], best_eval_cp: Optional[int]) -> bool:
    """Is the engine's best move a CONCRETE winning shot — a forced mate, or a
    capture that nets material? `board` is the position BEFORE the move (player to
    move).

    This is what makes 'Miss' mean "you passed up a knockout" rather than "you
    let an edge slip": a quiet/defensive best move (e.g. Qc5 just defending a
    piece) or an even trade (e.g. Qxe3+ into a defended queen) returns False. The
    classifier can't tell these apart from eval numbers alone — it needs the move
    and the board, which is why this is computed by callers and passed in.
    """
    # Forced mate available (best_line scores mate as the ~100000 sentinel).
    if best_eval_cp is not None and best_eval_cp >= MATE_CP_SENTINEL:
        return True
    if not best_san:
        return False
    try:
        mv = board.parse_san(best_san)
    except Exception:
        return False
    if not board.is_capture(mv):
        return False
    # Value of the captured piece (en passant takes a pawn off a different square).
    if board.is_en_passant(mv):
        captured_val = _PIECE_VALUE[chess.PAWN]
    else:
        cap = board.piece_at(mv.to_square)
        captured_val = _PIECE_VALUE.get(cap.piece_type, 0) if cap else 0
    capturer_val = _PIECE_VALUE.get(board.piece_at(mv.from_square).piece_type, 0)
    # Simplified SEE: if the opponent can recapture on the target square, we net
    # captured - capturer; if it's undefended, we keep the whole captured value.
    after = board.copy()
    after.push(mv)
    can_recapture = bool(after.attackers(after.turn, mv.to_square))
    net = (captured_val - capturer_val) if can_recapture else captured_val
    return net >= MISS_MATERIAL_MIN


def open_engine():
    """Open a Stockfish engine process via UCI.

    Relies on `config.STOCKFISH_PATH`. If the binary is not found, prints
    a helpful message and exits the process.
    """
    try:
        return chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
    except FileNotFoundError:
        # Opens stockfish engine (program) to kickoff analysis
        print("❌ Can't find Stockfish. Edit STOCKFISH_PATH in config.py.")
        sys.exit(1)


def eval_cp(engine, board, pov_color, depth: Optional[int] = None):
    """Return centipawns from POV of pov_color (positive = good for pov_color).

    Optional depth overrides the default ANALYZE depth for quicker calls.
    """
    # Calls stockfish and tells it to analyze the board position up to the depth
    use_depth = ENGINE_DEPTH_ANALYZE if depth is None else int(depth)
    info = engine.analyse(board, chess.engine.Limit(depth=use_depth))

    # Extract score object from the player's perspective. If mate, use large value instead of None
    # Value of score object is centipawn score (cp) or mate score
    score = info["score"].pov(pov_color).score(mate_score=100000)
    return int(score if score is not None else 0)


_SEVERITY_RANK = {"Good": 0, "Inaccuracy": 1, "Mistake": 2, "Blunder": 3}


def parse_time_control(tc: Optional[str]) -> tuple:
    """Parse a PGN TimeControl header into (base_seconds, increment_seconds).

    Handles '600', '600+5', '180+2'. Returns (None, None) for unknown, '-', '?',
    or correspondence ('1/86400') — none of which represent real-time pressure."""
    if not tc or tc in ("-", "?"):
        return (None, None)
    try:
        if "/" in tc:  # correspondence (e.g. '1/86400') — not real-time, no pressure
            return (None, None)
        if "+" in tc:
            base, inc = tc.split("+", 1)
            return (int(base), int(inc))
        return (int(tc), 0)
    except Exception:
        return (None, None)


def under_time_pressure(clock_after_s: Optional[float], base_s: Optional[int]) -> Optional[bool]:
    """Was the player in time pressure when they made this move?

    Returns True/False when a clock reading exists, None when clock data is absent
    (older PGNs / some imports) — so callers never *claim* time pressure they can't see.

    Threshold: the clock dropped below max(10% of base time, 15s). The 15s floor
    keeps very short controls (bullet) from labelling almost nothing as pressure.
    With no base time known, falls back to an absolute 30s."""
    if clock_after_s is None:
        return None
    threshold = max(0.10 * base_s, 15.0) if base_s else 30.0
    return clock_after_s < threshold


def win_percent(cp: int) -> float:
    """Convert centipawns to win probability (0-100), from the player's POV.

    Uses Lichess's sigmoid mapping (slope ~0.00368). Mate scores clamp to 0/100.
    Why this matters: a 100cp drop from +500 barely changes winning chances
    (~93%→89%), but a 100cp drop from 0 is huge (50%→41%). Linear cp deltas
    misjudge both cases; win% handles them naturally.
    """
    if cp >= 10000:
        return 100.0
    if cp <= -10000:
        return 0.0
    return 100.0 / (1.0 + math.exp(-0.00368208 * cp))


def _severity_from_cp_delta(delta_cp: int, threshold_factor: float = 1.0) -> str:
    """Pure cp-delta severity (positional-context-blind). Catches drastic material
    losses regardless of whether the player was already winning/losing.

    threshold_factor scales the Good/Inaccuracy and Inaccuracy/Mistake boundaries
    for rating-awareness (see _rating_leniency_factor). The Mistake/Blunder
    boundary (c = -300) is deliberately NOT scaled: a move that drops >=300cp is a
    Blunder for everyone, so a beginner's hung piece is never softened into a
    lesser label. Leniency only adjusts the gray zone between 'fine' and 'real
    mistake', never whether a material drop counts as a blunder."""
    a, b, c = BLUNDER_THRESHOLDS  # -50, -100, -300
    a *= threshold_factor
    b *= threshold_factor
    if delta_cp >= a:
        return "Good"
    if delta_cp >= b:
        return "Inaccuracy"
    if delta_cp >= c:
        return "Mistake"
    return "Blunder"


def _severity_from_win_loss(win_loss: float, threshold_factor: float = 1.0) -> str:
    """Lichess-style win%-delta severity. Catches 'squandered a winning position'
    where a small cp drop crosses a key 50%/win threshold.

    threshold_factor scales the cutoffs for rating-awareness (see
    _rating_leniency_factor). 1.0 == Lichess-standard 10/20/30. A factor >1
    loosens (beginner), <1 tightens (advanced)."""
    if win_loss >= 30 * threshold_factor:
        return "Blunder"
    if win_loss >= 20 * threshold_factor:
        return "Mistake"
    if win_loss >= 10 * threshold_factor:
        return "Inaccuracy"
    return "Good"


def _rating_leniency_factor(rating: Optional[int]) -> float:
    """Rating-aware scaling for the win%-delta thresholds.

    Research basis: Chess.com's move classifier uses a rating-dependent
    "Expected Points Model" — the engine eval that counts as winning/equal/losing
    shifts with the player's rating, because weaker players neither convert
    advantages nor punish opponent mistakes reliably. So a given swing in winning
    chances is objectively less decisive for a beginner than for a strong player.
    We mirror that by LOOSENING the win%-delta thresholds below a pivot rating and
    tightening them above it.

    Factor 1.0 == today's Lichess-standard thresholds (10/20/30):
      - rating None  -> 1.0   (unchanged — fully backward compatible)
      - rating  800  -> 1.2   (12/24/36 — a beginner's positional swings flagged less harshly)
      - rating 1200  -> 1.0
      - rating 2000+ -> 0.8   (8/16/24 — stronger players held to a stricter bar)

    SAFETY: the factor scales the lower boundaries of BOTH arms (the Good/
    Inaccuracy and Inaccuracy/Mistake lines), but the cp-delta Mistake/Blunder
    floor (-300) is NEVER scaled. So a hung piece / material drop (>=300cp) is
    ALWAYS a Blunder regardless of rating: leniency only adjusts whether a
    moderate slip reads as Inaccuracy vs Mistake — it can never soften "dropped a
    rook" into a non-blunder. That preserves the hung-piece / CCT lesson for
    beginners while cutting positional-noise badges that don't matter at their level.
    """
    if rating is None:
        return 1.0
    pivot = 1200
    factor = 1.0 + (pivot - rating) / 2000.0
    return max(0.8, min(1.5, factor))


def classify_move(eval_before_cp: int, eval_after_cp: int, best_eval_cp: Optional[int] = None,
                  rating: Optional[int] = None, best_is_winning_shot: Optional[bool] = None,
                  played_best: bool = False) -> dict:
    """Single source of truth for move severity, used by every endpoint.

    Combines two methods and TAKES THE WORSE verdict:
      - cp-delta:  catches drastic material/positional losses regardless of context
                   (a 300cp drop is at least a Mistake even if you were already winning)
      - win%-delta: catches squandered-winning-position cases (a 60cp drop that flips
                   the game from winning to drawing is at least an Inaccuracy)

    Why both: each method has a known failure mode the other covers.
      - Pure cp-delta over-penalizes drops in already-decided positions.
      - Pure win%-delta under-classifies cp drops when you're already very losing
        or very winning (sigmoid saturates, so big cp moves look small in win%).

    `rating` (optional): the moving player's rating. When supplied, the win%-delta
    thresholds are scaled by _rating_leniency_factor so badges are judged relative
    to the player's level — gentler for beginners, stricter for strong players —
    mirroring Chess.com's rating-dependent classifier. rating=None reproduces the
    old rating-blind behavior exactly (so existing callers are unaffected). Only the
    win% arm scales; cp-delta stays rating-blind, so material drops are always caught.

    All eval values are in centipawns from the player's POV (positive = good for them).
    """
    delta_cp   = eval_after_cp - eval_before_cp
    win_before = win_percent(eval_before_cp)
    win_after  = win_percent(eval_after_cp)
    win_loss   = win_before - win_after  # positive = player got worse

    rating_factor = _rating_leniency_factor(rating)
    cp_severity   = _severity_from_cp_delta(delta_cp, rating_factor)
    win_severity  = _severity_from_win_loss(win_loss, rating_factor)

    # Worst-of-both: the move can never be softened by either method alone.
    severity = max(cp_severity, win_severity, key=lambda s: _SEVERITY_RANK[s])
    created_problem = severity != "Good"

    # 'Miss' = the player passed up a CONCRETE winning shot they didn't play.
    # Two detectors, because the trigger info lives in different places:
    #   - DECLINED MATE is visible from evals alone (best_eval is the mate
    #     sentinel, eval_after is not), so it works for eval-only callers like the
    #     classification eval harness. DON'T REMOVE — see CLAUDE.md "Declined-mate
    #     detection". (win% can't catch it: mate 100% vs still-winning ~91% is <15%.)
    #   - a material-winning CAPTURE needs the move + board, so the caller computes
    #     best_is_winning_shot() and passes it. A quiet/defensive best move (Qc5)
    #     or an even trade is NOT a shot — that's what removed the move-19 false Miss.
    # The old broad win%-gap trigger is gone: it fired on ANY unplayed winning move.
    win_best = win_percent(best_eval_cp) if best_eval_cp is not None else None
    missed_opportunity = False
    if best_eval_cp is not None and (best_eval_cp - eval_after_cp) >= MISS_MARGIN:
        declined_mate = best_eval_cp >= MATE_CP_SENTINEL and eval_after_cp < MATE_CP_SENTINEL
        missed_opportunity = bool(best_is_winning_shot) or declined_mate

    # 'Missed Win': you declined a winning chance (often a forced mate) but the
    # move you played leaves you STILL clearly winning. The cp-delta arm brands
    # these as Blunder/Mistake only because a forced mate is stored as the 100000
    # sentinel — subtracting it produces a ~-100000 "drop" that has nothing to do
    # with how much the position actually worsened. In practical (win%) terms you
    # barely moved. So when the player stays winning we discard the sentinel-
    # inflated cp arm, take the honest win% severity, and surface it as its own
    # category instead of double-counting it as a Blunder. A move that throws the
    # win away (drops below the winning bar) is NOT a missed win — it stays a real
    # Blunder/Mistake. See CLAUDE.md and RESEARCH.md (mirrors Chess.com's "Miss").
    missed_win = missed_opportunity and win_after >= WINNING_AFTER_THRESHOLD
    if missed_win:
        severity = win_severity            # sentinel-inflated cp arm discarded
        created_problem = severity != "Good"

    if missed_win:
        label = "Missed Win"
    elif missed_opportunity and created_problem:
        label = f"{severity} + Miss"
    elif missed_opportunity:
        label = "Miss"
    elif severity == "Good":
        if played_best:
            label = "Best"
        elif win_loss < EXCELLENT_WIN_LOSS_THRESHOLD:
            label = "Excellent"
        else:
            label = "Good"
    else:
        label = severity

    return {
        "label": label,
        "severity": severity,
        "cp_severity":  cp_severity,
        "win_severity": win_severity,
        "missed_opportunity": missed_opportunity,
        "missed_win": missed_win,
        "created_problem": created_problem,
        "win_before": round(win_before, 1),
        "win_after":  round(win_after, 1),
        "win_best":   round(win_best, 1) if win_best is not None else None,
        "win_loss":   round(win_loss, 1),
        "cp_delta":   delta_cp,
        "rating":         rating,
        "rating_factor":  round(rating_factor, 2),
    }


def best_line(engine, board, depth=ENGINE_DEPTH_ANALYZE, multipv=3, plies=6):
    """Return a few candidate lines with scores from side-to-move POV."""
    # Call Stockfish and inform it to analyze the board position up to the depth and return multiple best lines
    info = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=multipv)
    infos = info if isinstance(info, list) else [info]
    lines = []
    for inf in infos:
        if "pv" not in inf or not inf["pv"]:
            continue
        # For each best line, extract the top plies moves and the score for the upcoming player's
        line_moves = inf["pv"][:plies]
        score_cp = inf["score"].pov(board.turn).score(mate_score=100000)
        lines.append((line_moves, int(score_cp) if score_cp is not None else 0))
    return lines


def san_line(board, moves):
    """Takes output in UCI from stockfish into SAN format for human readability"""
    tmp = board.copy()
    res = []
    for m in moves:
        res.append(tmp.san(m))
        tmp.push(m)
    return " ".join(res)


def humanish_reply(
    engine,
    board,
    target_elo: Optional[int] = None,
    target_skill: Optional[int] = None,
):
    """Pick among top-N engine moves to feel less robotic.

    Preference order for limiting strength during reply selection:
    1) Stockfish "Skill Level" (0–20) if available and `target_skill` provided
    2) Generic UCI_Elo via UCI_LimitStrength if available and `target_elo` provided

    The limitation is applied only while picking the reply, then restored.
    """
    did_limit_elo = False
    did_limit_skill = False
    prev_skill_value = None
    try:
        # Prefer explicit Skill Level if requested and supported by the engine
        if target_skill is not None and "Skill Level" in engine.options:
            # Clamp to engine-supported range
            opt = engine.options["Skill Level"]
            try:
                min_skill = int(getattr(opt, "min", 0))
                max_skill = int(getattr(opt, "max", 20))
            except Exception:
                min_skill, max_skill = 0, 20
            eff_skill = max(min_skill, min(int(target_skill), max_skill))
            # Best-effort: restore to default after
            try:
                prev_skill_value = int(getattr(opt, "default", 20))
            except Exception:
                prev_skill_value = 20
            engine.configure({"Skill Level": eff_skill})
            did_limit_skill = True
        # Otherwise fall back to Elo limiting if available
        elif target_elo is not None and "UCI_LimitStrength" in engine.options and "UCI_Elo" in engine.options:
            elo_opt = engine.options["UCI_Elo"]
            try:
                min_elo = int(getattr(elo_opt, "min", 0))
                max_elo = int(getattr(elo_opt, "max", 9999))
            except Exception:
                min_elo, max_elo = 0, 9999
            eff_elo = max(min_elo, min(int(target_elo), max_elo))
            engine.configure({"UCI_LimitStrength": True, "UCI_Elo": eff_elo})
            did_limit_elo = True

        lines = best_line(engine, board, depth=ENGINE_DEPTH_REPLY, multipv=3, plies=6)
    finally:
        # Restore engine options after reply selection
        if did_limit_skill:
            try:
                # Restore to previous/default value
                engine.configure({"Skill Level": prev_skill_value if prev_skill_value is not None else 20})
            except Exception:
                pass
        if did_limit_elo:
            try:
                engine.configure({"UCI_LimitStrength": False})
            except Exception:
                pass
    if not lines:
        # Fallback: If best_line() failed for some reason, just let Stockfish pick one move.
        move = engine.play(board, chess.engine.Limit(depth=ENGINE_DEPTH_REPLY)).move
        return move, "Play active and safe."
    # 75% of the time pick the top move, 25% of the time pick the second-best. choice_idx=0 = top move
    choice_idx = 0 if random.random() < 0.25 else min(1, len(lines) - 1)
    move = lines[choice_idx][0][0]
    # Tiny “plan” in natural words
    plan = "Improve piece activity and ensure king safety."
    return move, plan


def parse_user_move(board, txt):
    """This function lets the user type moves in either:
    SAN (e4, Nf3, Qxe5); UCI (e2e4, g1f3, d1e5)
    and safely converts them into a move the program can use.
    Also checks legality"""
    txt = txt.strip()
    # Try SAN first
    try:
        return board.parse_san(txt)
    except Exception:
        pass

    # Keep helpful normalizations while preserving the original behavior/comments:
    # - Allow lowercase piece letters like 'nf3' -> 'Nf3'
    if txt and txt[0] in "kqrbn":
        try:
            return board.parse_san(txt[0].upper() + txt[1:])
        except Exception:
            pass
    # - Allow castling variants commonly typed as o/0
    castle_map = {"o-o": "O-O", "0-0": "O-O", "o-o-o": "O-O-O", "0-0-0": "O-O-O"}
    if txt.lower() in castle_map:
        try:
            return board.parse_san(castle_map[txt.lower()])
        except Exception:
            pass

    # Try UCI
    try:
        move = chess.Move.from_uci(txt if len(txt) < 5 else txt.lower())
        return move if move in board.legal_moves else None
    except Exception:
        return None

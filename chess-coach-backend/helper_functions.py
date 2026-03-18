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


def label_delta(delta_cp):
    """Map centipawn delta to a friendly label."""
    # delta_cp = (after - before) from YOUR POV. Negative = got worse.
    a, b, c = BLUNDER_THRESHOLDS  # -50, -100, -300
    if delta_cp >= a:
        return "Good"
    if delta_cp >= b:
        return "Inaccuracy"
    if delta_cp >= c:
        return "Mistake"
    return "Blunder"


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

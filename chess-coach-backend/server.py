# server.py: bridge between your chess logic (Python + Stockfish + helper functions) and the UI (React or any other client).
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import chess
import time
import logging
import os
import threading
from pathlib import Path
from dotenv import load_dotenv

# Ensure .env is loaded when running without run-dev.sh (from backend folder)
_ENV_PATH = Path(__file__).with_name('.env')
# Allow .env to override any inherited env to avoid stale shells during dev
load_dotenv(dotenv_path=_ENV_PATH, override=True)

from helper_functions import (
    open_engine,
    eval_cp,
    classify_move,
    best_line,
    san_line,
    humanish_reply,
    parse_time_control,
    under_time_pressure,
)
try:
    from gemini_client import summarize_move, clarify_move, analyze_game  # prefer Gemini if available
    SUMMARIZER_PROVIDER = "gemini"
except Exception:  # fallback to OpenAI if Gemini is not importable
    try:
        from openai_client import summarize_move  # no clarify in OpenAI client
        SUMMARIZER_PROVIDER = "openai"
        analyze_game = None  # type: ignore
    except Exception:
        summarize_move = None  # type: ignore
        analyze_game = None  # type: ignore
        SUMMARIZER_PROVIDER = None  # type: ignore

# Best-effort check if the selected summarizer is configured (API key + SDK present).
SUMMARIZER_CONFIGURED = False
try:
    if SUMMARIZER_PROVIDER == "gemini":
        from gemini_client import get_gemini  # type: ignore
        try:
            _ = get_gemini()
            SUMMARIZER_CONFIGURED = True
        except Exception:
            SUMMARIZER_CONFIGURED = False
    elif SUMMARIZER_PROVIDER == "openai":
        from openai_client import get_openai  # type: ignore
        try:
            _ = get_openai()
            SUMMARIZER_CONFIGURED = True
        except Exception:
            SUMMARIZER_CONFIGURED = False
except Exception:
    SUMMARIZER_CONFIGURED = False

app = FastAPI()

@app.on_event("startup")
def _warm_puzzle_cache():
    try:
        from puzzle_db import _build_cache
        _build_cache()
    except Exception as e:
        logging.warning("Puzzle cache warm-up failed: %s", e)

app.add_middleware(
    CORSMiddleware,
    # Allow common dev origins (localhost and 127.0.0.1)
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ENGINE = open_engine()  # reuse one engine instance
ENGINE_LOCK = threading.Lock()  # Stockfish is not thread-safe — serialize all engine calls

# Accumulates moves for the current game so /analyze-game can review them
game_history: list[dict] = []

# Simple perf logger setup
logger = logging.getLogger("chess_coach.perf")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

def _log_duration(label: str, t0: float) -> None:
    try:
        dt_ms = (time.perf_counter() - t0) * 1000.0
        logger.info(f"[perf] {label}: {dt_ms:.1f} ms")
    except Exception:
        pass

# Stockfish Skill Level presets (1–6) with approximate Elo hints
# These labels are purely informational for the UI.
SKILL_PRESETS = {
    1: "<400",
    2: "500",
    3: "800",
    4: "1100",
    5: "1500",
    6: "1900",
}

# Fallback mapping for engines without Skill Level (approximate UCI_Elo)
SKILL_TO_ELO = {
    1: 350,
    2: 500,
    3: 800,
    4: 1100,
    5: 1500,
    6: 1900,
}


# Gives backend the current position and the move to be played
# Pydantic model you defined in your FastAPI server. 
# It describes the shape of the request body (the JSON data the frontend must send when it calls your API).
class PlayBody(BaseModel):
    fen: str                 # current position (FEN string)
    uci: str                 # e.g. "e2e4" or "e7e8q"
    promotion: Optional[str] = None
    # Use Stockfish Skill Level (1–6 as per UI). If absent, no limiting.
    skill: Optional[int] = None
    # If true, include OpenAI JSON summary in response (requires OPENAI_API_KEY)
    summary: Optional[bool] = False


# Gives backend the current position. E.g to ask for hints when a move hasn't been made
class FenBody(BaseModel):
    fen: str


# To check if server is running. If get request failed, it'll show error in console
@app.get("/")
def health():
    # Diagnostics without leaking secrets
    if SUMMARIZER_PROVIDER == "gemini":
        has_key = bool(os.getenv("GEMINI_API_KEY"))
        try:
            from google import genai  # type: ignore
            sdk_ok = True
        except Exception:
            sdk_ok = False
    elif SUMMARIZER_PROVIDER == "openai":
        has_key = bool(os.getenv("OPENAI_API_KEY"))
        try:
            import openai  # type: ignore
            sdk_ok = True
        except Exception:
            sdk_ok = False
    else:
        has_key = False
        sdk_ok = False

    return {
        "ok": True,
        "summarizer": SUMMARIZER_PROVIDER,
        "summarizer_configured": SUMMARIZER_CONFIGURED,
        "summarizer_has_key": has_key,
        "summarizer_sdk": sdk_ok,
        "env_loaded_from": str(_ENV_PATH),
    }

@app.get("/levels")
def levels():
    # Kept for backward-compatibility; now returns skill presets
    return {"skills": SKILL_PRESETS}


# Endpoint to get a hint (a short suggested line) from the current position
# JSON sent by frontend must match FenBody model
@app.post("/hint")
def hint(body: FenBody):
    board = chess.Board(body.fen)
    with ENGINE_LOCK:
        lines = best_line(ENGINE, board, plies=4)
    if not lines:
        if board.is_checkmate(): return {"idea": "Checkmate — no moves left!"}
        if board.is_stalemate(): return {"idea": "Stalemate — it’s a draw."}
        return {"idea": None}
    best_uci = lines[0][0][0].uci() if lines[0][0] else None
    return {"idea": san_line(board, lines[0][0]), "best_uci": best_uci}



# Main endpoint to play a move and get the coach's reply
@app.post("/play")
def play(body: PlayBody):
    t_all = time.perf_counter()
    #Reconstructs the board from the current FEN.
    board = chess.Board(body.fen)
    board_before = board.copy()

    # if promotion was sent separately, append it to UCI
    uci = body.uci if not body.promotion else (body.uci + body.promotion)
    try:
        user_move = chess.Move.from_uci(uci)
    except Exception:
        return {"ok": False, "error": "Bad UCI format"}

    if user_move not in board.legal_moves:
        return {"ok": False, "error": "Illegal move"}

    # Whoever is about to move IS the player. Derive their color so all cp values
    # passed into classify_move are from THEIR POV (matches /summarize, /import,
    # /analyze). Hardcoding chess.WHITE here used to invert labels for Black.
    user_color = board.turn

    # 1) score user's move from their POV
    user_san = board.san(user_move)  # SAN must be before push
    with ENGINE_LOCK:
        t = time.perf_counter()
        before_cp = eval_cp(ENGINE, board, user_color)
        _log_duration("play.before_cp", t)
        # Cheap pre-move best line so the label can flag Miss in real time.
        # depth=4 keeps the added latency tiny (~10 ms).
        t = time.perf_counter()
        pre_best = best_line(ENGINE, board, depth=4, multipv=1, plies=1)
        _log_duration("play.pre_best", t)
        best_eval_before = pre_best[0][1] if pre_best else None
    board.push(user_move)
    with ENGINE_LOCK:
        t = time.perf_counter()
        after_cp = eval_cp(ENGINE, board, user_color)
        _log_duration("play.after_cp", t)
        # Keep both white & black POV cp values for the frontend's eval bar.
        cp_after_user_white = eval_cp(ENGINE, board, chess.WHITE) if user_color == chess.BLACK else after_cp
        cp_after_user_black = eval_cp(ENGINE, board, chess.BLACK) if user_color == chess.WHITE else after_cp
        _log_duration("play.cp_after_user_dual", t)
    delta = after_cp - before_cp
    cls = classify_move(before_cp, after_cp, best_eval_before)
    tag = cls["label"]

    # Record this move in the running game history with full classification.
    game_history.append({
        "san": user_san,
        "label": tag,
        "severity": cls["severity"],
        "missed_opportunity": cls["missed_opportunity"],
        "missed_win": cls["missed_win"],
        "cp_delta": delta,
    })

    with ENGINE_LOCK:
        t = time.perf_counter()
        lines_after = best_line(ENGINE, board, plies=4)
        _log_duration("play.idea_pv", t)
    coach_idea = san_line(board, lines_after[0][0]) if lines_after else None

    fen_after_user = board.fen()

    if board.is_game_over():
        resp = {
            "ok": True,
            "user": {"uci": uci, "san": user_san, "delta_cp": delta, "label": tag, "idea": coach_idea},
            "coach": None,
            "fen_after_user": fen_after_user,
            "fen_after_coach": fen_after_user,
            "game_over": True,
            "result": board.result(),
            "cp_after_user": {"white": cp_after_user_white, "black": cp_after_user_black},
            "cp_after_coach": {"white": cp_after_user_white, "black": cp_after_user_black},
        }
        # Optional AI summary only when move is not labeled Good
        if body.summary and summarize_move and (tag != "Good"):
            try:
                with ENGINE_LOCK:
                    best_lines = best_line(ENGINE, board_before, multipv=1, plies=6)
                if best_lines:
                    best_moves = best_lines[0][0]
                    best_move = best_moves[0]
                    best_san = board_before.san(best_move)
                    best_uci = best_move.uci()
                    # Reuse the cheap pre-move best_eval already computed above (player POV).
                    # Avoids a second engine call AND avoids the chess.WHITE POV bug.
                    best_eval_cp = best_eval_before if best_eval_before is not None else best_lines[0][1]
                    pv_best_san = san_line(board_before, best_moves)
                else:
                    best_san = best_uci = pv_best_san = None
                    best_eval_cp = None

                with ENGINE_LOCK:
                    played_line = best_line(ENGINE, chess.Board(fen_after_user), multipv=1, plies=6)
                pv_played_san = user_san + (" " + san_line(chess.Board(fen_after_user), played_line[0][0]) if played_line else "")

                data = {
                    "fen": body.fen,
                    "san": user_san,
                    "uci": uci,
                    "eval_before_cp": before_cp,
                    "eval_after_cp": after_cp,
                    "best_san": best_san,
                    "best_uci": best_uci,
                    "best_eval_cp": best_eval_cp,
                    "pv_best_san": pv_best_san,
                    "pv_played_san": pv_played_san,
                }
                resp["ai_summary"] = summarize_move(data)
            except Exception as e:
                resp["ai_summary_error"] = str(e)
        _log_duration("play.total", t_all)
        return resp

    # 2) coach reply (limit playing strength to user's selected skill, if provided)
    target_skill = None
    try:
        if body.skill is not None:
            target_skill = max(0, min(int(body.skill), 20))  # clamp conservatively
    except Exception:
        target_skill = None

    # Fallback elo if engine lacks Skill Level
    target_elo = SKILL_TO_ELO.get(target_skill) if target_skill is not None else None

    with ENGINE_LOCK:
        t = time.perf_counter()
        reply_move, plan = humanish_reply(
            ENGINE,
            board,
            target_elo=target_elo,
            target_skill=target_skill,
        )
        _log_duration("play.reply_select", t)
    reply_san = board.san(reply_move)
    board.push(reply_move)
    fen_after_coach = board.fen()
    # CP after coach reply from both POVs (frontend eval bar needs both).
    with ENGINE_LOCK:
        t = time.perf_counter()
        cp_after_coach_white = eval_cp(ENGINE, board, chess.WHITE)
        _log_duration("play.cp_after_coach_white", t)
        t = time.perf_counter()
        cp_after_coach_black = eval_cp(ENGINE, board, chess.BLACK)
        _log_duration("play.cp_after_coach_black", t)

    resp = {
        "ok": True,
        "user": {"uci": uci, "san": user_san, "delta_cp": delta, "label": tag, "idea": coach_idea},
        "coach": {"uci": reply_move.uci(), "san": reply_san, "plan": plan},
        "fen_after_user": fen_after_user,
        "fen_after_coach": fen_after_coach,
        "game_over": board.is_game_over(),
        "result": board.result() if board.is_game_over() else None,
        "cp_after_user": {"white": cp_after_user_white, "black": cp_after_user_black},
        "cp_after_coach": {"white": cp_after_coach_white, "black": cp_after_coach_black},
    }

    # Optional AI summary only when move is not labeled Good
    if body.summary and summarize_move and (tag != "Good"):
        try:
            with ENGINE_LOCK:
                best_lines = best_line(ENGINE, board_before, multipv=1, plies=6)
            if best_lines:
                best_moves = best_lines[0][0]
                best_move = best_moves[0]
                best_san = board_before.san(best_move)
                best_uci = best_move.uci()
                # Reuse the pre-move best_eval (player POV) computed at the top of /play.
                # best_lines[0][1] is also from player POV (best_line uses board.turn).
                best_eval_cp = best_eval_before if best_eval_before is not None else best_lines[0][1]
                pv_best_san = san_line(board_before, best_moves)
            else:
                best_san = best_uci = pv_best_san = None
                best_eval_cp = None

            with ENGINE_LOCK:
                played_line = best_line(ENGINE, chess.Board(fen_after_user), multipv=1, plies=6)
            pv_played_san = user_san + (" " + san_line(chess.Board(fen_after_user), played_line[0][0]) if played_line else "")

            data = {
                "fen": body.fen,
                "san": user_san,
                "uci": uci,
                "eval_before_cp": before_cp,
                "eval_after_cp": after_cp,
                "best_san": best_san,
                "best_uci": best_uci,
                "best_eval_cp": best_eval_cp,
                "pv_best_san": pv_best_san,
                "pv_played_san": pv_played_san,
            }
            resp["ai_summary"] = summarize_move(data)
        except Exception as e:
            resp["ai_summary_error"] = str(e)

    _log_duration("play.total", t_all)
    return resp
class SummaryBody(BaseModel):
    fen: str                 # FEN before the user's move
    uci: str                 # user's move in UCI (e2e4, g1f3, e7e8q)
    promotion: Optional[str] = None
    label: Optional[str] = None  # pre-computed label from /play ("Good","Inaccuracy","Mistake","Blunder")
    # Classification carried forward from the UI's badge (computed by
    # /analyze-chessdotcom at depth=8). When present, the summarizer uses
    # these to frame the explanation instead of reclassifying at depth=19 —
    # which would otherwise produce "Mistake badge + 'solid choice' verdict"
    # mismatches when depth=19 disagrees with depth=8 on borderline moves.
    # All optional; single-call /summarize (no badge) keeps reclassifying.
    severity: Optional[str] = None
    missed_opportunity: Optional[bool] = None
    missed_win: Optional[bool] = None
    created_problem: Optional[bool] = None
    cp_delta: Optional[int] = None
    cp_before: Optional[int] = None
    cp_after: Optional[int] = None
    # Player rating, when known (review mode forwards it). Calibrates BOTH the
    # rating-aware move classification AND the coaching advice's depth/vocabulary
    # (a sub-800 player gets CCT/piece-safety advice, not positional nuance).
    rating: Optional[int] = None



# Lightweight summarization endpoint so the UI can render instantly and fetch
# the AI coach explanation without blocking gameplay.
@app.post("/summarize")
def summarize(body: SummaryBody):
    t_all = time.perf_counter()
    try:
        board_before = chess.Board(body.fen)
    except Exception:
        return {"ok": False, "error": "Bad FEN"}

    uci = body.uci if not body.promotion else (body.uci + body.promotion)
    try:
        user_move = chess.Move.from_uci(uci)
    except Exception:
        return {"ok": False, "error": "Bad UCI"}
    if user_move not in board_before.legal_moves:
        return {"ok": False, "error": "Illegal move"}

    # The side to move in board_before IS the player about to make this move.
    # All cp values must be from THEIR POV so classify_move's win% math matches
    # the rest of the app (evaluate-move, import-chessdotcom).
    user_color = board_before.turn

    # Compute engine data — serialized via ENGINE_LOCK to prevent race conditions.
    # Clear the hash table so the best-move answer is deterministic across repeated
    # /summarize calls for the same position. Without this, transposition entries
    # from earlier engine calls bias search order and the "best" move can flip
    # between near-equal candidates at low depth.
    with ENGINE_LOCK:
        try:
            ENGINE.configure({"Clear Hash": True})
        except Exception:
            pass  # not all engines expose this option
        t = time.perf_counter()
        before_cp = eval_cp(ENGINE, board_before, user_color, depth=19)
        _log_duration("summ.before_cp", t)
        user_san = board_before.san(user_move)
        board_after = board_before.copy()
        board_after.push(user_move)
        t = time.perf_counter()
        after_cp = eval_cp(ENGINE, board_after, user_color, depth=19)
        _log_duration("summ.after_cp", t)
        delta = after_cp - before_cp

        t = time.perf_counter()
        best_lines = best_line(ENGINE, board_before, depth=19, multipv=1, plies=4)
        _log_duration("summ.best_line_pre", t)
        if best_lines:
            best_moves = best_lines[0][0]
            best_move = best_moves[0]
            best_san = board_before.san(best_move)
            best_uci = best_move.uci()
            best_eval_cp = best_lines[0][1]
            pv_best_san = san_line(board_before, best_moves)
        else:
            best_san = best_uci = pv_best_san = None
            best_eval_cp = None

        t = time.perf_counter()
        played_line = best_line(ENGINE, board_after, depth=19, multipv=1, plies=4)
        _log_duration("summ.best_line_post", t)
        pv_played_san = user_san + (" " + san_line(board_after, played_line[0][0]) if played_line else "")

    # Unified classifier — same logic /play and /import-chessdotcom use, but with
    # best_eval available so we can also flag missed_opportunity.
    tag = classify_move(before_cp, after_cp, best_eval_cp, rating=body.rating)["label"]

    data = {
        "fen": body.fen,
        "san": user_san,
        "uci": uci,
        "eval_before_cp": before_cp,
        "eval_after_cp": after_cp,
        "best_san": best_san,
        "best_uci": best_uci,
        "best_eval_cp": best_eval_cp,
        "pv_best_san": pv_best_san,
        "pv_played_san": pv_played_san,
        "rating": body.rating,
    }
    # Only call the LLM when move is not labeled Good (Inaccuracy/Mistake/Blunder)
    if tag != "Good":
        if summarize_move is None:
            _log_duration("summ.total", t_all)
            return {"ok": False, "error": "Summarizer not configured"}
        try:
            t = time.perf_counter()
            ai = summarize_move(data)
            _log_duration("summ.llm_call", t)
            _log_duration("summ.total", t_all)
            return {"ok": True, "ai_summary": ai}
        except Exception as e:
            _log_duration("summ.total", t_all)
            return {"ok": False, "error": str(e)}
    else:
        # Skip LLM call; return a lightweight local summary so the UI can render.
        _log_duration("summ.total", t_all)
        return {
            "ok": True,
            "ai_summary": {
                "summary": "Solid move — evaluation did not drop.",
            },
        }


def _summarize_engine_data(body: SummaryBody) -> dict:
    """Run the engine work for one move and assemble the data dict that
    summarize_move (or summarize_move_batch) expects. Shared between /summarize
    and /summarize-batch so the engine setup stays identical.

    Returns the data dict, or raises ValueError with a user-facing message.
    """
    try:
        board_before = chess.Board(body.fen)
    except Exception:
        raise ValueError("Bad FEN")
    uci = body.uci if not body.promotion else (body.uci + body.promotion)
    try:
        user_move = chess.Move.from_uci(uci)
    except Exception:
        raise ValueError("Bad UCI")
    if user_move not in board_before.legal_moves:
        raise ValueError("Illegal move")

    user_color = board_before.turn
    with ENGINE_LOCK:
        try: ENGINE.configure({"Clear Hash": True})
        except Exception: pass
        before_cp = eval_cp(ENGINE, board_before, user_color, depth=19)
        user_san = board_before.san(user_move)
        board_after = board_before.copy()
        board_after.push(user_move)
        after_cp = eval_cp(ENGINE, board_after, user_color, depth=19)
        best_lines = best_line(ENGINE, board_before, depth=19, multipv=1, plies=4)
        if best_lines:
            best_moves = best_lines[0][0]
            best_move = best_moves[0]
            best_san = board_before.san(best_move)
            best_uci = best_move.uci()
            best_eval_cp = best_lines[0][1]
            pv_best_san = san_line(board_before, best_moves)
        else:
            best_san = best_uci = pv_best_san = None
            best_eval_cp = None
        played_line = best_line(ENGINE, board_after, depth=19, multipv=1, plies=4)
        pv_played_san = user_san + (" " + san_line(board_after, played_line[0][0]) if played_line else "")

    return {
        "fen": body.fen,
        "san": user_san,
        "uci": uci,
        "eval_before_cp": before_cp,
        "eval_after_cp": after_cp,
        "best_san": best_san,
        "best_uci": best_uci,
        "best_eval_cp": best_eval_cp,
        "pv_best_san": pv_best_san,
        "pv_played_san": pv_played_san,
        # Badge-level classification forwarded from the UI. summarize_move_batch
        # uses these to keep its framing consistent with the badge the user
        # clicked; absent fields trigger fallback re-classification.
        "label": body.label,
        "severity": body.severity,
        "missed_opportunity": body.missed_opportunity,
        "missed_win": body.missed_win,
        "created_problem": body.created_problem,
        "cp_delta": body.cp_delta,
        "cp_before": body.cp_before,
        "cp_after": body.cp_after,
        "rating": body.rating,
    }


class SummaryBatchBody(BaseModel):
    items: list[SummaryBody] = []


@app.post("/summarize-batch")
def summarize_batch(body: SummaryBatchBody):
    """Batch the coach explanation for many flagged moves into ONE Gemini call.
    Frontend uses this when the user enters a game review — instead of calling
    /summarize N times (one per click), it pre-loads all flagged moves at once.

    Returns: {ok, summaries: [{ok, ai_summary | error}, ...]} — same length as input.
    """
    t_all = time.perf_counter()
    if not body.items:
        return {"ok": True, "summaries": []}

    # Run engine work for each item (serialized via ENGINE_LOCK inside the helper).
    # Items that fail engine setup get a None placeholder — we'll fill in an error
    # entry at the end so the per-item summary list stays index-aligned with input.
    data_list = []
    per_item_errors = []
    for item in body.items:
        try:
            d = _summarize_engine_data(item)
            # Carry the UI-provided label so summarize_move_batch's classifier
            # uses the same label the user sees (matches /summarize behavior).
            data_list.append(d)
            per_item_errors.append(None)
        except ValueError as e:
            data_list.append(None)
            per_item_errors.append(str(e))

    # Filter to only valid items for the LLM batch. Keep an index map back to original.
    valid_indices = [i for i, d in enumerate(data_list) if d is not None]
    valid_data = [data_list[i] for i in valid_indices]

    summaries_by_index: dict = {}
    if valid_data:
        if summarize_move is None:
            for i in valid_indices:
                summaries_by_index[i] = {"ok": False, "error": "Summarizer not configured"}
        else:
            try:
                from gemini_client import summarize_move_batch
                ai_list = summarize_move_batch(valid_data)
                for slot, src in zip(valid_indices, ai_list):
                    summaries_by_index[slot] = {"ok": True, "ai_summary": src or {}}
            except Exception as e:
                err = str(e)
                for i in valid_indices:
                    summaries_by_index[i] = {"ok": False, "error": err}

    # Reassemble in original order, filling failed-engine items with their error.
    out = []
    for i in range(len(body.items)):
        if per_item_errors[i] is not None:
            out.append({"ok": False, "error": per_item_errors[i]})
        else:
            out.append(summaries_by_index.get(i, {"ok": False, "error": "Unknown error"}))

    _log_duration("summ_batch.total", t_all)
    return {"ok": True, "summaries": out}


class ClarifyBody(BaseModel):
    summary: dict            # last ai_summary JSON returned by /summarize or /play
    question: str            # user's clarification question



@app.post("/clarify")
def clarify(body: ClarifyBody):
    if summarize_move is None:
        return {"ok": False, "error": "Summarizer not configured"}
    try:
        answer = clarify_move(body.summary, body.question)
        return {"ok": True, "answer": answer}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/new-game")
def new_game():
    """Reset the server-side game history for a fresh game."""
    global game_history
    game_history = []
    return {"ok": True}


class AnalyzeGameBody(BaseModel):
    moves: list[dict]  # [{san, label, cp_delta}, ...]


@app.post("/analyze-game")
def analyze_game_endpoint(body: AnalyzeGameBody):
    """Send the completed game move list to Gemini for weakness detection."""
    if analyze_game is None:
        return {"ok": False, "error": "Game analysis requires Gemini — not configured"}
    if not body.moves:
        return {"ok": False, "error": "No moves provided"}
    try:
        result = analyze_game(body.moves)
        return {"ok": True, **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class EvalMoveBody(BaseModel):
    fen: str
    uci: str


@app.post("/evaluate-move")
def evaluate_move(body: EvalMoveBody):
    """Evaluate a single move with Stockfish — fast, no LLM.
    Returns label, cp_delta, best move, and context needed for /summarize.
    """
    try:
        board = chess.Board(body.fen)
    except Exception:
        return {"ok": False, "error": "Bad FEN"}

    user_color = board.turn

    try:
        move = chess.Move.from_uci(body.uci)
    except Exception:
        return {"ok": False, "error": "Bad UCI"}

    if move not in board.legal_moves:
        return {"ok": False, "error": "Illegal move"}

    user_san = board.san(move)

    with ENGINE_LOCK:
        before_cp = eval_cp(ENGINE, board, user_color, depth=8)
        best_lines = best_line(ENGINE, board, depth=8, multipv=1, plies=4)

    best_eval_before = best_lines[0][1] if best_lines else None
    best_move_obj    = best_lines[0][0][0] if best_lines and best_lines[0][0] else None
    best_san_str     = board.san(best_move_obj) if best_move_obj else None
    best_uci_str     = best_move_obj.uci() if best_move_obj else None
    pv_best_san      = san_line(board, best_lines[0][0]) if best_lines and best_lines[0][0] else None

    board.push(move)
    fen_after = board.fen()

    with ENGINE_LOCK:
        after_cp     = eval_cp(ENGINE, board, user_color, depth=8)
        played_lines = best_line(ENGINE, board, depth=8, multipv=1, plies=4)

    pv_played_san = user_san + (
        " " + san_line(board, played_lines[0][0]) if played_lines and played_lines[0][0] else ""
    )

    delta = after_cp - before_cp
    label = classify_move(before_cp, after_cp, best_eval_before)["label"]

    return {
        "ok": True,
        "fen": body.fen,          # FEN before the move — needed by /summarize
        "san": user_san,
        "uci": body.uci,
        "label": label,
        "cp_delta": round(delta),
        "best_san": best_san_str,
        "best_uci": best_uci_str,
        "best_eval_cp": best_eval_before,
        "fen_after": fen_after,
        # Full context so the frontend can call /summarize without another round-trip
        "eval_before_cp": before_cp,
        "eval_after_cp": after_cp,
        "pv_best_san": pv_best_san,
        "pv_played_san": pv_played_san,
    }


class PuzzleRequestBody(BaseModel):
    themes: list[str]
    count: int = 5


class DrillExplainBatchBody(BaseModel):
    # Each item carries everything Gemini needs for one drill explanation:
    # fen_after, played_san, opponent_best_san, correction_fen, correction_best_san, motif.
    # Caller batches up to ~10 to keep one Gemini call covering many drills.
    items: list[dict] = []


@app.post("/drill/explain-batch")
def drill_explain_batch(body: DrillExplainBatchBody):
    """Generate threat + correction explanations for a batch of drill questions.
    One Gemini call covers all items — saves quota vs one-call-per-question.
    """
    if not body.items:
        return {"ok": True, "explanations": []}
    try:
        from gemini_client import explain_drill_batch
        explanations = explain_drill_batch(body.items)
        return {"ok": True, "explanations": explanations}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/puzzles")
def get_puzzles(body: PuzzleRequestBody):
    """Return random puzzles filtered by weakness themes."""
    try:
        from puzzle_db import get_puzzles_by_themes, db_stats  # lazy import so missing DB gives a clean error
    except ImportError:
        return {"ok": False, "error": "puzzle_db module not found"}

    try:
        puzzles = get_puzzles_by_themes(
            themes=body.themes,
            count=body.count,
            rating_min=400,
            rating_max=1100,
        )
        return {"ok": True, "puzzles": puzzles}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/puzzles/stats")
def puzzle_stats():
    """Health check for the puzzle database."""
    try:
        from puzzle_db import db_stats
        return {"ok": True, **db_stats()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class AnalyzeChessDotComBody(BaseModel):
    username: str
    count: int = 10
    time_class: Optional[str] = None  # filter by "rapid", "blitz", "bullet"


@app.post("/analyze-chessdotcom")
def analyze_chessdotcom(body: AnalyzeChessDotComBody):
    """Fetch the last N Chess.com games, annotate each with Stockfish, and find cross-game weakness patterns."""
    import chess.pgn
    import io
    import urllib.request
    import json as _json
    from datetime import datetime

    username = body.username.strip()
    if not username:
        return {"ok": False, "error": "Username required"}

    # Fetch enough months to collect `count` games
    now = datetime.now()
    all_games = []
    for delta in range(6):
        month = now.month - delta
        year = now.year
        while month <= 0:
            month += 12
            year -= 1
        url = f"https://api.chess.com/pub/player/{username}/games/{year}/{month:02d}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "chess-coach/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read())
                month_games = data.get("games", [])
                if body.time_class:
                    month_games = [g for g in month_games if g.get("time_class") == body.time_class]
                all_games = month_games + all_games  # prepend older games so list is chronological
                if len(all_games) >= body.count:
                    break
        except Exception:
            continue

    if not all_games:
        return {"ok": False, "error": f"No games found for '{username}'"}

    recent_games = all_games[-body.count:]  # take the most recent N

    annotated_games = []
    for raw_game in recent_games:
        pgn_str = raw_game.get("pgn", "")
        if not pgn_str:
            continue
        try:
            pgn_game = chess.pgn.read_game(io.StringIO(pgn_str))
        except Exception:
            continue

        white_username = raw_game.get("white", {}).get("username", "").lower()
        user_color = chess.WHITE if white_username == username.lower() else chess.BLACK

        # The user's rating in THIS game — feeds rating-aware move classification
        # (beginner swings judged less harshly than a strong player's). Chess.com's
        # game JSON carries per-game ratings on white/black.
        user_rating = (raw_game.get("white", {}) if user_color == chess.WHITE
                       else raw_game.get("black", {})).get("rating")

        # Extract opening from PGN headers
        opening = pgn_game.headers.get("ECOUrl", "") or pgn_game.headers.get("Opening", "")
        if "/" in opening:
            opening = opening.rsplit("/", 1)[-1].replace("-", " ").title()

        board = pgn_game.board()
        move_history = []
        move_number = 0
        # Clock context for time-pressure diagnosis (PGN %clk annotations).
        base_s, inc_s = parse_time_control(pgn_game.headers.get("TimeControl"))
        prev_user_clock = None  # the user's remaining clock after their PREVIOUS move
        with ENGINE_LOCK:
            prev_cp = eval_cp(ENGINE, board, user_color, depth=6)

        # Iterate nodes (not bare moves) so node.clock() is available per move.
        for node in pgn_game.mainline():
            move = node.move
            is_user_move = (board.turn == user_color)
            san = board.san(move)
            fen_before = board.fen()

            # Get best-available eval + best move SAN before the user's move
            # (for Miss detection AND for tactical-context enrichment below).
            best_eval_before = None
            best_san_before = None
            pre_move_options = []  # top 3 candidates for the user — feeds the correction quiz
            if is_user_move:
                with ENGINE_LOCK:
                    # multipv=3 so we have 2 distractor candidates for the correction phase
                    bl = best_line(ENGINE, board, depth=6, multipv=3, plies=1)
                if bl:
                    best_eval_before = bl[0][1]
                    for line_moves, score_cp in bl:
                        if not line_moves:
                            continue
                        try:
                            pre_move_options.append({
                                "san": board.san(line_moves[0]),
                                "eval_cp": int(score_cp),  # user's POV
                            })
                        except Exception:
                            continue
                    best_san_before = pre_move_options[0]["san"] if pre_move_options else None

            board.push(move)
            with ENGINE_LOCK:
                curr_cp = eval_cp(ENGINE, board, user_color, depth=6)

            if is_user_move:
                move_number += 1
                delta = curr_cp - prev_cp
                cls = classify_move(prev_cp, curr_cp, best_eval_before, rating=user_rating)

                # Time-pressure context: clock left after this move, time spent on it,
                # and whether the player was in time pressure. None when no clock data.
                clock_after = node.clock()
                tp_flag = under_time_pressure(clock_after, base_s)
                time_spent = None
                if clock_after is not None and prev_user_clock is not None:
                    # spent = (clock before this move) − (clock after) + increment gained
                    time_spent = max(0.0, prev_user_clock - clock_after + (inc_s or 0))
                if clock_after is not None:
                    prev_user_clock = clock_after

                # For flagged moves, enrich with tactical context (motif, hanging pieces,
                # opponent's punishment) so the cross-game analyzer sees WHAT went wrong
                # — not just the bare move name. Without this, the LLM pattern-matches
                # on the move name (e.g. "O-O" → "king safety") even when the real issue
                # was a hanging piece.
                motif = None
                tactical_summary = None
                drill_question = None
                is_flagged = cls["severity"] in ("Mistake", "Blunder") or cls["missed_opportunity"]
                if is_flagged and best_san_before:
                    try:
                        # Get top 3 opponent replies from position AFTER the user's blunder.
                        # #1 is the "correct" answer for the drill quiz; #2 and #3 are
                        # plausible distractors (legitimate moves, just objectively worse).
                        # This board state is post-user-move, so it's opponent's turn.
                        fen_after = board.fen()
                        with ENGINE_LOCK:
                            opp_top = best_line(ENGINE, board, depth=6, multipv=3, plies=1)
                        opp_options = []
                        for line_moves, score_cp in opp_top:
                            if not line_moves:
                                continue
                            try:
                                opp_options.append({
                                    "san": board.san(line_moves[0]),
                                    "eval_cp": int(score_cp),  # opponent's POV
                                })
                            except Exception:
                                continue

                        opp_san = opp_options[0]["san"] if opp_options else None
                        pv_played_san = f"{san} {opp_san}" if opp_san else san
                        from gemini_client import _compute_chess_facts
                        facts = _compute_chess_facts(
                            fen_before=fen_before,
                            player_san=san,
                            pv_played_san=pv_played_san,
                            best_san=best_san_before,
                        )
                        motif = facts.get("motif")
                        # Build a compact one-line summary the cross-game LLM can categorize on.
                        parts = []
                        if facts.get("opponent_reply_desc"):
                            parts.append(f"opponent: {facts['opponent_reply_desc']}")
                        elif facts.get("hanging_pieces"):
                            parts.append(f"left hanging: {', '.join(facts['hanging_pieces'][:2])}")
                        if facts.get("best_move_desc") and facts["best_move_desc"] != best_san_before:
                            parts.append(f"better: {facts['best_move_desc']}")
                        tactical_summary = "; ".join(parts) or None

                        # Drill question: only emit for genuine blunders or missed wins.
                        # Mistakes are real but often positional (no clean tactical lesson),
                        # so they make for muddy drill content. Keep them in motif/tactical
                        # context for the cross-game analyzer, just skip the quiz.
                        # Also require ≥2 distinct candidate moves so the multi-choice works.
                        should_drill = cls["severity"] == "Blunder" or cls["missed_opportunity"]
                        if should_drill and len(opp_options) >= 2:
                            # Phase 2: correction question — "knowing the opponent threatens X,
                            # what should you have played?" Drops the user back into fen_before
                            # with the same multi-choice UI but for THEIR best move.
                            correction = None
                            if len(pre_move_options) >= 2:
                                correction = {
                                    "fen": fen_before,
                                    "options": pre_move_options,
                                    "correct_san": pre_move_options[0]["san"],
                                    "correct_desc": facts.get("best_move_desc") or pre_move_options[0]["san"],
                                }
                            drill_question = {
                                "fen_after": fen_after,
                                "previous_move_san": san,        # what the user played (sets up the position)
                                "options": opp_options,           # [{san, eval_cp}, ...] — index 0 is correct
                                "correct_san": opp_options[0]["san"],
                                "correct_desc": facts.get("opponent_reply_desc") or opp_options[0]["san"],
                                "correction": correction,         # phase 2 — what you SHOULD have played
                            }
                    except Exception:
                        pass  # enrichment is best-effort — don't fail the whole analysis

                move_history.append({
                    "san": san,
                    "label": cls["label"],          # composite (may include "+ Miss") or "Missed Win"
                    "severity": cls["severity"],    # base label only — for filtering
                    "missed_opportunity": cls["missed_opportunity"],
                    "missed_win": cls["missed_win"],
                    "cp_delta": round(delta),
                    "move_number": move_number,
                    "fen_before": fen_before,
                    # Tactical context (only populated for flagged moves — None otherwise).
                    # Lets analyze_multiple_games categorize WHY a move was bad instead of
                    # guessing from the move name alone.
                    "motif": motif,
                    "tactical_summary": tactical_summary,
                    # Threat-spotter drill question (only flagged moves with ≥2 candidates).
                    "drill_question": drill_question,
                    # Time-pressure context (None when the PGN carried no clock data).
                    "under_time_pressure": tp_flag,
                    "time_spent_s": round(time_spent, 1) if time_spent is not None else None,
                    "clock_after_s": round(clock_after, 1) if clock_after is not None else None,
                })

            prev_cp = curr_cp

        white_info = raw_game.get("white", {})
        black_info = raw_game.get("black", {})
        user_result = white_info.get("result") if user_color == chess.WHITE else black_info.get("result")

        annotated_games.append({
            "game_info": {
                "white": white_info.get("username"),
                "black": black_info.get("username"),
                "white_rating": white_info.get("rating"),
                "black_rating": black_info.get("rating"),
                "user_color": "white" if user_color == chess.WHITE else "black",
                "result": user_result,
                "time_class": raw_game.get("time_class"),
                "date": (lambda dt: f"{dt.day} {dt.strftime('%b %Y')}")(datetime.fromtimestamp(raw_game["end_time"])) if raw_game.get("end_time") else None,
                "url": raw_game.get("url"),
                "opening": opening,
            },
            "moves": move_history,
        })

    if not annotated_games:
        return {"ok": False, "error": "Could not parse any games"}

    # Compute hard stats from annotated move data.
    # Use `severity` (base label: Good/Inaccuracy/Mistake/Blunder) for counting —
    # NOT `label` which may be a composite like "Mistake + Miss" or "Miss".
    all_moves = [m for g in annotated_games for m in g["moves"]]
    total = len(all_moves)
    blunders    = sum(1 for m in all_moves if m.get("severity") == "Blunder")
    mistakes    = sum(1 for m in all_moves if m.get("severity") == "Mistake")
    inaccuracies= sum(1 for m in all_moves if m.get("severity") == "Inaccuracy")
    good_moves  = sum(1 for m in all_moves if m.get("severity") == "Good")
    # Cap at 1000 cp to exclude mate-detection values (Stockfish reports ~99000 for forced mate)
    MATE_THRESHOLD = 1000
    regular_losses = [min(max(0, -m["cp_delta"]), MATE_THRESHOLD) for m in all_moves]
    avg_cp_loss = round(sum(regular_losses) / total, 1) if total else 0
    n_games = len(annotated_games)

    def _is_error(m: dict) -> bool:
        # Any move that hurt the position (Mistake/Blunder) OR missed a clear win.
        return m.get("severity") in ("Mistake", "Blunder") or m.get("missed_opportunity", False)

    # Time-pressure split of serious errors (Mistake/Blunder). Only moves that
    # carried clock data (under_time_pressure is not None) are counted, so the
    # split honestly reflects what we can see. This drives the coach's choice
    # between "manage your clock / play slower" and "train recognition".
    serious = [m for m in all_moves if m.get("severity") in ("Mistake", "Blunder")]
    serious_with_clock = [m for m in serious if m.get("under_time_pressure") is not None]
    errors_time_pressure = sum(1 for m in serious_with_clock if m.get("under_time_pressure"))
    errors_with_time     = sum(1 for m in serious_with_clock if not m.get("under_time_pressure"))

    player_stats = {
        "total_games":       n_games,
        "total_moves":       total,
        "blunders":          blunders,
        "mistakes":          mistakes,
        "inaccuracies":      inaccuracies,
        "good_moves":        good_moves,
        "blunder_rate_pct":  round(blunders / total * 100, 1) if total else 0,
        "accuracy_pct":      round(good_moves / total * 100, 1) if total else 0,
        "blunders_per_game": round(blunders / n_games, 1) if n_games else 0,
        "avg_cp_loss":       avg_cp_loss,
        # Phase breakdown (approximate by move number)
        "errors_opening":    sum(1 for m in all_moves if _is_error(m) and m.get("move_number",0) <= 15),
        "errors_middlegame": sum(1 for m in all_moves if _is_error(m) and 15 < m.get("move_number",0) <= 35),
        "errors_endgame":    sum(1 for m in all_moves if _is_error(m) and m.get("move_number",0) > 35),
        # Miss (missed win) is independent of severity — count every move with the flag set.
        "miss_count":        sum(1 for m in all_moves if m.get("missed_opportunity", False)),
        "active_blunders":   blunders,
        # Time-pressure diagnosis: of serious errors WITH clock data, how many were
        # made in time pressure (→ clock/time-control advice) vs with time to spare
        # (→ recognition / safety-check advice). errors_clock_known = denominator.
        "errors_time_pressure": errors_time_pressure,
        "errors_with_time":     errors_with_time,
        "errors_clock_known":   len(serious_with_clock),
    }

    # Fetch Chess.com rating for the most common time class played
    time_class_counts: dict = {}
    for g in annotated_games:
        tc = g["game_info"].get("time_class")
        if tc:
            time_class_counts[tc] = time_class_counts.get(tc, 0) + 1
    dominant_tc = max(time_class_counts, key=time_class_counts.get) if time_class_counts else None

    player_rating = None
    try:
        stats_url = f"https://api.chess.com/pub/player/{username}/stats"
        req = urllib.request.Request(stats_url, headers={"User-Agent": "chess-coach/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            stats_data = _json.loads(r.read())
        tc_key_map = {"rapid": "chess_rapid", "blitz": "chess_blitz", "bullet": "chess_bullet", "daily": "chess_daily"}
        for tc in ([dominant_tc] if dominant_tc else []) + ["rapid", "blitz", "bullet"]:
            key = tc_key_map.get(tc, f"chess_{tc}")
            rating_val = stats_data.get(key, {}).get("last", {}).get("rating")
            if rating_val:
                player_rating = rating_val
                break
    except Exception:
        pass  # rating stays None; UI handles gracefully

    # Cross-game weakness analysis via Gemini
    try:
        from gemini_client import analyze_multiple_games
        analysis = analyze_multiple_games(annotated_games)
    except Exception as e:
        analysis = {"common_weaknesses": [], "error": str(e)}

    # High-level player summary via Gemini
    player_summary = None
    try:
        from gemini_client import generate_player_summary
        player_summary = generate_player_summary(
            games=annotated_games,
            stats=player_stats,
            rating=player_rating,
            time_class=dominant_tc,
            weaknesses=analysis.get("common_weaknesses", []),
        )
    except Exception as e:
        player_summary = {"error": str(e)}

    return {
        "ok": True,
        "games": annotated_games,
        "player_stats": player_stats,
        "player_rating": player_rating,
        "player_time_class": dominant_tc,
        "player_summary": player_summary,
        **analysis,
    }


class ImportChessDotComBody(BaseModel):
    username: str
    time_class: Optional[str] = None  # "rapid", "blitz", "bullet" — None means any


@app.post("/import-chessdotcom")
def import_chessdotcom(body: ImportChessDotComBody):
    """Fetch the user's last Chess.com game, annotate with Stockfish, and analyze weaknesses."""
    import chess.pgn
    import io
    import urllib.request
    import json
    from datetime import datetime

    username = body.username.strip()
    if not username:
        return {"ok": False, "error": "Username required"}

    # Try current month then up to 2 months back to find a game
    now = datetime.now()
    games = []
    for delta in range(3):
        month = now.month - delta
        year = now.year
        while month <= 0:
            month += 12
            year -= 1
        url = f"https://api.chess.com/pub/player/{username}/games/{year}/{month:02d}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "chess-coach/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                month_games = data.get("games", [])
                if body.time_class:
                    month_games = [g for g in month_games if g.get("time_class") == body.time_class]
                if month_games:
                    games = month_games
                    break
        except Exception:
            continue

    if not games:
        return {"ok": False, "error": f"No games found for '{username}'"}

    last_game = games[-1]
    pgn_str = last_game.get("pgn", "")
    if not pgn_str:
        return {"ok": False, "error": "Game has no PGN data"}

    try:
        pgn_game = chess.pgn.read_game(io.StringIO(pgn_str))
    except Exception as e:
        return {"ok": False, "error": f"Failed to parse PGN: {e}"}

    # Determine which color the user played
    white_username = last_game.get("white", {}).get("username", "").lower()
    user_color = chess.WHITE if white_username == username.lower() else chess.BLACK

    # The user's rating in this game — feeds rating-aware move classification.
    user_rating = (last_game.get("white", {}) if user_color == chess.WHITE
                   else last_game.get("black", {})).get("rating")

    # Annotate each user move with Stockfish
    board = pgn_game.board()
    move_history = []
    move_number = 0
    base_s, inc_s = parse_time_control(pgn_game.headers.get("TimeControl"))
    prev_user_clock = None
    with ENGINE_LOCK:
        prev_cp = eval_cp(ENGINE, board, user_color, depth=8)

    for node in pgn_game.mainline():
        move = node.move
        is_user_move = (board.turn == user_color)
        san = board.san(move)

        # Get best-available eval before the user's move (for Miss detection).
        # Cheap shallow lookahead — the per-move label uses depth=8, so depth=4
        # here is enough to spot "you missed a much better move."
        best_eval_before = None
        if is_user_move:
            with ENGINE_LOCK:
                bl = best_line(ENGINE, board, depth=4, multipv=1, plies=1)
                if bl:
                    best_eval_before = bl[0][1]  # score from user's POV (board.turn == user_color)

        board.push(move)
        with ENGINE_LOCK:
            curr_cp = eval_cp(ENGINE, board, user_color, depth=8)

        if is_user_move:
            move_number += 1
            delta = curr_cp - prev_cp
            cls = classify_move(prev_cp, curr_cp, best_eval_before, rating=user_rating)
            clock_after = node.clock()
            tp_flag = under_time_pressure(clock_after, base_s)
            time_spent = None
            if clock_after is not None and prev_user_clock is not None:
                time_spent = max(0.0, prev_user_clock - clock_after + (inc_s or 0))
            if clock_after is not None:
                prev_user_clock = clock_after
            move_history.append({
                "san": san,
                "label": cls["label"],
                "severity": cls["severity"],                # base label, for filtering
                "missed_opportunity": cls["missed_opportunity"],
                "missed_win": cls["missed_win"],
                "cp_delta": round(delta),
                "move_number": move_number,
                "under_time_pressure": tp_flag,
                "time_spent_s": round(time_spent, 1) if time_spent is not None else None,
                "clock_after_s": round(clock_after, 1) if clock_after is not None else None,
            })

        prev_cp = curr_cp

    if not move_history:
        return {"ok": False, "error": "Could not extract moves from game"}

    # Run Gemini weakness analysis
    analysis = {}
    if analyze_game is not None:
        try:
            analysis = analyze_game(move_history)
        except Exception as e:
            analysis = {"error": str(e)}

    white_info = last_game.get("white", {})
    black_info = last_game.get("black", {})
    user_result = white_info.get("result") if user_color == chess.WHITE else black_info.get("result")

    return {
        "ok": True,
        "moves": move_history,
        "game_info": {
            "white": white_info.get("username"),
            "black": black_info.get("username"),
            "white_rating": white_info.get("rating"),
            "black_rating": black_info.get("rating"),
            "user_color": "white" if user_color == chess.WHITE else "black",
            "result": user_result,
            "time_class": last_game.get("time_class"),
            "url": last_game.get("url"),
        },
        **analysis,
    }

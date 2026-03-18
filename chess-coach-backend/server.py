# server.py: bridge between your chess logic (Python + Stockfish + helper functions) and the UI (React or any other client).
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import chess
import time
import logging
import os
from pathlib import Path
from dotenv import load_dotenv

# Ensure .env is loaded when running without run-dev.sh (from backend folder)
_ENV_PATH = Path(__file__).with_name('.env')
# Allow .env to override any inherited env to avoid stale shells during dev
load_dotenv(dotenv_path=_ENV_PATH, override=True)

from helper_functions import (
    open_engine,
    eval_cp,
    label_delta,
    best_line,
    san_line,
    humanish_reply,
)
try:
    from gemini_client import summarize_move, clarify_move  # prefer Gemini if available
    SUMMARIZER_PROVIDER = "gemini"
except Exception:  # fallback to OpenAI if Gemini is not importable
    try:
        from openai_client import summarize_move  # no clarify in OpenAI client
        SUMMARIZER_PROVIDER = "openai"
    except Exception:
        summarize_move = None  # type: ignore
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
app.add_middleware(
    CORSMiddleware,
    # Allow common dev origins (localhost and 127.0.0.1)
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ENGINE = open_engine()  # reuse one engine instance

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
    lines = best_line(ENGINE, board, plies=4)
    if not lines:
        if board.is_checkmate(): return {"idea": "Checkmate — no moves left!"}
        if board.is_stalemate(): return {"idea": "Stalemate — it’s a draw."}
        return {"idea": None}
    return {"idea": san_line(board, lines[0][0])}



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

    # 1) score user's move (White POV like your CLI)
    t = time.perf_counter()
    before_cp = eval_cp(ENGINE, board, chess.WHITE)
    _log_duration("play.before_cp", t)
    user_san = board.san(user_move)  # SAN must be before push
    board.push(user_move)
    t = time.perf_counter()
    after_cp = eval_cp(ENGINE, board, chess.WHITE)
    _log_duration("play.after_cp", t)
    # CP after user's move from both POVs
    cp_after_user_white = after_cp
    t = time.perf_counter()
    cp_after_user_black = eval_cp(ENGINE, board, chess.BLACK)
    _log_duration("play.cp_after_user_black", t)
    delta = after_cp - before_cp
    tag = label_delta(delta)

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
                best_lines = best_line(ENGINE, board_before, multipv=1, plies=6)
                if best_lines:
                    best_moves = best_lines[0][0]
                    best_move = best_moves[0]
                    best_san = board_before.san(best_move)
                    best_uci = best_move.uci()
                    tmp_best = board_before.copy(); tmp_best.push(best_move)
                    best_eval_cp = eval_cp(ENGINE, tmp_best, chess.WHITE)
                    pv_best_san = san_line(board_before, best_moves)
                else:
                    best_san = best_uci = pv_best_san = None
                    best_eval_cp = None

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
    # CP after coach reply from both POVs
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
            best_lines = best_line(ENGINE, board_before, multipv=1, plies=6)
            if best_lines:
                best_moves = best_lines[0][0]
                best_move = best_moves[0]
                best_san = board_before.san(best_move)
                best_uci = best_move.uci()
                tmp_best = board_before.copy(); tmp_best.push(best_move)
                best_eval_cp = eval_cp(ENGINE, tmp_best, chess.WHITE)
                pv_best_san = san_line(board_before, best_moves)
            else:
                best_san = best_uci = pv_best_san = None
                best_eval_cp = None

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

    # Compute engine data (mirror of /play's summary section)
    # Use a lighter eval depth for summary speed
    t = time.perf_counter()
    before_cp = eval_cp(ENGINE, board_before, chess.WHITE, depth=6)
    _log_duration("summ.before_cp", t)
    user_san = board_before.san(user_move)
    board_after = board_before.copy()
    board_after.push(user_move)
    t = time.perf_counter()
    after_cp = eval_cp(ENGINE, board_after, chess.WHITE, depth=6)
    _log_duration("summ.after_cp", t)
    delta = after_cp - before_cp
    # Prefer the label already computed by /play at full depth to avoid depth mismatch
    tag = body.label if body.label else label_delta(delta)

    # Use a lighter search for speed when summarizing
    t = time.perf_counter()
    best_lines = best_line(ENGINE, board_before, depth=6, multipv=1, plies=4)
    _log_duration("summ.best_line_pre", t)
    if best_lines:
        best_moves = best_lines[0][0]
        best_move = best_moves[0]
        best_san = board_before.san(best_move)
        best_uci = best_move.uci()
        # Use score from best_line to avoid re-evaluating after best move
        best_eval_cp = best_lines[0][1]
        pv_best_san = san_line(board_before, best_moves)
    else:
        best_san = best_uci = pv_best_san = None
        best_eval_cp = None

    t = time.perf_counter()
    played_line = best_line(ENGINE, board_after, depth=6, multipv=1, plies=4)
    _log_duration("summ.best_line_post", t)
    pv_played_san = user_san + (" " + san_line(board_after, played_line[0][0]) if played_line else "")

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
                "verdict": "good",
                "summary": "Solid move — evaluation did not drop.",
            },
        }
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

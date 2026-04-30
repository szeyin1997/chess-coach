import os
import json
import time
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_RATE_LIMIT_KEYWORDS = ("429", "rate limit", "quota", "resource exhausted", "resourceexhausted", "too many requests")

def _with_retry(fn, retries: int = 3, base_delay: float = 5.0):
    """Call fn(), retrying up to `retries` times on Gemini rate-limit errors."""
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            err = str(e).lower()
            is_rate_limit = any(kw in err for kw in _RATE_LIMIT_KEYWORDS)
            if is_rate_limit and attempt < retries - 1:
                wait = base_delay * (2 ** attempt)  # 5s, 10s, 20s
                logger.warning("Gemini rate limit hit, retrying in %.0fs (attempt %d/%d)", wait, attempt + 1, retries)
                time.sleep(wait)
            else:
                raise

try:
    from google import genai  # type: ignore
except Exception:  # pragma: no cover
    genai = None

from chess_principles import principles_for_move

_client = None


def get_gemini() -> Any:
    """Return a cached Gemini client using GEMINI_API_KEY.

    Raises a RuntimeError if the key is not set or the SDK is missing.
    """
    global _client
    if _client is not None:
        return _client
    if genai is None:
        raise RuntimeError("google-genai SDK not installed. Add 'google-genai' to requirements.txt and install.")
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set in environment")
    _client = genai.Client(api_key=key)
    return _client


def summarize_move(data: Dict[str, Any], model: Optional[str] = None, temperature: float = 0.4) -> Dict[str, Any]:
    """Call Gemini to summarize a move using engine data plus chess knowledge.

    Expects keys:
      fen, san, uci, eval_before_cp, eval_after_cp,
      best_san, best_uci, best_eval_cp, pv_best_san, pv_played_san
    """
    client = get_gemini()
    model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    eval_before = data.get('eval_before_cp', 0)
    eval_after = data.get('eval_after_cp', 0)
    delta = (eval_after or 0) - (eval_before or 0)

    # Give the LLM a human-readable magnitude hint so it calibrates severity correctly
    if abs(delta) < 30:
        severity = "minor inaccuracy (less than 0.3 pawns lost)"
    elif abs(delta) < 100:
        severity = "inaccuracy (~{:.1f} pawns lost)".format(abs(delta) / 100)
    elif abs(delta) < 250:
        severity = "mistake (~{:.1f} pawns lost)".format(abs(delta) / 100)
    else:
        severity = "blunder (~{:.1f} pawns lost)".format(abs(delta) / 100)

    relevant_principles = principles_for_move(data)

    prompt = """\
You are an encouraging chess coach explaining a move to an improving player.

{principles}

ENGINE DATA (ground truth — use these moves and evals exactly):
  FEN before move : {fen}
  Move played     : {san} ({uci})
  Eval before     : {eval_before} cp  (positive = White is better)
  Eval after      : {eval_after} cp
  Severity        : {severity}
  Engine best move: {best_san} ({best_uci}), eval {best_eval} cp
  PV after best   : {pv_best}
  PV after played : {pv_played}

TASK:
1. Read the FEN and the two PV lines carefully.
2. Identify which chess principle above was violated (or upheld). Name it explicitly.
3. In "summary": state the principle broken and its consequence in 1-2 plain sentences.
   BAD example: "Bxf5 gives up your bishop for a knight and lets Black develop with tempo."
   GOOD example: "Bxf5 trades an active bishop for a passive knight — an unfair exchange that hands Black a free developing move and leaves you without the bishop pair."
4. In "reasons": one bullet per principle violated. Be concrete — name the piece, square, or pawn.
5. In "what_next": 1-2 actionable rules the player should apply next time (phrased as a principle, not just "don't do that").
6. If bad, fill "if_bad_fix": name the missed idea as a principle, explain why the engine move upholds it.
   If good, set "if_bad_fix" to null.

Respond ONLY with JSON (no markdown fences, no extra text):
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
        fen=data.get('fen'),
        san=data.get('san'),
        uci=data.get('uci'),
        eval_before=eval_before,
        eval_after=eval_after,
        severity=severity,
        best_san=data.get('best_san', '?'),
        best_uci=data.get('best_uci', '?'),
        best_eval=data.get('best_eval_cp', '?'),
        pv_best=data.get('pv_best_san', '(none)'),
        pv_played=data.get('pv_played_san', '(none)'),
        principles=relevant_principles,
    )

    try:
        # Newer google-genai clients accept `config` (not `generation_config`).
        resp = _with_retry(lambda: client.models.generate_content(
            model=model,
            contents=prompt,
            config={
                "temperature": float(temperature),
                "response_mime_type": "application/json",
            },
        ))
        text = getattr(resp, "text", None) or getattr(resp, "candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text")
        content = text or "{}"
        return json.loads(content)
    except Exception as e:
        # Surface as an error so callers can fallback cleanly (no UI leak of raw error text)
        raise RuntimeError(f"Gemini error: {e}")


def analyze_game(move_history: list, model: Optional[str] = None, temperature: float = 0.3) -> Dict[str, Any]:
    """Analyze a completed game and identify the player's main weakness theme.

    move_history: list of dicts with keys: san, label, cp_delta
    Returns: { weakness_summary, themes, explanation }
    """
    client = get_gemini()
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
        resp = _with_retry(lambda: client.models.generate_content(
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


def clarify_move(summary_json: Dict[str, Any], question: str, model: Optional[str] = None, temperature: float = 0.4) -> str:
    """Ask Gemini to clarify the previously returned summary.

    Returns plain text suitable for inline display.
    """
    client = get_gemini()
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
        resp = _with_retry(lambda: client.models.generate_content(
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

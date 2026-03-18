import os
import json
from typing import Any, Dict, Optional

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
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config={
                "temperature": float(temperature),
                "response_mime_type": "application/json",
            },
        )
        text = getattr(resp, "text", None) or getattr(resp, "candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text")
        content = text or "{}"
        return json.loads(content)
    except Exception as e:
        # Surface as an error so callers can fallback cleanly (no UI leak of raw error text)
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
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config={
                "temperature": float(temperature),
                "response_mime_type": "text/plain",
            },
        )
        text = getattr(resp, "text", None) or ""
        return text.strip()
    except Exception as e:
        raise RuntimeError(f"Gemini error: {e}")

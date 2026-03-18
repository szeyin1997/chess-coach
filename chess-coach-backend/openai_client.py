import os
import json
from typing import Any, Dict, Optional

try:
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover
    OpenAI = None  # Allow import even if dependency not installed yet

_client = None


def get_openai() -> Any:
    """Return a cached OpenAI client using OPENAI_API_KEY.

    Raises a RuntimeError if the key is not set or the SDK is missing.
    """
    global _client
    if _client is not None:
        return _client
    if OpenAI is None:
        raise RuntimeError("openai SDK not installed. Add 'openai>=1.0.0' to requirements.txt and install.")
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set in environment")
    _client = OpenAI(api_key=key)
    return _client


def summarize_move(data: Dict[str, Any], model: Optional[str] = None, temperature: float = 0.2) -> Dict[str, Any]:
    """Call OpenAI to summarize a move using only provided engine data.

    Expects keys:
      fen, san, uci, eval_before_cp, eval_after_cp,
      best_san, best_uci, best_eval_cp, pv_best_san, pv_played_san
    """
    client = get_openai()
    model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    system_msg = (
        "You are a chess coach. Use ONLY the engine data provided. "
        "Do not invent moves or lines. Explain the move for the player to move in this position."
    )

    prompt = (
        "DATA:\n"
        f"FEN: {data.get('fen')}\n"
        f"Move played: {data.get('san')} ({data.get('uci')})\n"
        f"Eval before: {data.get('eval_before_cp')} cp\n"
        f"Eval after: {data.get('eval_after_cp')} cp\n"
        f"Engine best: {data.get('best_san')} ({data.get('best_uci')}) with {data.get('best_eval_cp')} cp\n"
        f"PV(best): {data.get('pv_best_san')}\n"
        f"PV(move played): {data.get('pv_played_san')}\n\n"
        "Output JSON:\n"
        "{\n"
        "  \"verdict\": \"good|ok|bad\",\n"
        "  \"summary\": \"...\",\n"
        "  \"reasons\": [\"...\", \"...\"],\n"
        "  \"what_next\": [\"...\", \"...\"],\n"
        "  \"if_bad_fix\": {\n"
        "     \"missed_idea\": \"...\",\n"
        "     \"best_move\": \"...\",\n"
        "     \"why_best\": \"...\"\n"
        "  }\n"
        "}"
    )

    resp = client.chat.completions.create(
        model=model,
        temperature=temperature,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": prompt},
        ],
    )

    content = resp.choices[0].message.content if resp and resp.choices else "{}"
    try:
        return json.loads(content)
    except Exception:
        # If model didn't return JSON, wrap as text
        return {"summary_text": content}


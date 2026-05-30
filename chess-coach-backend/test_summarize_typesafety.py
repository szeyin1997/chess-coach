"""Type-contract tests for _validate_summarize_result.

The summarize LLM call uses response_mime_type=json but NO response_schema, so
field types are not guaranteed. The frontend assumes summary:str,
what_next:list[str], if_bad_fix:{missed_idea,best_move,why_best}:str. When the
LLM returns an off-shape value (what_next as a string, best_move as an object),
the bad shape reached React and threw during render ("what_next.map is not a
function" / "objects are not valid as a React child") — blanking the page,
since there is no ErrorBoundary.

The single shared validator must coerce the result to the contract so neither
/summarize nor /summarize-batch can ever emit a shape that crashes the client.
"""
from gemini_client import _validate_summarize_result

CTX = {
    "fens": ["rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"],
    "facts": {"best_move_desc": "Nf3", "opponent_reply_desc": "e5 is a quiet move"},
    "best_san": "Nf3",
    "fallback_summary": "Engine best was Nf3.",
}


def _validate(result):
    # fresh ctx each call (validator may mutate)
    return _validate_summarize_result(result, dict(CTX))


def test_what_next_as_string_becomes_list():
    r = _validate({"summary": "Nf3 develops and hits e5.",
                   "what_next": "Develop your knights toward f3 and the center on e4."})
    assert isinstance(r["what_next"], list)
    assert all(isinstance(w, str) for w in r["what_next"])


def test_what_next_list_with_object_items_does_not_crash():
    # Previously raised TypeError inside the validator.
    r = _validate({"summary": "Nf3 develops and hits e5.",
                   "what_next": [{"tip": "Develop knights."}, "Castle early to safety on g1."]})
    assert isinstance(r["what_next"], list)
    assert all(isinstance(w, str) for w in r["what_next"])


def test_if_bad_fix_object_fields_never_reach_output():
    r = _validate({"summary": "Nf3 develops, hitting e5.",
                   "if_bad_fix": {"best_move": {"san": "Nf3"},
                                  "why_best": "Nf3 develops and controls e5.",
                                  "missed_idea": "Develop with Nf3 before e4."}})
    fix = r.get("if_bad_fix") or {}
    for k in ("missed_idea", "best_move", "why_best"):
        assert not isinstance(fix.get(k), (dict, list)), f"{k} is still a non-string"


def test_summary_as_object_falls_back_to_string():
    r = _validate({"summary": {"text": "Nf3 develops."}, "what_next": ["Develop knights to f3 and c3."]})
    assert isinstance(r["summary"], str)


def test_valid_result_passes_through():
    r = _validate({"summary": "Nf3 develops and contests e5.",
                   "what_next": ["Develop knights to f3 and c3.", "Castle to g1 for king safety."],
                   "if_bad_fix": {"best_move": "Nf3", "why_best": "Nf3 develops and controls e5.",
                                  "missed_idea": "Develop a piece before pushing pawns."}})
    assert isinstance(r["summary"], str) and r["summary"]
    assert isinstance(r["what_next"], list) and len(r["what_next"]) == 2
    assert r["if_bad_fix"]["best_move"] == "Nf3"

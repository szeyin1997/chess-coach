#!/usr/bin/env python3
"""Accuracy eval for `classify_move` — the DIAGNOSIS layer.

Compares classify_move's output against a hand-labelled golden set of CORRECT
diagnoses. No Gemini, no server — just Stockfish + the classifier — so it's free
to run and fully deterministic-ish. A mismatch means the label OR the code is
wrong; investigate, don't assume the code is right.

Runs at the multi-game review depth (default 8; override with EVAL_DEPTH). Prints
the raw signal (cp delta, win%, both severity arms) for every case so a mismatch
shows you WHY, not just that it failed.

Usage:  python evals/run_classification_eval.py
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))

import chess
from classification_golden import GOLDEN
from helper_functions import open_engine, eval_cp, best_line, classify_move

DEPTH = int(os.getenv("EVAL_DEPTH", "8"))
FIELDS = ["severity", "missed_opportunity", "missed_win"]


def position_for(case):
    if "fen" in case:
        board = chess.Board(case["fen"])
    else:
        board = chess.Board()
        for san in case.get("setup", []):
            board.push_san(san)
    move = board.parse_san(case["move"])
    return board, board.turn, move


def classify_case(engine, case):
    board, color, move = position_for(case)
    before = eval_cp(engine, board, color, depth=DEPTH)
    bl = best_line(engine, board, depth=DEPTH, multipv=1, plies=1)
    best_eval = bl[0][1] if bl else None
    after_board = board.copy()
    after_board.push(move)
    after = eval_cp(engine, after_board, color, depth=DEPTH)
    return classify_move(before, after, best_eval, rating=case.get("rating"))


def main():
    engine = open_engine()
    try:
        total = passed = 0
        fails = []
        print(f"Classification accuracy eval (Stockfish depth {DEPTH})")
        print("=" * 64)
        for case in GOLDEN:
            try:
                cls = classify_case(engine, case)
            except Exception as e:
                print(f"\n✗ {case['id']}: could not evaluate ({e})")
                fails.append(case["id"]); total += 1
                continue
            exp = case["expect"]
            checks = {f: (cls.get(f) == exp[f]) for f in FIELDS if f in exp}
            ok = all(checks.values())
            total += 1; passed += int(ok)
            print(f"\n{'✓' if ok else '✗'} {case['id']}  (rating {case.get('rating')})")
            for f in FIELDS:
                if f in exp:
                    flag = "" if checks[f] else "   ← MISMATCH"
                    print(f"    {f:20} expected={str(exp[f]):8} actual={str(cls.get(f)):8}{flag}")
            print(f"    raw: cp {cls['cp_delta']:+d}  win {cls['win_before']}%→{cls['win_after']}%  "
                  f"best_win={cls['win_best']}  | cp_arm={cls['cp_severity']} win_arm={cls['win_severity']} "
                  f"label={cls['label']!r}")
            if not ok:
                fails.append(case["id"])
                print(f"    correct label because: {case['why']}")
        print("\n" + "=" * 64)
        print(f"Passed {passed}/{total}.   Failing: {fails or 'none'}")
        if fails:
            print("A failure = the code's diagnosis disagrees with the hand-verified "
                  "correct label. Check the raw signal above to see which is wrong.")
    finally:
        engine.quit()


if __name__ == "__main__":
    main()

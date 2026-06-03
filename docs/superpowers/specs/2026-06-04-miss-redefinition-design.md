# Redefine the "Miss" classification

**Date:** 2026-06-04
**Status:** Approved (design)

## Problem

`classify_move` flags `missed_opportunity` (rendered as the "Miss" tag, e.g.
`Blunder + Miss`) whenever a clearly-winning move existed and the played move
dropped winning chances by ≥15% (`win%(best) − win%(after) ≥ 15 and best_eval ≥
100`). That fires on *any* unplayed winning move — including quiet/defensive ones
— so almost every blunder from a winning position also gets "+ Miss". The tag
then reads as "you missed a tactic" when it really means "you let an advantage
slip", and it's redundant on top of a Blunder.

Example (grandmastergibbsy vs yiinny, move 19): `e5` is a Blunder that hangs the
c4 knight; the best move `Qc5` merely *defends* it. There is no tactic to miss,
yet it's labelled `Blunder + Miss`.

## Decision (from brainstorming)

1. **"Miss" = the player passed up a *concrete winning shot*.** A concrete
   winning shot is the engine's best move being either:
   - a **forced mate**, or
   - a **material-winning capture** — it captures an enemy piece and nets
     material (simplified SEE: the target is undefended, or the exchange wins ≥ a
     minor piece's worth).

   …and the played move didn't achieve it (`best_eval − eval_after ≥ 100cp`).

   This *replaces* the win%-delta trigger.

2. **Label composition is unchanged** (user chose "keep the composite"):
   - missed shot **and still winning** (`win_after ≥ WINNING_AFTER_THRESHOLD`) →
     `Missed Win`
   - missed shot **and not winning** → `Blunder + Miss` / `Mistake + Miss`
   - no missed shot → plain `Blunder` / `Mistake` / `Inaccuracy` / `Good`

3. `missed_opportunity` (and `missed_win`) stay in the returned dict for
   filtering and the Threat Drill. Only the *trigger* changes.

## Implementation shape

- New pure helper in `helper_functions.py`:
  `best_is_winning_shot(board_before, best_san, best_eval_cp) -> bool`
  — forced-mate check (`best_eval_cp >= MATE_THRESHOLD`) OR material-winning
  capture (reuse the simplified-SEE hanging logic: captured value minus cheapest
  recapture ≥ `MISS_MATERIAL_MIN`).
- `classify_move(..., best_is_winning_shot: Optional[bool] = None)`:
  `missed_opportunity = bool(best_is_winning_shot) and (best_eval_cp - eval_after_cp) >= MISS_MARGIN`.
  When `best_is_winning_shot` is None (eval-only callers, e.g. CLI), no Miss — no crash.
- Callers that have the board + engine line (the chess.com analysis endpoints,
  `/play`, `/evaluate-move`, `/summarize`) compute and pass the flag.

## Tunable constants
- `MISS_MATERIAL_MIN = 2` (a minor piece — missing a free *pawn* is not a Miss)
- `MISS_MARGIN = 100` (cp; confirms the played move wasn't the shot or its equal)
- `MATE_THRESHOLD = 10000` (mate sentinel is 100000)

## Game impact (verification targets)
| Move | Best | Old | New |
|---|---|---|---|
| 6 e6 | e5 (quiet) | Mistake + Miss | Mistake |
| 19 e5 | Qc5 (defends) | Blunder + Miss | Blunder |
| 20 O-O | Qc5 (defends) | Blunder + Miss | Blunder |
| 21 Rad8 | Qc5+ (check) | Blunder + Miss | Blunder |
| 22 Rde8 | Qc5+ (check) | Blunder + Miss | Blunder |
| 27 Rxf8 | Kxf8 (wins B) | Mistake + Miss | Mistake + Miss |
| 29 Rf7 | Bxe6 (wins Q) | Blunder + Miss | Blunder + Miss |
| 12 Bd6 (other game) | Bb4+ (forced mate) | Missed Win | Missed Win |

## Invariants preserved
- The legal-move guarantee (`best_move` = engine move; citation validation) is untouched.
- `rating=None` and eval-only callers keep working (no Miss without the flag).

## Tests (TDD)
- Missed material-winning capture (move 29 / 27 shape) → Miss.
- Missed forced mate, still winning → Missed Win.
- Best move is quiet/defensive (move 19) → NOT a Miss.
- Best move is a check that wins nothing → NOT a Miss.
- Missing a free *pawn* → NOT a Miss (below material threshold).
- Best move is a capture that's only an even trade → NOT a Miss.

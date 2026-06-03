# Chess Coach — Notes for Claude

AI-powered chess improvement tool. Stockfish does the analysis; Gemini explains it.
See `README.md` for the product story.

## Run

```bash
bash run-dev.sh
# Backend:  http://127.0.0.1:8000  (FastAPI, --reload enabled)
# Frontend: http://127.0.0.1:5173  (Vite)
```

Stockfish must be installed (`brew install stockfish`). Path is in `chess-coach-backend/config.py`.

## Layout

- `chess-coach-backend/server.py` — FastAPI routes (`/play`, `/summarize`, `/evaluate-move`, `/import-chessdotcom`, `/analyze-chessdotcom`)
- `chess-coach-backend/helper_functions.py` — Stockfish wrappers (`eval_cp`, `best_line`) and the unified `classify_move`
- `chess-coach-backend/gemini_client.py` — All Gemini prompts. Multi-key failover lives here.
- `chess-coach-backend/chess_principles.py` — Static dictionary of tactical patterns and chess principles fed to the LLM
- `chess-coach-frontend/src/App.jsx` — Monolithic React app (game board, review UI, puzzle trainer)

## Source-of-truth conventions

**Move classification: `classify_move(before, after, best_eval=None, rating=None)` in `helper_functions.py`.**
Single function used by every endpoint. Takes the WORSE of two methods:
- cp-delta thresholds: -50 / -100 / -300 → Good/Inaccuracy/Mistake/Blunder
- win%-delta thresholds (Lichess): 10% / 20% / 30%

Returns `{label, severity, cp_severity, win_severity, missed_opportunity, missed_win, created_problem, rating, rating_factor, ...}`.
**Filter on `severity` (base label), not `label` (which can be composite like `"Mistake + Miss"`).**

**`Missed Win` (mirrors Chess.com's "Miss").** When a move `missed_opportunity` AND the
player is still clearly winning afterward (`win_after ≥ WINNING_AFTER_THRESHOLD = 70%`), it
is labelled `"Missed Win"` and its `severity` drops to the **win% arm** (the honest practical
damage) instead of the cp arm. Why: a forced mate is stored as the `mate_score=100000`
sentinel, so the cp-delta arm reads "mate → still winning" as a ~−100000 drop and brands
*every* declined mate a Blunder regardless of how winning you remain — a sentinel doing
arithmetic. The `+ Miss` tag already says "you missed a win"; stacking Blunder double-counts.
A move that throws the win away (drops below 70%) is NOT a Missed Win — it stays a real
`Blunder/Mistake + Miss`. So a Missed Win never inflates the blunder count but is still flagged
for review/drill (via `missed_opportunity`).

**Declined-mate detection (don't remove).** `missed_opportunity` fires on a ≥15% best-vs-played
win-gap — but that gate CANNOT catch a declined forced mate, because win% saturates (mate=100%
vs still-winning ~91% is only ~9%, under 15%). So there's a second branch: if `best_eval ≥
MATE_CP_SENTINEL` (9000; a mate sentinel, not real material), the played move isn't itself a
mate (`eval_after < MATE_CP_SENTINEL`), and `win_after ≥ 70%`, it's a Missed Win. Without this,
declining a mate while up a rook is mislabelled **Blunder** (the cp arm's −99367 sentinel wins).
This was caught by `evals/run_classification_eval.py` (the `declined_mate_still_winning` case).

**Rating-aware (research-backed, mirrors Chess.com's rating-dependent classifier).** When
`rating` is supplied, the win%-delta thresholds AND the lower cp-delta boundaries
(Good/Inaccuracy, Inaccuracy/Mistake) are scaled by `_rating_leniency_factor` —
`1.0 + (1200 − rating)/2000`, clamped [0.8, 1.5]. Gentler for beginners, stricter for
strong players. `rating=None` reproduces the old rating-blind behavior exactly, so the
single-move endpoints (`/play`, `/summarize`, `/evaluate-move`) are unaffected; only the
chess.com analysis endpoints pass the user's per-game rating.
- **INVARIANT — never break:** the cp-delta **Mistake/Blunder floor (−300) is NOT scaled**.
  A material drop ≥300cp is a Blunder at every rating, so a beginner's hung piece is never
  softened away. Leniency only moves the Inaccuracy↔Mistake line. See `RESEARCH.md`.
- Known calibration quirk: the win%-delta arm currently never out-ranks the cp-delta arm
  over realistic evals (the cp arm always dominates) — so today rating-awareness bites via
  the cp boundaries. Don't "fix" this by scaling the −300 floor.

**Eval POV: always pass the player's color** (`board.turn` before the move is played).
Evaluating from `chess.WHITE` regardless of who moved was a real bug that inverted win% for Black players.

## Stockfish depth choices (per endpoint)

| Endpoint | Depth | Why |
|---|---|---|
| `/play` | default (ANALYZE) | Real-time, must feel instant |
| `/import-chessdotcom`, `/analyze-chessdotcom` | 8 | Per-move across whole games — keep batch fast |
| `/evaluate-move` | 8 | Per-click in Try mode |
| `/summarize` | **19** | On-demand single-position; ~1.2s acceptable for "explain this move" UX. Higher depth catches deep tactics (e.g. Bxh2+ sacs) that depth 14 missed. **Always pair with `engine.configure({"Clear Hash": True})`** before the search or repeated calls return different "best" moves due to TT pollution. |

Chess.com Game Review (free) uses Stockfish 16 ~1s. We use Stockfish 17.1 full NNUE — already stronger per node.

## Gemini API

- Primary key: `GEMINI_API_KEY` (required)
- Fallback key: `GEMINI_API_KEY_FALLBACK` (optional spare for daily-quota failover)
- Free tier: 20 requests/day per key
- `_with_retry` in `gemini_client.py` handles: rate-limit retry (exponential), 503/transient retry, daily-quota failover across keys, friendly user-facing error when all keys exhaust

All prompts route through `_with_retry(lambda c: c.models.generate_content(...))`. Never call `client.models.generate_content` directly — you'd lose failover.

## Move-explanation prompt: single source of truth

**`summarize_move_batch` is THE explanation path.** `summarize_move(data)` is a thin wrapper that calls it with a one-item list, so the prompt, rule-set, output schema, and validator (`_validate_summarize_result`) exist exactly once.
- Don't reintroduce a second standalone prompt string in `summarize_move`. They used to be two hand-synced copies that drifted (batch grew stricter anti-hand-wave rules the single version never got), which made the same move read differently per endpoint.
- The per-position facts block is built by `_summarize_item_block` (framing + tactical pattern + selected `chess_principles` + verified facts). Both paths use it.
- **Engine severity is the sole judgment.** The LLM no longer emits a `verdict` field — the `classify_move` badge is shown everywhere. Don't ask the LLM to re-rate move quality.
- **Schema = what the UI renders**: `summary`, `what_next`, `if_bad_fix.{missed_idea, best_move, why_best}`. Don't add fields (e.g. the old unrendered `reasons`) without a place to show them.
- **Temperature is 0.15** — this is a fact-grounded task, not creative writing. Higher temps reintroduce the "re-clicking gives different wording" inconsistency.
- **Advice is rating-calibrated.** When `data["rating"]` is present, `_level_guidance(rating)` injects a PLAYER LEVEL block (beginner <800 / improver <1400 / intermediate+) that tells the LLM how deep to pitch every field — sub-800 gets piece-safety + one-move tactics and is told to AVOID positional/opening/endgame theory. `rating=None` → generic advice (Play/Try modes). Review mode forwards `player_rating`. Bands are tunable in `_level_guidance`. Note: `summaryCache` is keyed on `fen::uci` only, so cached advice doesn't vary if a player's rating later changes — acceptable (rating is stable within a review session).
- **CCT is taught as a cheap "Is it safe?" check, not an every-move scan.** The beginner band (and `generate_player_summary`) teach Heisman's one-question safety check on the *chosen* move, not a full Checks/Captures/Threats board scan every move (too slow for a beginner — they won't do it). Don't revert either to "scan every check/capture/threat every move."

## Clock-aware blunder diagnosis

Both chess.com analysis loops iterate **PGN nodes** (`pgn_game.mainline()`, not `mainline_moves()`) so `node.clock()` is available. `parse_time_control` + `under_time_pressure` (in `helper_functions.py`) tag each user move with `under_time_pressure` (bool|None — **None when the PGN had no clock data**, never assume), `time_spent_s`, `clock_after_s`. `player_stats` aggregates serious errors (Mistake/Blunder) **that carried clock data** into `errors_time_pressure` / `errors_with_time` / `errors_clock_known`. `generate_player_summary` turns that into a TIME PROFILE that routes advice: time-pressure-driven → slower time controls + time management; recognition-driven → safety-check habit + puzzles; unknown → recognition default. **Invariant:** time pressure changes the *advice* only — never the badge (a low-clock blunder is still a Blunder), same principle as rating-awareness.

## Things not to undo

- Don't reintroduce `label_delta` or `label_move`. They were band-aids that disagreed with `classify_move` and produced contradictory UI labels vs LLM verdicts.
- Don't filter LLM-side on exact label strings (`label == "Mistake"`) — use `severity` or `missed_opportunity`.
- Don't drop the `Clear Hash` call before `/summarize` engine work — non-determinism comes back immediately.
- Don't re-split the move-explanation prompt into single + batch copies (see above), and don't reintroduce the LLM `verdict` field.
- Don't scale the cp-delta `-300` Mistake/Blunder floor by rating. Beginners must still see hung pieces as Blunders — that's the CCT lesson. Rating-leniency only adjusts the Inaccuracy↔Mistake band.

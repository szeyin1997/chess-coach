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

**Move classification: `classify_move(before, after, best_eval=None)` in `helper_functions.py`.**
Single function used by every endpoint. Takes the WORSE of two methods:
- cp-delta thresholds: -50 / -100 / -300 → Good/Inaccuracy/Mistake/Blunder
- win%-delta thresholds (Lichess): 10% / 20% / 30%

Returns `{label, severity, cp_severity, win_severity, missed_opportunity, created_problem, ...}`.
**Filter on `severity` (base label), not `label` (which can be composite like `"Mistake + Miss"`).**

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

## Things not to undo

- Don't reintroduce `label_delta` or `label_move`. They were band-aids that disagreed with `classify_move` and produced contradictory UI labels vs LLM verdicts.
- Don't filter LLM-side on exact label strings (`label == "Mistake"`) — use `severity` or `missed_opportunity`.
- Don't drop the `Clear Hash` call before `/summarize` engine work — non-determinism comes back immediately.

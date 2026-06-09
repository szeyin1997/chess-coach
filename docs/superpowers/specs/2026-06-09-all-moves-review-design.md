# All-Moves Review with Positive Labels — Design

**Date:** 2026-06-09
**Scope:** Chess.com game review (3-column layout). Backend `classify_move` + `analyze_chessdotcom`. Frontend move list + ExplainPanel gating.

## Problem

The chess.com review currently shows only mistakes/blunders in the left column. Every non-error move is labelled "Good" with no further granularity. Users have no way to browse the full game or see which moves were objectively best.

## Goal

1. Show ALL moves in the game review's left column, color-coded with quality labels.
2. Subdivide the "Good" severity into three positive labels mirroring Chess.com's Expected Points model: **Best**, **Excellent**, **Good**.
3. Restrict "Analyse with Coach" to Mistake and Blunder severity moves only.

## Research: Chess.com / Stockfish labeling

Chess.com uses an Expected Points (win probability) model. Positive label thresholds:

| Label | Win% loss (expected points) | Our mapping |
|---|---|---|
| Best | 0.00 | Played the engine's top move (UCI match) |
| Excellent | 0.00–0.02 | Win% loss < 2%, not the top move |
| Good | 0.02–0.05 | Win% loss 2–5% (our current "Good" upper band) |

Negative labels (unchanged): Inaccuracy (5–10%), Mistake (10–20%), Blunder (20%+).

Brilliant and Great Move are out of scope for this version (require piece-sacrifice detection and position-turnaround heuristics).

---

## Backend changes

### `classify_move` in `helper_functions.py`

Add optional parameter `played_best: bool = False`.

When `severity == "Good"`, determine label as:
```python
if played_best:
    label = "Best"
elif win_loss < EXCELLENT_WIN_LOSS_THRESHOLD:   # 0.02
    label = "Excellent"
else:
    label = "Good"
```

`severity` remains `"Good"` for all three — no existing filter, drill, or weakness logic is affected.
`EXCELLENT_WIN_LOSS_THRESHOLD = 0.02` is a named constant (not magic number).

**Invariant:** `played_best=False` (the default) reproduces the old "Good" label exactly, so `/play`, `/summarize`, `/evaluate-move`, and `/import-chessdotcom` endpoints are unaffected.

### `analyze_chessdotcom` in `server.py`

After computing `best_san_before` (the engine's top-move SAN, already available from the multipv lookup), compare it to the played move's SAN:

```python
played_best = bool(best_san_before and san == best_san_before)
cls = classify_move(..., played_best=played_best)
```

No new engine calls. `best_san_before` is already computed for flagged-move enrichment.

**Edge case:** if `best_san_before` is `None` (engine returned no line), `played_best` defaults to `False` → label falls through to "Excellent" or "Good" based on win% loss.

---

## Frontend changes

### Move list column (`App.jsx`)

Replace the existing `badMoves`-only left column with a full game move list.

**Layout:** paired rows (1. e4 e5, 2. Nf3 Nc6…). Each half-move shows the SAN and a color-coded badge.

**Badge colors:**

| Label | Color |
|---|---|
| Best | `#4299e1` (blue) |
| Excellent | `#48bb78` (green) |
| Good | `var(--text-muted)` (grey, no badge — matches chess.com's no-annotation for solid moves) |
| Inaccuracy | `#ecc94b` (yellow) |
| Mistake | `#ed8936` (orange) |
| Blunder | `#e53e3e` (red) |
| Miss / Missed Win | existing purple |

**Interaction:** clicking any half-move calls `selectMoveForReview(move, allGameMoves)` (rename from `selectMistakeForReview` — same function, same position-step + best-move-arrow flow).

**Scroll:** the list is scrollable (`max-height`, `overflow-y: auto`) so long games don't overflow.

**Selected state:** clicked move is highlighted with `active` class (already used for mistakes).

### ExplainPanel "Analyse with Coach" gate (`App.jsx`)

The `ExplainPanel` `onAnalyze` prop is currently always passed. Change the call site to pass `null` when the selected move's severity is not "Mistake" or "Blunder":

```jsx
onAnalyze={
  reviewSelectedMove?.severity === "Mistake" || reviewSelectedMove?.severity === "Blunder"
    ? fetchReviewExplanation
    : null
}
```

`ExplainPanel` already conditionally renders the button when `onAnalyze` is null (line ~214: `if (!explanation && !bestMove && !onAnalyze) return ...`), so no changes needed inside `ExplainPanel` itself.

---

## What is NOT changing

- `severity` field values (`"Good"`, `"Inaccuracy"`, `"Mistake"`, `"Blunder"`) — all existing filtering uses `severity`.
- `/play`, `/summarize`, `/evaluate-move`, `/import-chessdotcom` endpoints — `played_best` defaults to `False`, so these return `"Good"` as before.
- Best-move arrow behaviour (built in previous session) — `selectMoveForReview` rename only, no logic change.
- Drill questions, tactical summaries, weakness analysis — all filter on `severity`, unaffected.
- `player_stats.good_moves` counter — counts `severity == "Good"`, which now includes Best/Excellent/Good labels. Counts stay consistent.
- ANALYSIS_CACHE_VERSION must be bumped (Best/Excellent labels are new fields in the cached move data).

---

## Cache version bump

`ANALYSIS_CACHE_VERSION` in `App.jsx` must be incremented (currently 10 → 11) so cached analysis with the old single "Good" label is invalidated and re-fetched with the new Best/Excellent/Good labels.

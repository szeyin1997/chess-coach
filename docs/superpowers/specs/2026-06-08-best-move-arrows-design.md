# Best-Move Arrows in Chess.com Review

**Date:** 2026-06-08
**Scope:** Chess.com game review only (3-column layout, `reviewPositions` / `reviewPosIndex`).

## Problem

The review board currently shows green square highlights for the best move at position 0 only. Navigating to later positions (after the actual move, after opponent's reply, etc.) gives no guidance on what the best play would have been at each step.

## Goal

Show a light-blue arrow on the board at every review position indicating the best move for the side to move. The arrow updates as the user steps forward and back.

## Data model

`reviewPositions[]` items gain one optional field:

```js
{ fen: string, label: string, bestMove?: { from: string, to: string } | null }
```

`null` means the `/hint` call hasn't resolved yet (arrow not shown). `undefined` means it hasn't been attempted. In practice both are treated as "no arrow."

## Population

Inside `selectMistakeForReview`, after the positions array is fully built:

1. If `reviewBestMove` is already set from the LLM cache (depth-19), copy `{from, to}` into `positions[0].bestMove` — no `/hint` call for position 0 in this case.
2. Fire parallel `/hint` calls for all remaining positions (and position 0 if not covered by step 1).
3. As each call resolves, update state immutably:
   ```js
   setReviewPositions(prev =>
     prev.map((p, i) => i === idx ? { ...p, bestMove: { from, to } } : p)
   );
   ```
4. If a `/hint` call for position 0 completes AFTER the LLM-derived `reviewBestMove` was set, discard the hint result for position 0 (same guard as the existing line-720 logic). This preserves the deeper depth-19 hint as ground truth.

Errors from individual `/hint` calls are swallowed silently — a missing arrow is fine, a crashed review is not.

## Board rendering

Replace the existing `customSquareStyles` best-move highlight block (lines ~1767-1774 in App.jsx) with a `customArrows` prop:

```jsx
customArrows={
  !tryCurrentFen && reviewPositions[reviewPosIndex]?.bestMove
    ? [[
        reviewPositions[reviewPosIndex].bestMove.from,
        reviewPositions[reviewPosIndex].bestMove.to,
        "rgb(163,213,255)"
      ]]
    : []
}
```

Color `rgb(163,213,255)` — light blue, matching the design reference image.

The `customSquareStyles` prop on this Chessboard can be removed (or kept empty) since the arrow replaces the old green-square highlight.

## Preserved behavior

- `reviewBestMove` state is untouched. `ExplainPanel` still receives it and renders the "Bishop to e4" text.
- Try-mode suppresses the arrow (`tryCurrentFen` is set).
- The "skip `/hint` if LLM cache already set" logic at line 720 is unchanged.
- No changes to the play-vs-bot review mode.

## What is NOT changing

- Arrow only appears in the chess.com 3-column review, not the play-vs-bot Prev/Next review.
- No new API endpoints. `/hint` is the existing fast Stockfish endpoint.
- No changes to backend.

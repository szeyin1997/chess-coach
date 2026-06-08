# Best-Move Arrows in Chess.com Review — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a light-blue best-move arrow on every position in the chess.com game review board, updating as the user steps forward and back.

**Architecture:** Enrich `reviewPositions[]` with a `bestMove: {from, to}` field populated by parallel `/hint` calls fired on mistake-click. Position 0 uses the LLM-cached hint when available (depth-19 ground truth); positions 1+ always use fast `/hint`. Replace the existing green square highlights at position 0 with `customArrows` on the review Chessboard.

**Tech Stack:** React (App.jsx monolith), react-chessboard `customArrows` prop, existing `/hint` FastAPI endpoint.

---

## Files

- Modify: `chess-coach-frontend/src/App.jsx`
  - `selectMistakeForReview` (~line 652): seed pos-0 bestMove from LLM cache; add parallel fetches for positions 1+; wire pos-0 bestMove into existing `/hint` path
  - Review Chessboard (~line 1755): replace `customSquareStyles` best-move block with `customArrows`

---

## Task 1: Populate `bestMove` on `reviewPositions` items

**Files:**
- Modify: `chess-coach-frontend/src/App.jsx` — `selectMistakeForReview` function (~line 652)

- [ ] **Step 1: Seed position 0's `bestMove` from LLM cache**

  Find the line that reads:
  ```js
  setReviewPositions(positions); setReviewPosIndex(0); setReviewFen(fenBefore);
  ```
  (currently line 715). Insert this block **immediately before** that line:

  ```js
  // Seed pos-0 bestMove from LLM cache so the arrow shows immediately on re-click.
  if (cached) {
    const bm0 = bestMoveFromExplanation(cached, fenBefore);
    if (bm0) positions[0] = { ...positions[0], bestMove: { from: bm0.from, to: bm0.to } };
  }
  ```

- [ ] **Step 2: Add parallel `/hint` fetches for positions 1+**

  Insert this block **immediately after** the `setReviewPositions(positions)` line:

  ```js
  // Parallel best-move hints for positions 1+ (pos 0 handled separately).
  positions.slice(1).forEach(async (pos, relIdx) => {
    const absIdx = relIdx + 1;
    try {
      const hintRes = await axios.post(`${API}/hint`, { fen: pos.fen });
      if (hintRes.data?.best_uci) {
        const hfrom = hintRes.data.best_uci.slice(0, 2);
        const hto   = hintRes.data.best_uci.slice(2, 4);
        setReviewPositions(prev =>
          prev.map((p, i) => i === absIdx ? { ...p, bestMove: { from: hfrom, to: hto } } : p)
        );
      }
    } catch {}
  });
  ```

- [ ] **Step 3: Wire pos-0 `bestMove` into the existing `/hint` path**

  Find the existing no-cache hint fetch block (currently lines 720-731):
  ```js
  if (!cached) {
    try {
      const hintRes = await axios.post(`${API}/hint`, { fen: fenBefore });
      if (hintRes.data?.best_uci) {
        const from = hintRes.data.best_uci.slice(0,2), to = hintRes.data.best_uci.slice(2,4);
        const NAMES = { p:"Pawn", n:"Knight", b:"Bishop", r:"Rook", q:"Queen", k:"King" };
        let displayText = `to ${to}`;
        try { const c = new Chess(fenBefore); const piece = c.get(from); if (piece) displayText = `${NAMES[piece.type]} to ${to}`; } catch {}
        setReviewBestMove({ from, to, displayText });
      }
    } catch {}
  }
  ```

  Replace the `setReviewBestMove({ from, to, displayText });` line so it also updates position 0:
  ```js
  setReviewBestMove({ from, to, displayText });
  setReviewPositions(prev =>
    prev.map((p, i) => i === 0 ? { ...p, bestMove: { from, to } } : p)
  );
  ```

- [ ] **Step 4: Verify no console errors on mistake-click**

  Run the dev server:
  ```bash
  bash run-dev.sh
  ```
  Open http://127.0.0.1:5173, go to Analyze My Games, load a game, click a mistake.
  Open DevTools console — expect `/hint` calls for each position (6-12 calls) with no errors.
  The board at this point still shows green squares (Task 2 replaces them).

- [ ] **Step 5: Commit**

  ```bash
  git add chess-coach-frontend/src/App.jsx
  git commit -m "Populate bestMove on reviewPositions via parallel /hint calls"
  ```

---

## Task 2: Replace square highlights with `customArrows`

**Files:**
- Modify: `chess-coach-frontend/src/App.jsx` — review Chessboard (~line 1755)

- [ ] **Step 1: Replace `customSquareStyles` with `customArrows`**

  Find the review Chessboard component (starts with `<Chessboard` around line 1755). It currently has:
  ```jsx
  customSquareStyles={(() => {
    const s = {};
    if (!tryCurrentFen && reviewPosIndex === 0 && reviewBestMove) {
      s[reviewBestMove.from] = { boxShadow:"inset 0 0 0 3px var(--green)" };
      s[reviewBestMove.to]   = { boxShadow:"inset 0 0 0 4px var(--green)", background:"rgba(72,187,120,.2)" };
    }
    return s;
  })()}
  ```

  Replace the entire `customSquareStyles` prop with `customArrows`:
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

- [ ] **Step 2: Verify the arrow renders and updates on navigation**

  With the dev server running, load a chess.com game and click a mistake.

  Check:
  - Position 0: a light-blue arrow points from the best move's origin to its destination.
  - Click → (next): arrow updates to the best move for the next position (opponent's best response).
  - Click → again: arrow updates for the following position.
  - Click ← : arrow goes back to previous position's best move.
  - Click Reset: arrow returns to position 0's arrow.
  - While in Try Mode (`tryCurrentFen` is set): no arrow shown.
  - After LLM explanation loads and you re-click the same mistake: arrow on position 0 matches the explanation's recommended move.

- [ ] **Step 3: Commit**

  ```bash
  git add chess-coach-frontend/src/App.jsx
  git commit -m "Show best-move arrows on review board at every position"
  ```

# All-Moves Review with Positive Labels — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show all user moves (not just mistakes) in the chess.com game review with color-coded Best/Excellent/Good/Inaccuracy/Mistake/Blunder labels, and restrict "Analyse with Coach" to Mistake and Blunder severity only.

**Architecture:** Approach A — enrich `classify_move`'s `label` field (leaving `severity` untouched) with positive sub-labels when a Good move is played. Backend computes `played_best` by comparing the played SAN to the engine's top move. Frontend replaces the badMoves-filtered column with a flat all-moves list and gates the coach button on `severity`.

**Tech Stack:** Python/FastAPI backend (`helper_functions.py`, `server.py`), React frontend (`App.jsx`, `index.css`). No new dependencies.

**Scale note on win_loss:** `win_percent()` returns 0–100, so `win_loss` is in percentage points. Chess.com's 0.02 EP threshold = **2.0** in this codebase's scale.

---

## Files

- Modify: `chess-coach-backend/helper_functions.py` — add `EXCELLENT_WIN_LOSS_THRESHOLD`, `played_best` param to `classify_move`
- Create: `chess-coach-backend/test_classify_move.py` — unit tests for new label behavior
- Modify: `chess-coach-backend/server.py` (~line 954) — compute and pass `played_best` in `analyze_chessdotcom`
- Modify: `chess-coach-frontend/src/index.css` — add `.badge-best`, `.badge-excellent`, move-cell colors
- Modify: `chess-coach-frontend/src/App.jsx` — `moveBadgeClass`, `MoveBadge`, cache version, all-moves list, rename, gate

---

## Task 1: Backend — add `played_best` to `classify_move`

**Files:**
- Modify: `chess-coach-backend/helper_functions.py`
- Create: `chess-coach-backend/test_classify_move.py`

- [ ] **Step 1: Write failing tests**

Create `chess-coach-backend/test_classify_move.py`:

```python
import pytest
from helper_functions import classify_move


def test_played_best_gives_best_label():
    """played_best=True → label 'Best', severity stays 'Good'."""
    result = classify_move(100, 100, played_best=True)
    assert result["label"] == "Best"
    assert result["severity"] == "Good"


def test_played_best_overrides_excellent():
    """Even with near-zero win_loss, played_best wins."""
    result = classify_move(100, 95, played_best=True)
    assert result["label"] == "Best"
    assert result["severity"] == "Good"


def test_small_win_loss_gives_excellent():
    """win_loss < 2.0 (not played_best) → label 'Excellent', severity 'Good'.
    cp 100→90: win_loss ≈ 0.9 percentage points."""
    result = classify_move(100, 90)
    assert result["label"] == "Excellent"
    assert result["severity"] == "Good"


def test_larger_win_loss_gives_good():
    """win_loss >= 2.0 but still within Good severity → label 'Good'.
    cp 100→50: win_loss ≈ 4.6 percentage points."""
    result = classify_move(100, 50)
    assert result["label"] == "Good"
    assert result["severity"] == "Good"


def test_default_played_best_false_is_backward_compatible():
    """Omitting played_best (default False) never produces 'Best' label."""
    result = classify_move(50, 48)
    assert result["label"] != "Best"
    assert result["severity"] == "Good"


def test_positive_labels_not_applied_to_miss():
    """A Good-severity move that also missed an opportunity stays 'Miss', not 'Best'."""
    # Miss fires when best_eval >> eval_after. Use a big best_eval sentinel.
    result = classify_move(100, 90, best_eval_cp=9500, best_is_winning_shot=True, played_best=True)
    # missed_opportunity fires; label should be "Miss" or a composite, NOT "Best"
    assert result["label"] != "Best"
    assert result["missed_opportunity"] is True


def test_inaccuracy_not_affected():
    """Positive label logic only fires on Good severity; Inaccuracy unchanged."""
    # win_loss ≥ 10 → Inaccuracy. cp 300→0 gives large win_loss.
    result = classify_move(300, 0, played_best=False)
    assert result["severity"] == "Inaccuracy"
    assert result["label"] == "Inaccuracy"
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd chess-coach-backend
python -m pytest test_classify_move.py -v 2>&1 | head -30
```

Expected: FAIL — most tests fail because `classify_move` doesn't accept `played_best` yet.

- [ ] **Step 3: Add `EXCELLENT_WIN_LOSS_THRESHOLD` constant to `helper_functions.py`**

Find the block of constants near line 52–64 (after `MATE_CP_SENTINEL`, before `MISS_MATERIAL_MIN`). Insert after `MISS_MARGIN = 100`:

```python
# Win% loss (percentage points, 0–100 scale) below which a Good move is labelled
# "Excellent" — mirrors Chess.com's Expected Points Model threshold of 0.02 EP.
# Moves at or above this threshold (but still Good severity) are labelled "Good".
EXCELLENT_WIN_LOSS_THRESHOLD = 2.0
```

- [ ] **Step 4: Add `played_best` parameter to `classify_move` signature**

Find the function signature at line ~260:
```python
def classify_move(eval_before_cp: int, eval_after_cp: int, best_eval_cp: Optional[int] = None,
                  rating: Optional[int] = None, best_is_winning_shot: Optional[bool] = None) -> dict:
```

Replace with:
```python
def classify_move(eval_before_cp: int, eval_after_cp: int, best_eval_cp: Optional[int] = None,
                  rating: Optional[int] = None, best_is_winning_shot: Optional[bool] = None,
                  played_best: bool = False) -> dict:
```

- [ ] **Step 5: Subdivide the "Good" label in `classify_move`**

Find the label assignment block near line 328–335 that currently reads:
```python
    if missed_win:
        label = "Missed Win"
    elif missed_opportunity and created_problem:
        label = f"{severity} + Miss"
    elif missed_opportunity:
        label = "Miss"
    else:
        label = severity
```

Replace with:
```python
    if missed_win:
        label = "Missed Win"
    elif missed_opportunity and created_problem:
        label = f"{severity} + Miss"
    elif missed_opportunity:
        label = "Miss"
    elif severity == "Good":
        if played_best:
            label = "Best"
        elif win_loss < EXCELLENT_WIN_LOSS_THRESHOLD:
            label = "Excellent"
        else:
            label = "Good"
    else:
        label = severity
```

- [ ] **Step 6: Run tests — verify they all pass**

```bash
cd chess-coach-backend
python -m pytest test_classify_move.py -v
```

Expected output:
```
test_classify_move.py::test_played_best_gives_best_label PASSED
test_classify_move.py::test_played_best_overrides_excellent PASSED
test_classify_move.py::test_small_win_loss_gives_excellent PASSED
test_classify_move.py::test_larger_win_loss_gives_good PASSED
test_classify_move.py::test_default_played_best_false_is_backward_compatible PASSED
test_classify_move.py::test_positive_labels_not_applied_to_miss PASSED
test_classify_move.py::test_inaccuracy_not_affected PASSED

7 passed
```

- [ ] **Step 7: Run full test suite to verify no regressions**

```bash
cd chess-coach-backend
python -m pytest test_attack_claim_verifier.py test_classify_move.py -v
```

Expected: all 16 tests pass (9 + 7).

- [ ] **Step 8: Commit**

```bash
git add chess-coach-backend/helper_functions.py chess-coach-backend/test_classify_move.py
git commit -m "Add Best/Excellent/Good positive labels to classify_move"
```

---

## Task 2: Backend — pass `played_best` in `analyze_chessdotcom`

**Files:**
- Modify: `chess-coach-backend/server.py` (~line 953)

- [ ] **Step 1: Find the `classify_move` call in `analyze_chessdotcom`**

In `server.py`, search for the call (currently around line 954):
```python
                cls = classify_move(prev_cp, curr_cp, best_eval_before, rating=user_rating,
                                    best_is_winning_shot=shot)
```

It is immediately preceded by:
```python
                shot = best_is_winning_shot(chess.Board(fen_before), best_san_before, best_eval_before)
```

- [ ] **Step 2: Insert `played_best` computation and pass it**

Replace the two lines (the `shot` computation and the `classify_move` call) with:

```python
                shot = best_is_winning_shot(chess.Board(fen_before), best_san_before, best_eval_before)
                played_best_flag = bool(best_san_before and san == best_san_before)
                cls = classify_move(prev_cp, curr_cp, best_eval_before, rating=user_rating,
                                    best_is_winning_shot=shot, played_best=played_best_flag)
```

Note: use `played_best_flag` as the local variable name to avoid shadowing the parameter name if the module is ever imported.

- [ ] **Step 3: Verify the server imports and starts cleanly**

```bash
cd chess-coach-backend
python -c "import server; print('OK')"
```

Expected: `OK` (no import errors)

- [ ] **Step 4: Run tests**

```bash
cd chess-coach-backend
python -m pytest test_attack_claim_verifier.py test_classify_move.py -v 2>&1 | tail -5
```

Expected: all 16 tests pass.

- [ ] **Step 5: Commit**

```bash
git add chess-coach-backend/server.py
git commit -m "Pass played_best to classify_move in analyze_chessdotcom"
```

---

## Task 3: Frontend CSS — Best/Excellent badge and move-cell styles

**Files:**
- Modify: `chess-coach-frontend/src/index.css`

- [ ] **Step 1: Add badge classes for Best and Excellent**

Find the badge class block (around line 156–160):
```css
.badge-blunder    { background: var(--red-bg);    color: var(--red);    border: 1px solid var(--red-border); }
.badge-mistake    { background: var(--orange-bg); color: var(--orange); border: 1px solid var(--orange-border); }
.badge-inaccuracy { background: var(--yellow-bg); color: var(--yellow); border: 1px solid var(--yellow-border); }
.badge-miss       { background: #0d2e35;           color: #38bcd4;       border: 1px solid #1a5f6e; }
.badge-good       { background: var(--green-bg);   color: var(--green);  border: 1px solid var(--green-border); }
```

Insert two new lines **before** `.badge-good`:
```css
.badge-best       { background: var(--blue-bg);   color: var(--blue);   border: 1px solid var(--blue-border); }
.badge-excellent  { background: var(--green-bg);  color: var(--green);  border: 1px solid var(--green-border); }
```

- [ ] **Step 2: Add move-cell colors for Best and Excellent**

Find the move-cell color block (around line 223–227):
```css
.move-cell.blunder    { background: var(--red-bg);    color: var(--red); }
.move-cell.mistake    { background: var(--orange-bg); color: var(--orange); }
.move-cell.inaccuracy { background: var(--yellow-bg); color: var(--yellow); }
.move-cell.good       { color: var(--text-primary); }
```

Insert two new lines **before** `.move-cell.good`:
```css
.move-cell.best       { color: var(--blue); }
.move-cell.excellent  { color: var(--green); }
```

- [ ] **Step 3: Commit**

```bash
git add chess-coach-frontend/src/index.css
git commit -m "Add Best/Excellent CSS badge and move-cell classes"
```

---

## Task 4: Frontend `App.jsx` — badge logic, MoveBadge, cache version

**Files:**
- Modify: `chess-coach-frontend/src/App.jsx`

- [ ] **Step 1: Bump ANALYSIS_CACHE_VERSION from 10 to 11**

Find line ~20:
```js
const ANALYSIS_CACHE_VERSION = 10;
```

Change to:
```js
const ANALYSIS_CACHE_VERSION = 11;
```

Comment at the top of the version history should be updated too — add to the comments block above it:
```js
//         11 = label now distinguishes Best/Excellent/Good for positive moves.
```

- [ ] **Step 2: Update `moveBadgeClass` to handle Best and Excellent, return "" for Good**

Find the function (line ~70):
```js
function moveBadgeClass(label) {
  if (!label) return "";
  const l = label.toLowerCase();
  // Composite labels like "Blunder + Miss" — color by severity, miss is conveyed in text
  if (l.includes("blunder"))    return "blunder";
  if (l.includes("mistake"))    return "mistake";
  if (l.includes("inaccuracy")) return "inaccuracy";
  if (l.includes("miss"))       return "miss";   // "Miss" and "Missed Win"
  return "good";
}
```

Replace with:
```js
function moveBadgeClass(label) {
  if (!label) return "";
  const l = label.toLowerCase();
  // Composite labels like "Blunder + Miss" — color by severity, miss is conveyed in text
  if (l.includes("blunder"))    return "blunder";
  if (l.includes("mistake"))    return "mistake";
  if (l.includes("inaccuracy")) return "inaccuracy";
  if (l.includes("miss"))       return "miss";   // "Miss" and "Missed Win"
  if (l === "best")             return "best";
  if (l === "excellent")        return "excellent";
  return "";  // "Good" and anything else: no colored badge (matches Chess.com's silent treatment)
}
```

- [ ] **Step 3: Update `MoveBadge` to skip rendering when badge class is empty**

Find the component (line ~96):
```js
function MoveBadge({ label }) {
  if (!label) return null;
  return <span className={`badge badge-${moveBadgeClass(label)}`}>{label}</span>;
}
```

Replace with:
```js
function MoveBadge({ label }) {
  if (!label) return null;
  const cls = moveBadgeClass(label);
  if (!cls) return null;
  return <span className={`badge badge-${cls}`}>{label}</span>;
}
```

- [ ] **Step 4: Commit**

```bash
git add chess-coach-frontend/src/App.jsx
git commit -m "Bump cache v11; update badge logic for Best/Excellent/Good"
```

---

## Task 5: Frontend `App.jsx` — all-moves list, rename `selectMistakeForReview`

**Files:**
- Modify: `chess-coach-frontend/src/App.jsx`

- [ ] **Step 1: Rename `selectMistakeForReview` → `selectMoveForReview` everywhere**

There are exactly 2 occurrences: the function definition (~line 652) and the call site (~line 1752).

Replace all occurrences:
```
selectMistakeForReview  →  selectMoveForReview
```

Verify with:
```bash
grep -n "selectMistakeForReview" chess-coach-frontend/src/App.jsx
```

Expected: no output (all renamed).

- [ ] **Step 2: Replace the `badMoves`-only left column with an all-moves flat list**

Find the left column block (~lines 1703–1765). It currently starts with:
```js
const badMoves = selectedGame.moves.filter(m =>
  ["Mistake","Blunder"].includes(m.severity) || m.missed_opportunity
);
return (
  <div>
    ...
    {/* 3-column: mistake list | board | explanation */}
    <div style={{ display:"grid", gridTemplateColumns:"200px minmax(0,auto) 1fr", gap:14, alignItems:"start" }}>

      {/* Col 1: mistake list */}
      <div className="card scroll-panel" style={{ maxHeight:480, padding:"10px 8px" }}>
        <div style={{ fontSize:11, fontWeight:700, letterSpacing:".06em", textTransform:"uppercase", color:"var(--text-muted)", padding:"0 4px 8px" }}>
          Mistakes ({badMoves.length})
        </div>
        <div style={{ display:"flex", flexDirection:"column", gap:4 }}>
          {badMoves.map((m, i) => {
            const isHighlighted = selectedOccurrence?.move_numbers?.includes(m.move_number);
            const isSelected = reviewMoveNumber === m.move_number;
            return (
              <button key={i} onClick={() => selectMoveForReview(m, selectedGame.moves)}
                className={`mistake-btn${isSelected?" active":isHighlighted?" highlighted":""}`}>
                <span>
                  {isHighlighted && <span style={{ marginRight:4, color:"var(--orange)" }}>●</span>}
                  <span style={{ color:"var(--text-muted)", fontSize:11, marginRight:4 }}>#{m.move_number}</span>
                  <span style={{ fontFamily:"monospace", fontWeight:600 }}>{m.san}</span>
                </span>
                <MoveBadge label={m.label} />
              </button>
            );
          })}
          {badMoves.length === 0 && <div style={{ fontSize:12, color:"var(--text-muted)", padding:"4px" }}>No mistakes found.</div>}
        </div>
      </div>
```

Replace the `const badMoves = ...` line AND the entire Col 1 div with:

```js
const allMoves = selectedGame.moves;   // all user moves, not just errors
return (
  <div>
    ...
    {/* 3-column: move list | board | explanation */}
    <div style={{ display:"grid", gridTemplateColumns:"200px minmax(0,auto) 1fr", gap:14, alignItems:"start" }}>

      {/* Col 1: full move list */}
      <div className="card scroll-panel" style={{ maxHeight:480, padding:"10px 8px" }}>
        <div style={{ fontSize:11, fontWeight:700, letterSpacing:".06em", textTransform:"uppercase", color:"var(--text-muted)", padding:"0 4px 8px" }}>
          Moves ({allMoves.length})
        </div>
        <div style={{ display:"flex", flexDirection:"column", gap:2 }}>
          {allMoves.map((m, i) => {
            const isHighlighted = selectedOccurrence?.move_numbers?.includes(m.move_number);
            const isSelected = reviewMoveNumber === m.move_number;
            const badgeCls = moveBadgeClass(m.label);
            return (
              <button key={i} onClick={() => selectMoveForReview(m, selectedGame.moves)}
                className={`mistake-btn${isSelected?" active":isHighlighted?" highlighted":""}`}>
                <span style={{ display:"flex", alignItems:"center", gap:4 }}>
                  {isHighlighted && <span style={{ color:"var(--orange)" }}>●</span>}
                  <span style={{ color:"var(--text-muted)", fontSize:11, minWidth:20 }}>#{m.move_number}</span>
                  <span style={{ fontFamily:"monospace", fontWeight:600 }}>{m.san}</span>
                </span>
                {badgeCls && <MoveBadge label={m.label} />}
              </button>
            );
          })}
          {allMoves.length === 0 && <div style={{ fontSize:12, color:"var(--text-muted)", padding:"4px" }}>No moves found.</div>}
        </div>
      </div>
```

**Important:** The `const badMoves` line must be deleted. Do not leave a dangling `badMoves` reference anywhere else in this render block. Check with:
```bash
grep -n "badMoves" chess-coach-frontend/src/App.jsx
```
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add chess-coach-frontend/src/App.jsx
git commit -m "Show all user moves in game review, not just mistakes"
```

---

## Task 6: Frontend `App.jsx` — gate "Analyse with Coach" to Mistake/Blunder

**Files:**
- Modify: `chess-coach-frontend/src/App.jsx` (~line 1863)

- [ ] **Step 1: Gate the `onAnalyze` prop on ExplainPanel**

Find the ExplainPanel usage in the game review (~line 1857–1866):
```jsx
<ExplainPanel
  explanation={reviewExplanation}
  bestMove={reviewBestMove}
  explaining={reviewExplaining}
  batchLoading={batchInFlightGame === selectedGameIndex}
  posIndex={reviewPosIndex}
  onAnalyze={reviewSelectedMove && !reviewExplanation && !reviewExplaining
    ? () => fetchMistakeExplanation(reviewSelectedMove, selectedGame?.moves, selectedGameIndex)
    : null}
/>
```

Replace the `onAnalyze` prop value:
```jsx
<ExplainPanel
  explanation={reviewExplanation}
  bestMove={reviewBestMove}
  explaining={reviewExplaining}
  batchLoading={batchInFlightGame === selectedGameIndex}
  posIndex={reviewPosIndex}
  onAnalyze={
    reviewSelectedMove &&
    ["Mistake", "Blunder"].includes(reviewSelectedMove.severity) &&
    !reviewExplanation &&
    !reviewExplaining
      ? () => fetchMistakeExplanation(reviewSelectedMove, selectedGame?.moves, selectedGameIndex)
      : null
  }
/>
```

`ExplainPanel` already returns `null` early when `!explanation && !bestMove && !onAnalyze` (line ~214), so for Best/Excellent/Good moves that have a best-move arrow, the panel will still render the arrow — it just won't show the "Analyse with Coach" button.

- [ ] **Step 2: Verify logic in `ExplainPanel`**

Read lines 199–250 of `App.jsx` and confirm that when `onAnalyze` is `null` and `bestMove` is non-null, the panel renders the best-move display without the button. No code changes needed — this is a confirmation step.

- [ ] **Step 3: Run backend tests one final time**

```bash
cd chess-coach-backend
python -m pytest test_attack_claim_verifier.py test_classify_move.py -v 2>&1 | tail -5
```

Expected: 16 passed.

- [ ] **Step 4: Commit**

```bash
git add chess-coach-frontend/src/App.jsx
git commit -m "Gate 'Analyse with Coach' to Mistake/Blunder severity only"
```

# Research: How a sub-800 player improves — and what that means for Chess Coach

> Compiled 2026-05-31 from a multi-source, adversarially fact-checked research pass
> (5 search angles, 24 sources, 108 claims extracted, 25 verified by 3-vote majority,
> 21 confirmed / 4 killed). Confidence levels and refuted claims are kept in-line on
> purpose — don't quietly drop the caveats.

This doc serves two readers at once: **the player** (a sub-800 improver) and **the builder**
(Chess Coach = Stockfish for analysis + an LLM for explanations). Each finding notes the
product implication.

---

## The one-line answer

At sub-800, the highest-leverage skill is **attention, not calculation**. Most beginner
losses come from *not noticing* a position needed care, and most beginners plateau by
playing volumes of fast games without ever reviewing them. The fix is a two-sided **CCT
habit** (scan Checks / Captures / Threats — yours *and* the opponent's) plus a real
review loop. Openings, deep calculation, and endgame theory are secondary at this level.

**Honesty up front:** no *controlled* study proves "do X to gain rating fastest" for
sub-800 online players. The evidence below is correlational + first-party product docs +
expert consensus. Confidence is marked throughout.

---

## Part 1 — What actually works (evidence-ranked)

| Finding | Confidence | Implication |
|---|---|---|
| Solitary serious study is the **single strongest predictor** of chess skill — beats tournament play *and* coaching (r≈0.54/0.48; ~40% of variance). [Charness 2005] | High (studied *tournament* players, not <800 — extrapolated) | Focused, deliberate study > just playing. |
| Practice is **necessary but not sufficient** — explains only ~34% of variance; hours-to-master ranged **832 → 24,284** across people. [Hambrick 2014] | High | There is **no fixed "hours to 1000."** Track *rate of improvement*, not a timeline. |
| Beginners plateau by **playing volume without analyzing mistakes**; most tactical errors are failures to *notice*, not to calculate. | High (corroborated by a Stanford ~96k-user analysis + Dan Heisman's "hope chess") | Play rapid not blitz; review losses; build a noticing habit. |
| **CCT is the lever** — a forcing-move scan, two-sided (your forcing moves + the opponent's forcing replies). Beginner games are decided by direct checks, free captures, one-move threats. | High | A CCT nudge — especially the *defensive replies* scan — is the single most buildable beginner intervention. |

**Nuance:** FM Gabor Horvath notes CCT isn't sufficient *as taught* — consciously listing
checks/captures isn't enough; pattern *recognition* must be trained until automatic. So
**CCT (the habit) + motif puzzles (the recognition training)** together, not either alone.

---

## Part 2 — Diagnosing weaknesses (the engine machinery)

First-party and high-confidence. This is the part most relevant to the product.

| Mechanism | How it works | Source |
|---|---|---|
| **Win% from centipawns** | Lichess sigmoid: `Win% = 50 + 50·(2/(1+exp(−0.00368208·cp)) − 1)` | lichess.org/page/accuracy |
| **Accuracy from win%-drop** | `Accuracy% = 103.1668·exp(−0.04354·Δwin) − 3.1669` | same |
| **"Good moves don't exist"** | Accuracy is penalty-only — you can only hold or lose winning chances | same |
| **Chess.com classification** | **Rating-dependent Expected Points Model** (NOT raw centipawns): Best 0.00 / Excellent ≤0.02 / Good ≤0.05 / Inaccuracy ≤0.10 / Mistake ≤0.20 / Blunder >0.20 expected-points lost | Chess.com Help |
| **Offensive vs defensive split** | Chess.com **"Miss"** = failed to punish opponent's mistake (offensive). Lichess **Opportunism** (you punish their blunders) vs **Luck** (they fail to punish yours) | Chess.com + Lichess Insights |
| **Phase clustering** | Lichess Insights segments opening/middlegame/endgame; uses **ACPL** (Average Centipawn Loss) per phase | Lichess Insights |
| **Explain the move *you* played** | DecodeChess explains your *actual* move (mistake/blunder/alt), not just the engine's best. Built on Stockfish + proprietary explainable-AI — **not an LLM** | DecodeChess |

---

## Part 3 — A weekly routine (LOWER evidence — flagged)

No surviving claim substantiated a specific weekly time-split as fastest for <800. This is
expert-consensus synthesis built on the validated principles above, not proven:

- **Play 3–5 rapid games** (10+ min), not blitz. Volume without thinking time entrenches the plateau.
- **Review every loss + close wins.** For each blunder, *first* ask "did I CCT-scan? what was the opponent threatening?" before opening the engine.
- **~15 min of motif puzzles daily**, tagged to your recurring weakness (hanging pieces, forks). Recognition training, not speed-running.
- **One CCT rep per move** in slow games until automatic.
- **Skip opening study** beyond basics (develop, castle, don't hang pieces) — low yield at <800.

---

## Part 4 — Product implications for Chess Coach

The striking result: **the architecture already implements the load-bearing insights.**

| Research finding | Chess Coach today | Status |
|---|---|---|
| Offensive/defensive split is "the load-bearing product insight" | `missed_opportunity` / `created_problem`; `generate_player_summary`'s DEFENSIVE/OFFENSIVE/MIXED logic | ✅ Validated — keep central |
| CCT habit (esp. defensive scan) is the #1 beginner lever | CCT-scan + "sit on your hands" recommendations | ✅ Validated — lean harder on it |
| Phase clustering of mistakes | `errors_opening/middlegame/endgame` | ✅ Validated (mirrors Lichess Insights) |
| "Explain the move you actually played" UX | The `/summarize` framing | ✅ Validated; our **LLM** explanation differentiates vs DecodeChess's non-LLM XAI |
| Motif-tagged puzzle training | `analyze_game` → Lichess puzzle themes | ✅ Right direction |
| Chess.com uses **rating-dependent** thresholds | `classify_move` was rating-blind | ⚠️ **Gap — addressed 2026-05-31 (see below)** |

### Change made: rating-aware move classification

`classify_move(before, after, best_eval=None, rating=None)` now scales the win%-delta and
the lower cp-delta boundaries (Good/Inaccuracy and Inaccuracy/Mistake) by a rating factor —
gentler for beginners, stricter for strong players — mirroring Chess.com's rating
dependence. Threaded into the chess.com analysis endpoints (which know the user's per-game
rating). Single-move endpoints (`/play`, `/summarize`, `/evaluate-move`) pass no rating and
are unchanged.

- Factor: `1.0 + (1200 − rating)/2000`, clamped to [0.8, 1.5]. (rating 800 → 1.2; 1200 → 1.0; 2000 → 0.8.)
- **Invariant — do not break:** the cp-delta **Mistake/Blunder floor (−300) is never scaled**, so a hung piece / material drop ≥300cp is a Blunder at *every* rating. Leniency only adjusts whether a moderate slip reads as Inaccuracy vs Mistake. This preserves the hung-piece / CCT lesson for beginners.
- **Latent finding while implementing:** across 25,921 realistic eval pairs the win%-delta arm *never* produced a worse label than the cp-delta arm — the cp arm always dominates. The win% arm is effectively dead weight in the current calibration. Worth revisiting the threshold calibration separately (out of scope here).

### Change made: rating-calibrated coaching advice

Move *badges* being rating-aware isn't enough — the coach's *words* should match the player's level too (research: a sub-800 player needs piece-safety, not positional nuance). `_level_guidance(rating)` in `gemini_client.py` injects a PLAYER LEVEL block into the (single, unified) explanation prompt: beginner (<800) → emphasize piece safety + one-move tactics, and explicitly AVOID opening theory / positional concepts / deep endgames; improver (<1400) → two-move tactics + basic plans; intermediate+ → full vocabulary. The prompt instructs the LLM to pitch *every* field (summary, what_next, if_bad_fix) to that level. `rating=None` (Play/Try modes) → generic advice, unchanged. Review mode forwards `player_rating`. Bands are tunable in one function.

**CCT reframed for the time problem.** A full Checks/Captures/Threats scan *every move* is too slow for a beginner in real games — they flag or abandon it. So the beginner band now teaches the cheap version: Heisman's one-question **safety check on the move already chosen** ("after this move, can my opponent take something for free, or give a check/threat I can't meet?") plus **playing slower time controls** (Rapid, not Blitz) so there's time to ask it. The full board scan is reserved for improvers+. The prompt explicitly tells the LLM *not* to say "scan every check/capture/threat on every move." (Sources: Dan Heisman, Real vs Hope Chess; community coaching consensus — not an RCT.)
> Resolved: `generate_player_summary` no longer hard-codes "CCT scan before every move" — it leads with the one-question "Is it safe?" check and chooses the second habit from the time profile (see Implemented section below).

---

## What got DEBUNKED (do not state as fact, do not bake into the product)

Four widely-repeated claims **failed** verification:

- ❌ **"~80% of games are decided by tactical blunders"** (refuted 0–3). Common figure, unsubstantiated. Don't put a number like this in the UI.
- ❌ **"Game review is *the* single highest-value activity; analyze 10 min before the engine"** (refuted 0–3). Review helps, but the *strong* claim didn't hold. Don't make it the central marketing promise — make **habit-building** the promise.
- ❌ **Chess.com classifies by raw centipawn tiers** (Best 0 / Good <100 / Blunder >500) (refuted 0–3). It's Expected Points. Don't replicate raw-cp tiers as "the Chess.com scheme."
- ❌ **Aimchess as *the* statistical-weakness tool** (1–2, unconfirmed).

---

## Open questions (would need primary data)

- No causal study ranks puzzles vs review vs spaced-repetition vs more-games for sub-800 specifically.
- Spaced-repetition / Woodpecker retention numbers for beginners: not substantiated.
- Exact motif-detection methodology of Chess.com Insights / Aimchess: unconfirmed.
- Real sub-800 ACPL / blunder-rate baselines and the "fastest" weekly time-split: unsubstantiated — **a good thing for Chess Coach to derive from its own users' games later.**

---

## Implemented: clock-aware blunder diagnosis

> **Status: shipped.** Clock parsing + the time-pressure/recognition split + reconciled
> player-summary advice are live. What follows is the design as built.

**Problem.** "Do a safety check / CCT" is the wrong fix for a blunder made with 4 seconds
left, and "play slower" is the wrong fix for a blunder made with 4 minutes left. The coach
couldn't tell these apart, so its time/CCT advice was sometimes exactly backwards.

**Goal.** Split a player's blunders into **time-pressure** vs **recognition-gap**, and route
each to the right advice:
- Blundered with little time / as a snap move → time management + play longer time controls + the *cheap* one-question safety check.
- Blundered with plenty of time → recognition gap → safety-check habit + targeted motif puzzles.

**Data we already have.** Chess.com PGNs carry per-move clock annotations (`[%clk h:mm:ss]`)
and a `TimeControl` header (e.g. `600+5`); `time_class` (bullet/blitz/rapid) is already
fetched. python-chess exposes the clock via `node.clock()`.

**Approach (mirrors the rating work — classification stays honest, only ADVICE adapts):**
1. In the `analyze_chessdotcom` / `import_chessdotcom` move loops, read each user move's
   remaining clock and compute `time_spent_s` (prev clock − this clock + increment).
2. Tag flagged moves with `under_time_pressure` (bool|None) using thresholds relative to the
   time control — e.g. clock-after below ~10% of base time, OR `time_spent_s` under ~2–3s
   (a snap move). `None` when the PGN has no clock data (older games / some imports) — never
   claim time pressure we can't see.
3. Aggregate into `player_stats`: `blunders_under_time_pressure` vs `blunders_with_time`.
4. Feed both to `generate_player_summary` as a third signal alongside the offensive/defensive
   split, and adjust recommendations accordingly. **Reconcile the hard-coded "CCT scan every
   move" recommendation here** → cheap safety check + time-control advice.
5. Optionally thread `under_time_pressure` into the per-move coach (`data`) so a single move's
   tip is tailored ("you had time here — slow down and run the safety check" vs "this was time
   pressure — a slower time control would help").

**Caveats / guardrails.**
- Classification (the badge) does **not** change — a low-clock blunder is still a Blunder.
  Time pressure only changes the *advice*. (Same principle as rating-awareness.)
- Increment matters (3+2 ≠ 3+0); read it from `TimeControl`, don't assume.
- Bullet is essentially always time pressure → the lever there is "play slower," full stop.
- Graceful fallback when clock data is absent.

**Effort.** Moderate. Clock parsing is the only genuinely new piece; the rest is the same
field-threading + prompt-tweak pattern used for rating-awareness.

---

## Sources

Primary / first-party (high confidence):
- Charness et al. (2005), *Applied Cognitive Psychology* — http://www.chrest.info/Fribourg_Cours_Expertise/Articles-www/II%20Donnees%20empiriques/CharnessEtal2005ACP.pdf
- Hambrick et al. (2014), *Intelligence* — https://gwern.net/doc/psychology/chess/2014-hambrick.pdf
- Lichess accuracy methodology — https://lichess.org/page/accuracy
- Chess.com move classification — https://support.chess.com/en/articles/8572705-how-are-moves-classified-what-is-a-blunder-or-brilliant-etc
- Lichess Chess Insights — https://lichess.org/@/lichess/blog/chess-insights/VmZbaigA
- DecodeChess "Explaining YOUR Moves" — https://decodechess.com/new-feature-explaining-your-moves/

Secondary (coaching blogs / forums — corroborating, lower authority):
- https://www.chessworld.net/chessclubs/openingguide/cct-and-tactical-alertness.asp
- https://www.chess.com/blog/GaborHorvath/the-truth-about-checks-captures-threats
- https://www.thechesslifestyle.com/blog/how-to-improve-chess-rating-fast/ (note: source of two *refuted* claims)

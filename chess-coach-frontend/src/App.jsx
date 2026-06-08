import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import axios from "axios";
import { Chess } from "chess.js";
import { Chessboard } from "react-chessboard";

const API = "http://127.0.0.1:8000";

// Bump when the shape of cached analysis data changes (new required fields, etc).
// Old cached entries from before the bump are treated as a cache miss and re-fetched.
// History: 1 = pre-classifier-unification (label only); 2 = adds severity + missed_opportunity;
//          3 = adds motif + tactical_summary per flagged move (cross-game LLM uses these);
//          4 = replaces player_summary.elo_context with player_summary.recommendations (list);
//          5 = recommendations now distinguish defensive habit-building vs offensive puzzles;
//          6 = recommendations forbid non-existent puzzle themes, require CCT + sit-on-hands;
//          7 = adds drill_question per flagged move (Threat Drill tab);
//          8 = drill_question.correction (phase 2 — what should you have played);
//          9 = drills now only for blunders and missed wins (mistakes excluded).
//         10 = adds missed_win flag + "Missed Win" label (missed-mate-still-winning
//              no longer mislabelled as Blunder; severity drops to the win% arm).
const ANALYSIS_CACHE_VERSION = 10;

function readAnalysisCache(username) {
  try {
    const raw = localStorage.getItem(`chess_coach_analysis_${username.toLowerCase()}`);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (parsed.version !== ANALYSIS_CACHE_VERSION) return null;  // schema bump invalidates
    return parsed;  // { version, data, timestamp }
  } catch { return null; }
}

function writeAnalysisCache(username, data, timestamp) {
  try {
    localStorage.setItem(
      `chess_coach_analysis_${username.toLowerCase()}`,
      JSON.stringify({ version: ANALYSIS_CACHE_VERSION, data, timestamp }),
    );
  } catch {}
}

axios.interceptors.request.use(req => {
  console.log(`[API] ${req.method?.toUpperCase()} ${req.url}`, req.data || "");
  return req;
});
axios.interceptors.response.use(
  res => { console.log(`[API] ${res.status} ${res.config.url}`, res.data); return res; },
  err => { console.error(`[API] ERR ${err.config?.url}`, err.response?.data || err.message); return Promise.reject(err); }
);

const LOSS_RESULTS = new Set(["checkmated","resigned","timeout","abandoned","kingofthehill","threecheck"]);
const DRAW_RESULTS = new Set(["stalemate","insufficient","50move","repetition","agreed","timevsinsufficient"]);

function gameResult(result) {
  if (result === "win")            return { label: "Win",  cls: "result-win" };
  if (LOSS_RESULTS.has(result))    return { label: resultLabel(result), cls: "result-loss" };
  if (DRAW_RESULTS.has(result))    return { label: "Draw", cls: "result-draw" };
  return { label: result || "?",   cls: "" };
}
function resultLabel(r) {
  return ({ checkmated:"Loss (checkmate)", resigned:"Loss (resigned)", timeout:"Loss (timeout)", abandoned:"Loss (abandoned)" }[r] || `Loss (${r})`);
}

const THEME_LABELS = {
  mateIn1:"Checkmate in 1", mateIn2:"Checkmate in 2", mateIn3:"Checkmate in 3",
  mateIn4:"Checkmate in 4", mateIn5:"Checkmate in 5+",
  fork:"Forks", hangingPiece:"Hanging Pieces", pin:"Pins", skewer:"Skewers",
  discoveredAttack:"Discovered Attacks", crushing:"Winning Combinations", defensiveMove:"Defensive Resources",
};

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

// ── Small reusable components ──────────────────────────────────────────────

function TabBar({ active, onChange }) {
  const labels = { analyze: "Analyze My Games", drill: "Threat Drill", play: "Play vs Bot" };
  return (
    <div className="tabs">
      {["analyze","drill","play"].map(tab => (
        <button key={tab} className={`tab-btn${active === tab ? " active" : ""}`} onClick={() => onChange(tab)}>
          {labels[tab]}
        </button>
      ))}
    </div>
  );
}

function MoveBadge({ label }) {
  if (!label) return null;
  return <span className={`badge badge-${moveBadgeClass(label)}`}>{label}</span>;
}

function EvalBar({ white, black, cap = 800 }) {
  const clamp = v => Math.max(-cap, Math.min(cap, Number.isFinite(v) ? v : 0));
  const fmt   = v => v == null || isNaN(v) ? "—" : `${v > 0 ? "+" : ""}${Math.round(v)}`;
  const w  = clamp(white ?? 0);
  const p  = (w + cap) / (2 * cap);
  return (
    <div className="eval-bar-wrap">
      <div className="eval-bar-track" style={{ width: 240 }}>
        <div className="eval-bar-black" style={{ width: `${((1 - p) * 100).toFixed(1)}%` }} title={`Black ${fmt(black)} cp`} />
        <div className="eval-bar-white" style={{ width: `${(p * 100).toFixed(1)}%` }} title={`White ${fmt(white)} cp`} />
      </div>
      <span className="eval-label">W: {fmt(white)} cp &nbsp;·&nbsp; B: {fmt(black)} cp</span>
    </div>
  );
}

function TryModePanel({ result, explaining, explanation, onAnalyze, onNext, onUndo, onReset, canUndo }) {
  const cls = moveBadgeClass(result?.label);
  const cpSign = result?.cp_delta > 0 ? "+" : "";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {/* Eval row */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <MoveBadge label={result?.label} />
        <span style={{ fontSize: 13, color: "var(--text-secondary)", fontFamily: "monospace" }}>
          {result?.san}
        </span>
        <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
          {cpSign}{result?.cp_delta} cp
        </span>
      </div>

      {/* Best move hint */}
      {result?.best_san && result?.label !== "Good" && (
        <div className="explain-better">
          <div className="explain-label" style={{ color: "var(--green)" }}>Better was</div>
          <span style={{ fontFamily: "monospace", fontWeight: 600 }}>{result.best_san}</span>
        </div>
      )}
      {result?.label === "Good" && (
        <div className="explain-better">
          <div className="explain-label" style={{ color: "var(--green)" }}>Good move!</div>
          This was one of the best options in this position.
        </div>
      )}

      {/* Analyse button */}
      {result?.label !== "Good" && !explanation && !explaining && onAnalyze && (
        <button className="btn-primary" onClick={onAnalyze} style={{ alignSelf: "flex-start" }}>
          Analyse with Coach
        </button>
      )}
      {explaining && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, color: "var(--text-secondary)", fontSize: 13 }}>
          <span className="spinner" /> Analysing…
        </div>
      )}
      {explanation?.summary && !explaining && (
        <div className="explain-why">
          <div className="explain-label" style={{ color: "var(--orange)" }}>Why it's not the best</div>
          {explanation.summary}
        </div>
      )}
      {explanation?.if_bad_fix?.why_best && !explaining && (
        <div className="explain-better">
          <div className="explain-label" style={{ color: "var(--green)" }}>
            Why {explanation.if_bad_fix.best_move || result?.best_san} is better
          </div>
          {explanation.if_bad_fix.why_best}
        </div>
      )}
      {Array.isArray(explanation?.what_next) && explanation.what_next.length > 0 && !explaining && (
        <div className="explain-tip">
          <div className="explain-label" style={{ color: "var(--blue)" }}>Next time</div>
          <ul style={{ margin: "4px 0 0", paddingLeft: 16 }}>
            {explanation.what_next.map((t, i) => <li key={i} style={{ marginBottom: 3 }}>{t}</li>)}
          </ul>
        </div>
      )}

      {/* Navigation */}
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 4 }}>
        <button onClick={onNext} style={{ fontSize: 12, padding: "5px 12px" }}>
          Opponent plays → Next
        </button>
        {canUndo && (
          <button onClick={onUndo} style={{ fontSize: 12, padding: "5px 12px" }}>
            ← Undo
          </button>
        )}
        <button onClick={onReset} style={{ fontSize: 12, padding: "5px 12px", color: "var(--text-muted)" }}>
          Reset position
        </button>
      </div>
    </div>
  );
}

function ExplainPanel({ explanation, bestMove, explaining, posIndex, onAnalyze, batchLoading }) {
  if (explaining) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 6, color: "var(--text-secondary)", fontSize: 13 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span className="spinner" /> {batchLoading ? "Pre-loading all flagged moves…" : "Analyzing…"}
        </div>
        {batchLoading && (
          <div style={{ fontSize: 11, color: "var(--text-muted)", paddingLeft: 26, lineHeight: 1.5 }}>
            Coach is analysing every mistake in this game in one pass — first move takes longer, the rest will be instant.
          </div>
        )}
      </div>
    );
  }
  if (!explanation && !bestMove && !onAnalyze) {
    return (
      <div style={{ color: "var(--text-muted)", fontSize: 13, fontStyle: "italic", paddingTop: 8 }}>
        Click a mistake in the list to see the position.
      </div>
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {bestMove?.displayText && posIndex === 0 && (
        <div className="explain-better">
          <div className="explain-label" style={{ color: "var(--green)" }}>Best Move</div>
          {bestMove.displayText}
        </div>
      )}

      {/* Analyse button — shown until the user explicitly requests it */}
      {!explanation && onAnalyze && (
        <button className="btn-primary" onClick={onAnalyze} style={{ alignSelf: "flex-start" }}>
          Analyse with Coach
        </button>
      )}

      {explanation?.summary && (
        <div className="explain-why">
          <div className="explain-label" style={{ color: "var(--orange)" }}>Why it was bad</div>
          {explanation.summary}
        </div>
      )}
      {explanation?.if_bad_fix?.why_best && (
        <div className="explain-better">
          <div className="explain-label" style={{ color: "var(--green)" }}>
            Why {explanation.if_bad_fix.best_move || "the best move"} is better
          </div>
          {explanation.if_bad_fix.why_best}
          {explanation.if_bad_fix.missed_idea && (
            <div style={{ marginTop: 6, color: "var(--green)", opacity: .85 }}>
              <strong>Missed idea:</strong> {explanation.if_bad_fix.missed_idea}
            </div>
          )}
        </div>
      )}
      {Array.isArray(explanation?.what_next) && explanation.what_next.length > 0 && (
        <div className="explain-tip">
          <div className="explain-label" style={{ color: "var(--blue)" }}>Next time</div>
          <ul style={{ margin: "4px 0 0", paddingLeft: 16 }}>
            {explanation.what_next.map((tip, i) => <li key={i} style={{ marginBottom: 3 }}>{tip}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}

function StatPill({ label, value, color }) {
  return (
    <div style={{ background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: "var(--radius)", padding: "8px 14px", textAlign: "center", minWidth: 80 }}>
      <div style={{ fontSize: 18, fontWeight: 700, color: color || "var(--text-primary)", lineHeight: 1 }}>{value}</div>
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 3, whiteSpace: "nowrap" }}>{label}</div>
    </div>
  );
}

function PlayerSummary({ username, data }) {
  const [expanded, setExpanded] = useState(false);
  const { player_stats: stats, player_summary: summary, player_rating: rating, player_time_class: timeClass } = data;
  if (!stats || !summary) return null;

  const accuracyColor = stats.accuracy_pct >= 70 ? "var(--green)" : stats.accuracy_pct >= 50 ? "var(--yellow)" : "var(--red)";
  const blunderColor  = stats.blunders_per_game <= 2 ? "var(--green)" : stats.blunders_per_game <= 4 ? "var(--yellow)" : "var(--red)";

  return (
    <div className="card" style={{ marginBottom: 20, padding: "16px 18px" }}>
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
        <div>
          <span style={{ fontWeight: 700, fontSize: 15 }}>{username}</span>
          {rating && <span style={{ color: "var(--text-secondary)", fontSize: 13, marginLeft: 8 }}>{rating} {timeClass && `· ${timeClass}`}</span>}
        </div>
        <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{stats.total_games} games · {stats.total_moves} moves</span>
      </div>

      {/* Stat pills */}
      <div style={{ display: "flex", gap: 10, marginBottom: 14, flexWrap: "wrap" }}>
        <StatPill label="Accuracy" value={`${stats.accuracy_pct}%`} color={accuracyColor} />
        <StatPill label="Blunders/game" value={stats.blunders_per_game} color={blunderColor} />
        <StatPill label="Avg CP loss" value={stats.avg_cp_loss} />
        <StatPill label="Mistakes" value={stats.mistakes} />
        <StatPill label="Blunders" value={stats.blunders} />
      </div>

      {/* Top priority */}
      {summary.improvement_lever && (
        <div className="explain-why" style={{ marginBottom: expanded ? 12 : 0 }}>
          <div className="explain-label" style={{ color: "var(--orange)" }}>Top Priority to Improve</div>
          {summary.improvement_lever}
        </div>
      )}

      {/* Expandable: strengths + recommendations */}
      <button
        className="btn-ghost"
        style={{ marginTop: 10, fontSize: 12, display: "flex", alignItems: "center", gap: 4 }}
        onClick={() => setExpanded(e => !e)}
      >
        {expanded ? "▲ Hide details" : "▼ Strengths & recommendations"}
      </button>

      {expanded && (
        <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 10 }}>
          {Array.isArray(summary.strengths) && summary.strengths.length > 0 && (
            <div className="explain-better">
              <div className="explain-label" style={{ color: "var(--green)" }}>What you do well</div>
              <ul style={{ margin: "4px 0 0", paddingLeft: 16 }}>
                {summary.strengths.map((s, i) => <li key={i} style={{ marginBottom: 3 }}>{s}</li>)}
              </ul>
            </div>
          )}
          {Array.isArray(summary.recommendations) && summary.recommendations.length > 0 && (
            <div className="explain-tip">
              <div className="explain-label" style={{ color: "var(--blue)" }}>Recommendations</div>
              <ul style={{ margin: "4px 0 0", paddingLeft: 16 }}>
                {summary.recommendations.map((r, i) => <li key={i} style={{ marginBottom: 3 }}>{r}</li>)}
              </ul>
            </div>
          )}
          {summary.error && (
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>Summary unavailable: {summary.error}</div>
          )}
        </div>
      )}
    </div>
  );
}

// ── App ────────────────────────────────────────────────────────────────────

export default function App() {
  const [game, setGame] = useState(() => new Chess());
  const [msg, setMsg] = useState("");
  const [coach, setCoach] = useState("");
  const [lastSummary, setLastSummary] = useState(null);
  const [askText, setAskText] = useState("");
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [skill, setSkill] = useState(3);
  const [history, setHistory] = useState(() => [new Chess().fen()]);
  const [redoStack, setRedoStack] = useState([]);
  const [lastUserMove, setLastUserMove] = useState(null);
  const [lastCoachMove, setLastCoachMove] = useState(null);
  const [selectedSquare, setSelectedSquare] = useState(null);
  const [cp, setCp] = useState({ white: null, black: null });
  const [moveHistory, setMoveHistory] = useState([]);

  const [gameOver, setGameOver] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [analysisResult, setAnalysisResult] = useState(null);
  const [analysisError, setAnalysisError] = useState(null);
  const [coachAnalysis, setCoachAnalysis] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [activeTab, setActiveTab] = useState("analyze");

  const [reviewFen, setReviewFen] = useState(null);
  const [reviewMoveNumber, setReviewMoveNumber] = useState(null);
  const [reviewPositions, setReviewPositions] = useState([]);
  const [reviewPosIndex, setReviewPosIndex] = useState(0);
  const [reviewBestMove, setReviewBestMove] = useState(null);
  const [reviewExplanation, setReviewExplanation] = useState(null);
  const [reviewExplaining, setReviewExplaining] = useState(false);
  const [reviewSelectedMove, setReviewSelectedMove] = useState(null);
  // Ref mirrors reviewSelectedMove. fetchMistakeExplanation's batch call is async
  // (can take 30s+); if the user clicks a different flagged move while it's in
  // flight, we use this ref to know the closure's move is no longer displayed
  // and skip the setReviewExplanation that would otherwise clobber the new selection.
  const reviewSelectedMoveRef = useRef(null);
  useEffect(() => { reviewSelectedMoveRef.current = reviewSelectedMove; }, [reviewSelectedMove]);

  // Try-a-move state (game detail review)
  const [tryStack, setTryStack] = useState([]); // [{fen, san, result}] — for undo
  const [tryCurrentFen, setTryCurrentFen] = useState(null); // null = not in try mode
  const [tryResult, setTryResult] = useState(null); // last /evaluate-move response
  const [tryLoading, setTryLoading] = useState(false);
  const [tryExplanation, setTryExplanation] = useState(null);
  const [tryExplaining, setTryExplaining] = useState(false);

  const [savedUsername, setSavedUsername] = useState(() => localStorage.getItem("chess_com_username") || "snoozydoody");
  const [analyzingGames, setAnalyzingGames] = useState(false);
  const [multiGameData, setMultiGameData] = useState(() => {
    const u = localStorage.getItem("chess_com_username") || "snoozydoody";
    return readAnalysisCache(u)?.data ?? null;
  });
  const [analysisCachedAt, setAnalysisCachedAt] = useState(() => {
    const u = localStorage.getItem("chess_com_username") || "snoozydoody";
    return readAnalysisCache(u)?.timestamp ?? null;
  });
  const [selectedWeaknessId, setSelectedWeaknessId] = useState(null);
  const [selectedGameIndex, setSelectedGameIndex] = useState(null);
  const [analyzeError, setAnalyzeError] = useState("");

  // Threat Drill state — builds a quiz from flagged moves in multiGameData.
  // drillIndex points into drillQuestions (computed lazily in the drill branch).
  // drillPicked = the SAN of the option the user chose; null until they pick.
  // drillStats tracks self-reported "saw it / missed it" per motif, persisted to
  // localStorage so progress survives reloads within the same analysis cache.
  // Two-phase drill state:
  //   phase 1 ("threat")     — board at fen_after, user picks opponent's best reply
  //   phase 2 ("correction") — board at fen_before, user picks the move they SHOULD have played
  // drillThreatPicked / drillCorrectionPicked hold the SAN picked in each phase (null until picked).
  // drillStats tracks correctness per motif for both phases.
  const [drillIndex, setDrillIndex] = useState(0);
  const [drillPhase, setDrillPhase] = useState("threat");
  const [drillThreatPicked, setDrillThreatPicked] = useState(null);
  const [drillCorrectionPicked, setDrillCorrectionPicked] = useState(null);
  const [drillHoveredSan, setDrillHoveredSan] = useState(null);
  const [drillStats, setDrillStats] = useState(() => {
    try { return JSON.parse(localStorage.getItem("chess_coach_drill_stats") || "{}"); }
    catch { return {}; }
  });

  // LLM-generated threat + correction explanations, keyed by fen_after so each
  // unique drill question is explained at most once. Cached to localStorage so
  // we never pay Gemini quota twice for the same position.
  const [drillExplanations, setDrillExplanations] = useState(() => {
    try { return JSON.parse(localStorage.getItem("chess_coach_drill_explanations_v4") || "{}"); }
    catch { return {}; }
  });
  const [drillLoadingExplanations, setDrillLoadingExplanations] = useState(false);

  // Per-move "Analyse with Coach" summary cache, keyed by `${fen_before}::${uci}`.
  // When the user clicks Analyse on any flagged move in a game, we batch-fetch
  // explanations for EVERY flagged move in that game so subsequent clicks are
  // instant cache hits. Persists to localStorage across sessions.
  const [summaryCache, setSummaryCache] = useState(() => {
    try { return JSON.parse(localStorage.getItem("chess_coach_summaries_v6") || "{}"); }
    catch { return {}; }
  });
  // Track which game's batch is currently in flight (one at a time is fine —
  // user can only view one game's review at once).
  const [batchInFlightGame, setBatchInFlightGame] = useState(null);

  // Bridge: when the in-flight batch lands and the user is sitting on a move
  // whose explanation just landed in the cache, surface it without requiring
  // another click. Covers the "user clicked B during A's batch" race.
  // Also realigns the sidebar's bestMove with the explanation's pick so the
  // two never contradict each other.
  useEffect(() => {
    if (!reviewSelectedMove || reviewExplanation) return;
    const uciSel = deriveUciForMove(reviewSelectedMove);
    if (!uciSel) return;
    const hit = summaryCache[summaryCacheKey(reviewSelectedMove.fen_before, uciSel)];
    if (hit) {
      setReviewExplanation(hit);
      setReviewExplaining(false);
      const derived = bestMoveFromExplanation(hit, reviewSelectedMove.fen_before);
      if (derived) setReviewBestMove(derived);
    }
  }, [summaryCache, reviewSelectedMove, reviewExplanation]);

  // Hoisted so the explanation-loading useEffect can depend on it. The drill
  // render branch reads the same value.
  const drillQuestions = useMemo(() => {
    return (multiGameData?.games || []).flatMap((g, gi) => {
      const info = g.game_info || {};
      const userIsWhite = info.user_color === "white";
      const opponentName = userIsWhite ? info.black : info.white;
      return (g.moves || [])
        .filter(m => m.drill_question)
        .map(m => ({
          ...m.drill_question,
          motif: m.motif,
          gameIndex: gi,
          moveNumber: m.move_number,
          userColor: info.user_color || "white",
          userSan: m.san,
          fenBeforeUserMove: m.fen_before,
          severity: m.severity,
          label: m.label,
          missedOpportunity: m.missed_opportunity,
          gameResult: info.result,
          opponentName,
          opening: info.opening,
        }));
    });
  }, [multiGameData]);

  // When the Drill tab opens (or new drill questions appear), batch-request
  // Gemini explanations for any question we haven't already cached. One call
  // covers all missing items — N drill questions = 1 Gemini call, not N.
  useEffect(() => {
    if (activeTab !== "drill") return;
    if (drillLoadingExplanations) return;
    if (drillQuestions.length === 0) return;
    const missing = drillQuestions.filter(q => !drillExplanations[q.fen_after]);
    if (missing.length === 0) return;

    const items = missing.map(q => ({
      fen_after: q.fen_after,
      played_san: q.userSan,
      opponent_best_san: q.correct_san,
      correction_fen: q.correction?.fen,
      correction_best_san: q.correction?.correct_san,
      motif: q.motif,
    }));

    setDrillLoadingExplanations(true);
    axios.post(`${API}/drill/explain-batch`, { items }, { timeout: 120000 })
      .then(res => {
        if (res.data?.ok && Array.isArray(res.data.explanations)) {
          const next = { ...drillExplanations };
          items.forEach((it, i) => {
            const e = res.data.explanations[i] || {};
            // Only store if Gemini actually returned content (skip empty padding)
            if (e.threat_explanation || e.correction_explanation) {
              next[it.fen_after] = e;
            }
          });
          setDrillExplanations(next);
          try { localStorage.setItem("chess_coach_drill_explanations_v4", JSON.stringify(next)); } catch {}
        }
      })
      .catch(() => {})  // best-effort; UI falls back to mechanical correct_desc
      .finally(() => setDrillLoadingExplanations(false));
  }, [activeTab, drillQuestions]);

  const [chesscomUsername, setChesscomUsername] = useState("snoozydoody");
  const [importingGame, setImportingGame] = useState(false);
  const [importError, setImportError] = useState("");
  const [sampleMode, setSampleMode] = useState(false);
  const hasSampleData = !!localStorage.getItem("chess_coach_sample_game");

  const [mistakePage, setMistakePage] = useState(0);
  const [reviewMove, setReviewMove] = useState(null);
  const [reviewHistoryIndex, setReviewHistoryIndex] = useState(null);
  const [bestMoveHighlight, setBestMoveHighlight] = useState(null);

  const [mode, setMode] = useState("game");
  const [puzzles, setPuzzles] = useState([]);
  const [puzzleIndex, setPuzzleIndex] = useState(0);
  const [puzzleGame, setPuzzleGame] = useState(null);
  const [puzzleMoveIndex, setPuzzleMoveIndex] = useState(0);
  const [puzzleSolved, setPuzzleSolved] = useState(false);
  const [puzzleFailed, setPuzzleFailed] = useState(false);
  const [puzzleScore, setPuzzleScore] = useState(0);
  const [loadingPuzzles, setLoadingPuzzles] = useState(false);
  const [puzzleMsg, setPuzzleMsg] = useState("");
  const [puzzleComplete, setPuzzleComplete] = useState(false);
  const [puzzleHistory, setPuzzleHistory] = useState([]);
  const [puzzleHistoryIndex, setPuzzleHistoryIndex] = useState(0);
  const [puzzleOrientation, setPuzzleOrientation] = useState("white");

  const PIECE_SYMBOLS = {
    w: { p:"♙", n:"♘", b:"♗", r:"♖", q:"♕" },
    b: { p:"♟", n:"♞", b:"♝", r:"♜", q:"♛" },
  };

  const capturedPieces = useMemo(() => {
    const countPieces = fen => {
      const c = new Chess(); c.load(fen);
      const counts = { w:{p:0,n:0,b:0,r:0,q:0}, b:{p:0,n:0,b:0,r:0,q:0} };
      for (const row of c.board()) for (const sq of row) { if (!sq || sq.type === "k") continue; counts[sq.color][sq.type]++; }
      return counts;
    };
    const byWhite = [], byBlack = [];
    for (let i = 1; i < history.length; i++) {
      const prev = countPieces(history[i-1]), curr = countPieces(history[i]);
      for (const color of ["w","b"]) for (const t of ["p","n","b","r","q"]) {
        const diff = curr[color][t] - prev[color][t];
        if (diff < 0) for (let d = 0; d < -diff; d++) (color === "b" ? byWhite : byBlack).push({ color, type: t });
      }
    }
    return { byWhite, byBlack };
  }, [history]);

  const score = useMemo(() => {
    const pv = { p:1, n:3, b:3, r:5, q:9 };
    return {
      user:  capturedPieces.byWhite.reduce((s,p) => s + (pv[p.type]||0), 0),
      coach: capturedPieces.byBlack.reduce((s,p) => s + (pv[p.type]||0), 0),
    };
  }, [capturedPieces]);

  const legalTargets = useMemo(() => {
    if (!selectedSquare) return [];
    try { return new Chess(game.fen()).moves({ square: selectedSquare, verbose: true }).map(m => m.to); } catch { return []; }
  }, [selectedSquare, game.fen()]);

  useEffect(() => { console.log("App mounted; initial FEN:", game.fen()); }, []);

  function CapturedRow({ pieces, label }) {
    if (!pieces.length) return <div style={{ minHeight: 22, fontSize: 12, color: "var(--text-muted)" }}>{label}: —</div>;
    const order = { q:0, r:1, b:2, n:3, p:4 };
    return (
      <div style={{ minHeight: 22, display: "flex", alignItems: "center", gap: 2, fontSize: 20 }}>
        {[...pieces].sort((a,b) => (order[a.type]??9)-(order[b.type]??9)).map((p,i) => (
          <span key={i} title={`${p.color==="w"?"White":"Black"} ${p.type}`}>{PIECE_SYMBOLS[p.color][p.type]}</span>
        ))}
      </div>
    );
  }

  async function analyzeMyGames(username, forceRefresh = false) {
    if (!username.trim()) return;
    const uname = username.trim();
    if (!forceRefresh) {
      const cached = readAnalysisCache(uname);
      if (cached) {
        setMultiGameData(cached.data); setAnalysisCachedAt(cached.timestamp);
        localStorage.setItem("chess_com_username", uname); setSavedUsername(uname);
        setSelectedWeaknessId(null); setSelectedGameIndex(null); return;
      }
    }
    localStorage.setItem("chess_com_username", uname); setSavedUsername(uname);
    setAnalyzingGames(true); setAnalyzeError(""); setMultiGameData(null); setAnalysisCachedAt(null);
    setSelectedWeaknessId(null); setSelectedGameIndex(null);
    try {
      const res = await axios.post(`${API}/analyze-chessdotcom`, { username: uname, count: 10 }, { timeout: 300000 });
      if (!res.data?.ok) { setAnalyzeError(res.data?.error || "Analysis failed."); return; }
      const data = {
        games: res.data.games || [],
        common_weaknesses: res.data.common_weaknesses || [],
        // The endpoint returns ok:true even when the cross-game weakness LLM call
        // failed (e.g. Gemini daily quota) — the failure is reported in `error`
        // alongside an empty common_weaknesses. Capture it so the empty-state can
        // tell the truth instead of rendering "you're playing well!" over a failure.
        weaknesses_error: res.data.error || null,
        player_stats: res.data.player_stats || null,
        player_summary: res.data.player_summary || null,
        player_rating: res.data.player_rating || null,
        player_time_class: res.data.player_time_class || null,
      };
      const timestamp = new Date().toISOString();
      writeAnalysisCache(uname, data, timestamp);
      setMultiGameData(data); setAnalysisCachedAt(timestamp);
    } catch (e) {
      setAnalyzeError(e?.response?.data?.detail || e?.message || "Request failed.");
    } finally { setAnalyzingGames(false); }
  }

  async function selectMistakeForReview(move, allGameMoves) {
    setReviewMoveNumber(move.move_number);
    setReviewPositions([]); setReviewPosIndex(0);
    setReviewSelectedMove(move);
    // Seed explanation + bestMove from cache so a previously-batched move loads
    // instantly on re-click AND the sidebar's best-move matches the explanation
    // (otherwise /hint at low depth can disagree with the LLM's depth=19 pick).
    const uciForMove = deriveUciForMove(move);
    const cached = uciForMove && summaryCache[summaryCacheKey(move.fen_before, uciForMove)];
    if (cached) {
      setReviewExplanation(cached);
      setReviewBestMove(bestMoveFromExplanation(cached, move.fen_before));
    } else {
      setReviewExplanation(null);
      setReviewBestMove(null);
    }
    // Clear any previous try-mode session
    setTryStack([]); setTryCurrentFen(null); setTryResult(null); setTryExplanation(null); setTryExplaining(false);

    const fenBefore = move.fen_before;
    if (!fenBefore) { setReviewFen(null); return; }

    // Build a position sequence: mistake point + up to 5 full moves of follow-through
    const positions = [];
    positions.push({ fen: fenBefore, label: `Move ${move.move_number} — before your ${move.label?.toLowerCase()}` });

    let fenAfterMistake = null;
    try { const c = new Chess(fenBefore); if (c.move(move.san)) fenAfterMistake = c.fen(); } catch {}
    if (fenAfterMistake) {
      positions.push({ fen: fenAfterMistake, label: `After ${move.san} — your ${move.label?.toLowerCase()}` });
    }

    // Subsequent user moves carry the opponent-replied FEN as their fen_before
    const mistakeIdx = allGameMoves.findIndex(m => m.move_number === move.move_number);
    const MAX_FOLLOW = 5;
    for (let i = 1; i <= MAX_FOLLOW; i++) {
      const next = allGameMoves[mistakeIdx + i];
      if (!next?.fen_before) break;
      // next.fen_before is after the opponent replied to the previous user move
      positions.push({ fen: next.fen_before, label: `After opponent's reply (before move ${next.move_number})` });
      let fenAfterNext = null;
      try { const c = new Chess(next.fen_before); if (c.move(next.san)) fenAfterNext = c.fen(); } catch {}
      if (fenAfterNext) {
        positions.push({ fen: fenAfterNext, label: `After your move ${next.move_number}: ${next.san}` });
      }
    }

    // If this is the last user move (game ended with opponent's reply), fetch that reply
    const isLastMove = mistakeIdx + 1 >= allGameMoves.length;
    if (isLastMove && fenAfterMistake) {
      try {
        const oppRes = await axios.post(`${API}/hint`, { fen: fenAfterMistake });
        if (oppRes.data?.best_uci) {
          const uci = oppRes.data.best_uci;
          const c = new Chess(fenAfterMistake);
          const opp = c.move({ from: uci.slice(0,2), to: uci.slice(2,4), promotion: uci[4]||undefined });
          if (opp) {
            positions.push({ fen: c.fen(), label: `After ${opp.san} — game over` });
          }
        }
      } catch {}
    }

    // Seed pos-0 bestMove from LLM cache so the arrow shows immediately on re-click.
    if (cached) {
      const bm0 = bestMoveFromExplanation(cached, fenBefore);
      if (bm0) positions[0] = { ...positions[0], bestMove: { from: bm0.from, to: bm0.to } };
    }

    setReviewPositions(positions); setReviewPosIndex(0); setReviewFen(fenBefore);

    // Parallel best-move hints for positions 1+ (pos 0 handled separately).
    positions.slice(1).forEach(async (pos, relIdx) => {
      const absIdx = relIdx + 1;
      try {
        const hintRes = await axios.post(`${API}/hint`, { fen: pos.fen });
        if (hintRes.data?.best_uci) {
          const hfrom = hintRes.data.best_uci.slice(0, 2);
          const hto   = hintRes.data.best_uci.slice(2, 4);
          if (reviewSelectedMoveRef.current === move) {
            setReviewPositions(prev =>
              prev.map((p, i) => i === absIdx ? { ...p, bestMove: { from: hfrom, to: hto } } : p)
            );
          }
        }
      } catch {}
    });

    // Fetch best-move hint (fast). Skipped when we already populated bestMove
    // from a cached LLM explanation — that's the depth=19 ground truth and
    // /hint at lower depth could overwrite it with a contradictory pick.
    if (!cached) {
      try {
        const hintRes = await axios.post(`${API}/hint`, { fen: fenBefore });
        if (hintRes.data?.best_uci) {
          const from = hintRes.data.best_uci.slice(0,2), to = hintRes.data.best_uci.slice(2,4);
          const NAMES = { p:"Pawn", n:"Knight", b:"Bishop", r:"Rook", q:"Queen", k:"King" };
          let displayText = `to ${to}`;
          try { const c = new Chess(fenBefore); const piece = c.get(from); if (piece) displayText = `${NAMES[piece.type]} to ${to}`; } catch {}
          setReviewBestMove({ from, to, displayText });
          setReviewPositions(prev =>
            prev.map((p, i) => i === 0 ? { ...p, bestMove: { from, to } } : p)
          );
        }
      } catch {}
    }
    // Gemini explanation is NOT triggered here — user clicks "Analyse with Coach"
  }

  // Derive sidebar bestMove ({from, to, displayText}) from a cached LLM
  // explanation's recommended best move. Uses the same SAN→squares conversion
  // as the /hint path so both rendering codepaths produce the same shape.
  // Returns null if the explanation has no best_move or the SAN can't be parsed.
  function bestMoveFromExplanation(exp, fenBefore) {
    const bestSan = exp?.if_bad_fix?.best_move;
    if (!bestSan || !fenBefore) return null;
    try {
      const c = new Chess(fenBefore);
      const match = c.moves({ verbose: true }).find(m =>
        m.san.replace(/[+#]$/, "") === String(bestSan).replace(/[+#]$/, "")
      );
      if (!match) return null;
      const NAMES = { p:"Pawn", n:"Knight", b:"Bishop", r:"Rook", q:"Queen", k:"King" };
      return {
        from: match.from,
        to: match.to,
        displayText: `${NAMES[match.piece]} to ${match.to}`,
      };
    } catch { return null; }
  }

  // Derive UCI from a move dict (san + fen_before). Returns null if unparseable.
  function deriveUciForMove(move) {
    if (!move?.fen_before || !move?.san) return null;
    try {
      const c = new Chess(move.fen_before);
      const match = c.moves({ verbose: true }).find(m => m.san.replace(/[+#]$/,"") === move.san.replace(/[+#]$/,""));
      if (match) return match.from + match.to + (match.promotion || "");
    } catch {}
    return null;
  }

  // Stable cache key for a single move's coach summary.
  function summaryCacheKey(fen, uci) { return `${fen}::${uci}`; }

  // Filter a game's moves down to the ones worth analyzing (Mistakes, Blunders,
  // or missed opportunities — same gate as the per-game drill-down badMoves).
  function flaggedMovesIn(gameMoves) {
    return (gameMoves || []).filter(m =>
      ["Mistake", "Blunder"].includes(m.severity) || m.missed_opportunity
    );
  }

  // Cap the batch at this many moves. Larger prompts get truncated or garbled
  // by Gemini; 12 covers nearly every real game (a 12-flag game is already
  // catastrophic) while keeping the prompt size predictable.
  const SUMMARIZE_BATCH_MAX = 12;

  async function fetchMistakeExplanation(move, gameMoves, gameIndex) {
    if (!move?.fen_before) return;
    const uci = deriveUciForMove(move);
    if (!uci) { setReviewExplanation({ summary: "Could not derive move notation." }); return; }

    const cacheKey = summaryCacheKey(move.fen_before, uci);
    // Cache hit — show instantly, no network call
    if (summaryCache[cacheKey]) {
      setReviewExplanation(summaryCache[cacheKey]);
      return;
    }

    // Skip display updates if the user has navigated to a different move
    // since we started this call. Cache writes still happen so the work isn't wasted.
    const isStillSelected = () => reviewSelectedMoveRef.current === move;
    const safeSetExplanation = (val) => { if (isStillSelected()) setReviewExplanation(val); };
    const safeSetExplaining  = (val) => { if (isStillSelected()) setReviewExplaining(val); };

    setReviewExplaining(true);
    // If we have the game's full move list, batch ALL flagged moves at once.
    // First click pays a longer wait; every subsequent click in this game is free.
    if (gameMoves && gameIndex !== undefined && gameIndex !== null) {
      const flagged = flaggedMovesIn(gameMoves);
      // Ensure the clicked move is part of the batch even if it's beyond the cap —
      // put it first, then fill remaining slots with other flagged moves in order.
      // Carry the badge-level classification fields through so the backend
      // can frame the LLM explanation consistently with what the user clicked
      // (depth=8 badge), instead of reclassifying at depth=19 and producing
      // "Mistake badge + 'solid choice' verdict" mismatches.
      const allItems = flagged
        .map(m => ({
          fen: m.fen_before,
          uci: deriveUciForMove(m),
          label: m.label,
          severity: m.severity,
          missed_opportunity: m.missed_opportunity,
          missed_win: m.missed_win,
          created_problem: m.severity !== "Good",
          cp_delta: m.cp_delta,
          cp_before: m.cp_before,
          cp_after: m.cp_after,
          move_number: m.move_number,
          _isClicked: m === move,
        }))
        .filter(it => it.fen && it.uci);
      const clickedItem = allItems.find(it => it._isClicked);
      const otherItems = allItems.filter(it => !it._isClicked);
      const items = (clickedItem ? [clickedItem, ...otherItems] : allItems).slice(0, SUMMARIZE_BATCH_MAX);

      if (items.length >= 2 && batchInFlightGame !== gameIndex) {
        setBatchInFlightGame(gameIndex);
        try {
          const res = await axios.post(`${API}/summarize-batch`,
            { items: items.map(it => ({
                fen: it.fen, uci: it.uci, label: it.label,
                severity: it.severity,
                missed_opportunity: it.missed_opportunity,
                missed_win: it.missed_win,
                created_problem: it.created_problem,
                cp_delta: it.cp_delta,
                cp_before: it.cp_before,
                cp_after: it.cp_after,
                // Calibrate the coach's advice depth to the player's level.
                rating: multiGameData?.player_rating ?? null,
              })) },
            { timeout: 300000 });
          if (res.data?.ok && Array.isArray(res.data.summaries)) {
            const next = { ...summaryCache };
            // Only write entries that have an actual summary string. An empty
            // {} comes back when Gemini drops an item (returns fewer analyses
            // than requested, or SAN-validator strips all text fields). Writing
            // {} would poison the cache and skip the single-call fallback below.
            items.forEach((it, i) => {
              const s = res.data.summaries[i];
              if (s?.ok && s.ai_summary?.summary) {
                next[summaryCacheKey(it.fen, it.uci)] = s.ai_summary;
              }
            });
            setSummaryCache(next);
            try { localStorage.setItem("chess_coach_summaries_v6", JSON.stringify(next)); } catch {}
            const mine = next[cacheKey];
            if (mine) {
              safeSetExplanation(mine);
              safeSetExplaining(false);
              // Sync sidebar bestMove with the LLM's pick (depth=19 ground truth)
              // so it can't disagree with what the explanation says is best.
              if (reviewSelectedMoveRef.current === move) {
                const derived = bestMoveFromExplanation(mine, move.fen_before);
                if (derived) setReviewBestMove(derived);
              }
              setBatchInFlightGame(null);
              return;
            }
            // Batch didn't produce content for the clicked move (Gemini dropped
            // the item, or the move wasn't in `flagged`). Fall through to the
            // single-call so the user always gets an answer.
          }
          // Batch returned ok:false — fall through to single-call below
        } catch (e) {
          // Network/timeout — fall through to single-call below
        } finally {
          setBatchInFlightGame(null);
        }
      }
    }

    // Fallback: single-move call (when we don't have full game context, or
    // the batch failed). Same behavior as before.
    try {
      const sumRes = await axios.post(`${API}/summarize`, { fen: move.fen_before, uci, label: move.label, rating: multiGameData?.player_rating ?? null }, { timeout: 60000 });
      if (!sumRes.data?.ok) {
        safeSetExplanation({ summary: `Analysis error: ${sumRes.data?.error || "unknown"}` });
      } else {
        const ai = sumRes.data?.ai_summary;
        if (ai?.summary) {
          // Cache successful single calls too
          const next = { ...summaryCache, [cacheKey]: ai };
          setSummaryCache(next);
          try { localStorage.setItem("chess_coach_summaries_v6", JSON.stringify(next)); } catch {}
          if (reviewSelectedMoveRef.current === move) {
            const derived = bestMoveFromExplanation(ai, move.fen_before);
            if (derived) setReviewBestMove(derived);
          }
        }
        safeSetExplanation(ai?.summary ? ai : { summary: "No explanation returned from coach." });
      }
    } catch (e) { safeSetExplanation({ summary: `Request failed: ${e?.message || "unknown error"}` }); }
    safeSetExplaining(false);
  }

  function resetTryMode() {
    setTryStack([]); setTryCurrentFen(null); setTryResult(null);
    setTryExplanation(null); setTryExplaining(false);
  }

  async function onTryDrop(from, to, baseFen) {
    const uci = from + to;
    // Validate legality client-side first
    try { const c = new Chess(baseFen); if (!c.move({ from, to })) return false; } catch { return false; }

    setTryLoading(true); setTryExplanation(null);
    try {
      const res = await axios.post(`${API}/evaluate-move`, { fen: baseFen, uci }, { timeout: 30000 });
      if (!res.data?.ok) return false;
      setTryStack(prev => [...prev, { fen: baseFen, result: tryResult }]);
      setTryCurrentFen(res.data.fen_after);
      setTryResult(res.data);
      return true;
    } catch { return false; }
    finally { setTryLoading(false); }
  }

  async function onTryNext() {
    const fen = tryCurrentFen || reviewPositions[reviewPosIndex]?.fen;
    if (!fen) return;
    try {
      const res = await axios.post(`${API}/hint`, { fen });
      if (!res.data?.best_uci) return;
      const uci = res.data.best_uci;
      const c = new Chess(fen);
      const m = c.move({ from: uci.slice(0,2), to: uci.slice(2,4), promotion: uci[4]||undefined });
      if (!m) return;
      setTryStack(prev => [...prev, { fen, result: tryResult }]);
      setTryCurrentFen(c.fen());
      setTryResult(null); // opponent's move — no eval panel, just position
    } catch {}
  }

  function onTryUndo() {
    if (!tryStack.length) return;
    const prev = tryStack[tryStack.length - 1];
    setTryStack(s => s.slice(0, -1));
    setTryCurrentFen(prev.fen === reviewPositions[reviewPosIndex]?.fen ? null : prev.fen);
    setTryResult(prev.result);
    setTryExplanation(null);
  }

  async function fetchTryExplanation() {
    if (!tryResult) return;
    const { fen, uci, label } = tryResult;
    console.log("[try] calling /summarize with", { fen, uci, label });
    setTryExplaining(true);
    try {
      const res = await axios.post(`${API}/summarize`, { fen, uci, label }, { timeout: 60000 });
      console.log("[try] /summarize response", res.data);
      if (!res.data?.ok) {
        setTryExplanation({ summary: `Analysis error: ${res.data?.error || "unknown"}` });
        return;
      }
      const ai = res.data?.ai_summary;
      setTryExplanation(ai?.summary ? ai : { summary: "No explanation returned from coach." });
    } catch (e) {
      console.error("[try] /summarize failed", e);
      setTryExplanation({ summary: `Request failed: ${e?.message || "unknown error"}` });
    }
    finally { setTryExplaining(false); }
  }

  async function importFromChessDotCom() {
    if (!chesscomUsername.trim()) return;
    setImportingGame(true); setImportError(""); onReset();
    try {
      const res = await axios.post(`${API}/import-chessdotcom`, { username: chesscomUsername.trim() }, { timeout: 120000 });
      if (!res.data?.ok) { setImportError(res.data?.error || "Import failed."); return; }
      const { moves, game_info, weakness_summary, themes, explanation } = res.data;
      setMoveHistory(moves || []); setGameOver(true);
      if (weakness_summary) setAnalysisResult({ weakness_summary, themes: themes||[], explanation });
      else if (res.data.error) setAnalysisError(res.data.error);
      const { label: rLabel } = gameResult(game_info?.result);
      setCoach(`Imported: ${game_info?.white} vs ${game_info?.black} (${game_info?.time_class}) — ${rLabel}`);
    } catch (e) { setImportError(e?.response?.data?.detail || e?.message || "Request failed."); }
    finally { setImportingGame(false); }
  }

  async function onHint() {
    try { setBusy(true); const res = await axios.post(`${API}/hint`, { fen: game.fen() }); setCoach(res.data?.idea ? `Hint: ${res.data.idea}` : "No hint."); }
    catch { setMsg("Failed to get hint."); } finally { setBusy(false); }
  }

  async function handleReviewMove(move) {
    const g = new Chess(); g.load(move.fen_before); setGame(g);
    setLastUserMove(null); setLastCoachMove(null); setBestMoveHighlight(null);
    setReviewMove(move); setReviewHistoryIndex(moveHistory.indexOf(move) * 2); setReviewExplanation(null);
    try {
      const res = await axios.post(`${API}/hint`, { fen: move.fen_before });
      if (res.data?.best_uci) setBestMoveHighlight([res.data.best_uci.slice(0,2), res.data.best_uci.slice(2,4)]);
    } catch {}
    if (move.uci && move.label && move.label !== "Good") {
      setReviewExplaining(true);
      try {
        const sres = await axios.post(`${API}/summarize`, { fen: move.fen_before, uci: move.uci, label: move.label }, { timeout: 60000 });
        const ai = sres.data?.ai_summary;
        const text = ai?.summary || ai?.summary_text;
        if (text) setReviewExplanation(text);
      } catch {}
      setReviewExplaining(false);
    }
  }

  function reviewUndo() {
    if (reviewHistoryIndex === null || reviewHistoryIndex <= 0) return;
    const newIdx = reviewHistoryIndex - 1;
    const g = new Chess(); g.load(history[newIdx]); setGame(g);
    setReviewHistoryIndex(newIdx); setLastUserMove(null); setLastCoachMove(null);
  }
  function reviewRedo() {
    if (reviewHistoryIndex === null || reviewHistoryIndex >= history.length - 1) return;
    const newIdx = reviewHistoryIndex + 1;
    const g = new Chess(); g.load(history[newIdx]); setGame(g);
    setReviewHistoryIndex(newIdx); setLastUserMove(null); setLastCoachMove(null);
  }
  function exitReview() {
    const g = new Chess(); g.load(history[history.length-1]); setGame(g);
    setReviewMove(null); setReviewHistoryIndex(null); setBestMoveHighlight(null);
    setReviewExplanation(null); setLastUserMove(null); setLastCoachMove(null); setCoach("");
  }

  async function triggerGameAnalysis(moves) {
    if (!moves.length) return;
    setAnalyzing(true); setAnalysisError(null);
    try {
      const res = await axios.post(`${API}/analyze-game`, { moves }, { timeout: 60000 });
      if (res.data?.ok) setAnalysisResult({ weakness_summary: res.data.weakness_summary, themes: res.data.themes||[], explanation: res.data.explanation });
      else setAnalysisError(res.data?.error || "Analysis returned an error.");
    } catch (e) { setAnalysisError(e?.message || "Request failed."); }
    finally { setAnalyzing(false); }
  }

  async function onPieceDrop(from, to) {
    if (gameOver) return false;
    const fenBefore = game.fen();
    const temp = new Chess(game.fen());
    const fromPiece = (() => { try { return temp.get(from); } catch { return null; } })();
    const willPromote = !!(fromPiece && fromPiece.type === "p" && ((fromPiece.color==="w"&&to[1]==="8")||(fromPiece.color==="b"&&to[1]==="1")));
    const promotion = willPromote ? "q" : undefined;
    if (!temp.move({ from, to, ...(promotion ? { promotion } : {}) })) { setMsg("Illegal move."); return false; }
    try {
      setBusy(true); setMsg(""); setCoach(""); setRedoStack([]);
      const res = await axios.post(`${API}/play`, { fen: game.fen(), uci: from+to, ...(promotion?{promotion}:{}), skill: Number(skill), summary: false });
      if (!res.data?.ok) { setMsg(res.data?.error || "Move rejected."); return false; }
      const userMove = res.data.user;
      const newMoveHistory = [...moveHistory, { san: userMove?.san, uci: userMove?.uci, label: userMove?.label, cp_delta: userMove?.delta_cp, fen_before: fenBefore, move_number: moveHistory.length + 1 }];
      setMoveHistory(newMoveHistory);
      if (res.data.fen_after_user) {
        const g1 = new Chess(); g1.load(res.data.fen_after_user); setGame(g1);
        setHistory(h => [...h, g1.fen()]); setRedoStack([]); setLastUserMove([from,to]);
        if (res.data.cp_after_user) setCp({ white: res.data.cp_after_user.white, black: res.data.cp_after_user.black });
      }
      await new Promise(r => setTimeout(r, 800));
      if (res.data.fen_after_coach) {
        const g2 = new Chess(); g2.load(res.data.fen_after_coach); setGame(g2);
        setHistory(h => [...h, g2.fen()]); setRedoStack([]);
        const cu = res.data.coach?.uci;
        setLastCoachMove(cu?.length >= 4 ? [cu.slice(0,2), cu.slice(2,4)] : null);
      }
      if (res.data.cp_after_coach) setCp({ white: res.data.cp_after_coach.white, black: res.data.cp_after_coach.black });
      const latestFen = res.data.fen_after_coach || res.data.fen_after_user;
      if (latestFen) {
        const end = new Chess(); end.load(latestFen);
        if (end.isCheckmate() || end.isStalemate() || end.isDraw() || res.data.game_over) {
          const endMsg = end.isCheckmate() ? "Checkmate" : end.isStalemate() ? "Stalemate" : "Draw";
          setCoach(`Game over — ${endMsg}. Analyzing your game…`); setGameOver(true);
          const snap = [...history, ...(res.data.fen_after_user?[res.data.fen_after_user]:[]), ...(res.data.fen_after_coach?[res.data.fen_after_coach]:[])];
          localStorage.setItem("chess_coach_sample_game", JSON.stringify({ moveHistory: newMoveHistory, history: snap, finalFen: latestFen }));
          triggerGameAnalysis(newMoveHistory); return true;
        }
      }
      if (coachAnalysis) {
        const label = res.data.user?.label, delta = res.data.user?.delta_cp, idea = res.data.user?.idea;
        const evalText = label != null && delta != null ? `Your move: ${label} (${delta>0?"+":""}${delta} cp)` : "";
        if (label !== "Good") {
          setCoach([evalText, "Coach: Summarizing…"].filter(Boolean).join(" · "));
          (async () => {
            try {
              const sres = await axios.post(`${API}/summarize`, { fen: fenBefore, uci: from+to, ...(promotion?{promotion}:{}), label }, { timeout: 60000 });
              const ai = sres.data?.ai_summary, aiText = ai?.summary || ai?.summary_text;
              if (aiText) { setCoach([evalText, `Coach: ${aiText}`].filter(Boolean).join(" · ")); setLastSummary(ai); }
              else if (sres.data?.error) { setCoach([evalText, idea ? `Idea: ${idea}` : ""].filter(Boolean).join(" · ")); setLastSummary(null); }
            } catch { setCoach([evalText, idea ? `Idea: ${idea}` : ""].filter(Boolean).join(" · ")); setLastSummary(null); }
          })();
        } else { setCoach([evalText, idea ? `Idea: ${idea}` : ""].filter(Boolean).join(" · ")); setLastSummary(null); }
      }
      return true;
    } catch (err) {
      const status = err?.response?.status, detail = err?.response?.data || err?.message || "Unknown error";
      setMsg(`Backend error${status?` (${status})`:""}: ${typeof detail==="string"?detail:JSON.stringify(detail)}`);
      return false;
    } finally { setBusy(false); }
  }

  function loadSampleGame() {
    const raw = localStorage.getItem("chess_coach_sample_game"); if (!raw) return;
    const { moveHistory: savedMoves, history: savedHistory, finalFen } = JSON.parse(raw);
    const g = new Chess(); g.load(finalFen); setGame(g); setHistory(savedHistory); setMoveHistory(savedMoves);
    setRedoStack([]); setLastUserMove(null); setLastCoachMove(null); setSelectedSquare(null);
    setGameOver(true); setAnalyzing(false); setAnalysisResult(null); setAnalysisError(null);
    setMistakePage(0); setReviewMove(null); setReviewHistoryIndex(null); setBestMoveHighlight(null); setReviewExplanation(null);
    setCoach("Sample game loaded. Analyzing…"); triggerGameAnalysis(savedMoves);
  }

  function onReset() {
    const g = new Chess(); setGame(g); setMsg(""); setCoach(""); setHistory([g.fen()]); setRedoStack([]);
    setLastUserMove(null); setLastCoachMove(null); setSelectedSquare(null); setMoveHistory([]); setGameOver(false);
    setAnalyzing(false); setAnalysisResult(null); setAnalysisError(null); setMistakePage(0);
    setReviewMove(null); setReviewHistoryIndex(null); setBestMoveHighlight(null); setReviewExplanation(null);
    setSampleMode(false); setMode("game"); axios.post(`${API}/new-game`).catch(()=>{});
  }

  function onUndo() {
    if (busy || gameOver) return;
    setHistory(h => {
      if (h.length <= 1) return h;
      const prev = h[h.length-2]; setRedoStack(r => [...r, h[h.length-1]]);
      const g = new Chess(); g.load(prev); setGame(g); return h.slice(0,-1);
    });
    setMoveHistory(m => m.slice(0,-1)); setMsg(""); setCoach(""); setLastUserMove(null); setLastCoachMove(null); setSelectedSquare(null);
  }

  function onRedo() {
    if (busy || gameOver) return;
    setRedoStack(r => {
      if (!r.length) return r;
      const next = r[r.length-1]; setHistory(h => [...h, next]);
      const g = new Chess(); g.load(next); setGame(g); return r.slice(0,-1);
    });
    setMsg(""); setCoach(""); setLastUserMove(null); setLastCoachMove(null); setSelectedSquare(null);
  }

  // ── Puzzle mode ────────────────────────────────────────────────────────────

  async function startPuzzleSession(themes) {
    setLoadingPuzzles(true);
    try {
      const res = await axios.post(`${API}/puzzles`, { themes, count: 5 }, { timeout: 15000 });
      if (res.data?.ok && res.data.puzzles?.length) {
        setPuzzles(res.data.puzzles); setPuzzleIndex(0); setPuzzleScore(0); setPuzzleComplete(false);
        loadPuzzle(res.data.puzzles[0]); setMode("puzzle");
      } else { setMsg("No puzzles found for these themes."); }
    } catch { setMsg("Failed to load puzzles."); }
    finally { setLoadingPuzzles(false); }
  }

  function loadPuzzle(puzzle) {
    const moves = puzzle.moves.split(" "); const g = new Chess(puzzle.fen);
    if (moves.length > 0) try { g.move({ from: moves[0].slice(0,2), to: moves[0].slice(2,4), promotion: moves[0][4]||undefined }); } catch {}
    setPuzzleGame(g); setPuzzleMoveIndex(1); setPuzzleSolved(false); setPuzzleFailed(false); setPuzzleMsg("");
    setPuzzleHistory([g.fen()]); setPuzzleHistoryIndex(0); setPuzzleOrientation(g.turn()==="b"?"black":"white");
  }

  function onPuzzleDrop(from, to) {
    if (!puzzleGame || puzzleSolved) return false;
    const puzzle = puzzles[puzzleIndex], solutionMoves = puzzle.moves.split(" "), expectedUci = solutionMoves[puzzleMoveIndex];
    if (!expectedUci) return false;
    const fromPiece = (() => { try { return puzzleGame.get(from); } catch { return null; } })();
    const willPromote = !!(fromPiece && fromPiece.type==="p" && ((fromPiece.color==="w"&&to[1]==="8")||(fromPiece.color==="b"&&to[1]==="1")));
    const promotion = willPromote ? (expectedUci[4]||"q") : undefined;
    if (puzzleFailed) { setPuzzleFailed(false); setPuzzleMsg(""); }
    if (from+to !== expectedUci.slice(0,4)) { setPuzzleMsg("Incorrect — try another move."); setPuzzleFailed(true); return false; }
    const newG = new Chess(puzzleGame.fen());
    try { newG.move({ from, to, promotion }); } catch { setPuzzleMsg("Illegal move."); return false; }
    setPuzzleFailed(false);
    const nextMoveIndex = puzzleMoveIndex + 1;
    const appendHistory = fen => { setPuzzleHistory(h => [...h, fen]); setPuzzleHistoryIndex(i => i+1); };
    if (nextMoveIndex >= solutionMoves.length) {
      setPuzzleGame(newG); setPuzzleMoveIndex(nextMoveIndex); setPuzzleSolved(true); setPuzzleScore(s => s+1); setPuzzleMsg("Correct!"); appendHistory(newG.fen()); return true;
    }
    const opponentUci = solutionMoves[nextMoveIndex];
    try { newG.move({ from: opponentUci.slice(0,2), to: opponentUci.slice(2,4), promotion: opponentUci[4]||undefined }); } catch {}
    setPuzzleGame(newG); setPuzzleMoveIndex(nextMoveIndex+1); appendHistory(newG.fen());
    if (nextMoveIndex+1 >= solutionMoves.length) { setPuzzleSolved(true); setPuzzleScore(s => s+1); setPuzzleMsg("Correct!"); }
    return true;
  }

  function onNextPuzzle() {
    const nextIndex = puzzleIndex + 1;
    if (nextIndex >= puzzles.length) { setPuzzleComplete(true); return; }
    setPuzzleIndex(nextIndex); loadPuzzle(puzzles[nextIndex]);
  }

  // ── Square highlights ──────────────────────────────────────────────────────

  const customSquareStyles = useMemo(() => {
    if (mode === "puzzle") return {};
    const styles = {};
    const mark  = (sq, c) => { styles[sq] = { boxShadow: `inset 0 0 0 3px ${c}` }; };
    const shade = (sq, c) => { styles[sq] = { ...(styles[sq]||{}), background: c }; };
    if (selectedSquare) mark(selectedSquare, "#68d391");
    if (legalTargets?.length) for (const t of legalTargets) shade(t, "rgba(255,105,180,.35)");
    const atMistakePos = reviewMove && reviewHistoryIndex === moveHistory.indexOf(reviewMove)*2;
    if (bestMoveHighlight && atMistakePos) { mark(bestMoveHighlight[0], "#68d391"); mark(bestMoveHighlight[1], "#38a169"); }
    return styles;
  }, [selectedSquare, legalTargets, bestMoveHighlight, reviewMove, reviewHistoryIndex, mode]);

  const checkmatedKingSquare = useMemo(() => {
    if (mode === "puzzle") return null;
    try {
      const c = new Chess(game.fen()); if (!c.isCheckmate()) return null;
      const loser = c.turn(), board = c.board(), files = "abcdefgh";
      for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
        const sq = board[r][f]; if (sq && sq.type === "k" && sq.color === loser) return `${files[f]}${8-r}`;
      }
    } catch {}
    return null;
  }, [game.fen(), mode]);

  const BOARD_SIZE = 480;
  const squarePixel = BOARD_SIZE / 8;

  const overlayStyleForSquare = square => {
    if (!square) return {};
    const files = "abcdefgh", file = square[0], rank = Number(square[1]);
    return {
      position:"absolute", left: files.indexOf(file)*squarePixel, top: (8-rank)*squarePixel,
      width: squarePixel, height: squarePixel,
      background: "rgba(220,38,38,.65)", display:"flex", alignItems:"center", justifyContent:"center",
      color:"#fff", fontWeight:800, textTransform:"uppercase", textShadow:"0 1px 2px rgba(0,0,0,.6)",
      zIndex:5, pointerEvents:"none", fontSize:13,
    };
  };

  async function onSquareClickHandler(square) {
    if (busy || gameOver) return;
    const temp = new Chess(game.fen()), piece = temp.get(square);
    if (!selectedSquare) { if (piece && piece.color === temp.turn()) setSelectedSquare(square); return; }
    if (piece && piece.color === temp.turn()) { setSelectedSquare(square); return; }
    const ok = await onPieceDrop(selectedSquare, square);
    if (ok) setSelectedSquare(null);
  }

  // ══════════════════════════════════════════════════════════════════════════
  // THREAT DRILL TAB
  // ══════════════════════════════════════════════════════════════════════════

  if (activeTab === "drill") {
    // drillQuestions is hoisted via useMemo above (so the batch-explanations
    // useEffect can depend on it). Same shape — flat list of drill questions
    // across all analyzed games, filtered to those with a drill_question.
    const total = drillQuestions.length;
    const current = drillQuestions[drillIndex];

    if (total === 0) {
      return (
        <div style={{ maxWidth: 720, margin: "0 auto", padding: "20px 16px 60px" }}>
          <TabBar active={activeTab} onChange={setActiveTab} />
          <div style={{ textAlign: "center", marginTop: 60, color: "var(--text-secondary)" }}>
            <h2>Threat Drill</h2>
            <p style={{ marginTop: 16, lineHeight: 1.6 }}>
              No drill questions available yet.<br/>
              Go to <strong>Analyze My Games</strong> first — drill questions are generated
              from positions where you blundered in real games.
            </p>
            <button className="btn-primary" style={{ marginTop: 20 }} onClick={() => setActiveTab("analyze")}>
              Go to Analyze
            </button>
          </div>
        </div>
      );
    }

    // Resolve a SAN to its from/to squares using chess.js — needed for board
    // hover preview and post-pick highlighting. Returns null if SAN can't be parsed.
    function sanToSquares(fen, san) {
      try {
        const c = new Chess(fen);
        const found = c.moves({ verbose: true }).find(m => m.san === san);
        return found ? { from: found.from, to: found.to, piece: found.piece, color: found.color } : null;
      } catch { return null; }
    }

    // Stable shuffle of the 3 options per question, derived from the question's
    // fen so the order doesn't change between renders. Correct answer is the
    // first option in the original list; shuffle so it can land anywhere.
    function shuffleOptions(opts, seed) {
      let h = 0; for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) | 0;
      const arr = [...opts];
      for (let i = arr.length - 1; i > 0; i--) {
        h = (h * 1103515245 + 12345) & 0x7fffffff;
        const j = h % (i + 1);
        [arr[i], arr[j]] = [arr[j], arr[i]];
      }
      return arr;
    }

    // Per-phase data: which board to show, which options to choose between,
    // which state holds the user's pick, etc. Keeps the render body uniform.
    const correction = current.correction || null;
    const isThreatPhase = drillPhase === "threat";
    const phaseData = isThreatPhase
      ? {
          fen: current.fen_after,
          options: current.options,
          correctSan: current.correct_san,
          correctDesc: current.correct_desc,
          picked: drillThreatPicked,
          setPicked: setDrillThreatPicked,
          opponentColor: current.userColor === "white" ? "Black" : "White",
          // Threat phase always shows the user's last move in purple as context.
          showUserMoveOverlay: true,
        }
      : {
          fen: correction?.fen,
          options: correction?.options || [],
          correctSan: correction?.correct_san,
          correctDesc: correction?.correct_desc,
          picked: drillCorrectionPicked,
          setPicked: setDrillCorrectionPicked,
          opponentColor: null,  // not used in correction phase
          // Correction phase = "rewind to before you moved" — don't show the bad move
          // in purple (would be confusing since user hasn't played anything yet here).
          showUserMoveOverlay: false,
        };

    const shuffled = shuffleOptions(phaseData.options, phaseData.fen || "");
    const isPicked = phaseData.picked !== null;
    const isCorrect = phaseData.picked === phaseData.correctSan;

    function pickOption(san) {
      if (phaseData.picked) return;
      phaseData.setPicked(san);
      // Record the result for stats. We tally per motif, per phase.
      const motif = current.motif || "uncategorized";
      const correct = san === phaseData.correctSan;
      const next = { ...drillStats };
      next[motif] = next[motif] || { threats_correct: 0, threats_total: 0, corrections_correct: 0, corrections_total: 0 };
      if (isThreatPhase) {
        next[motif].threats_total += 1;
        if (correct) next[motif].threats_correct += 1;
      } else {
        next[motif].corrections_total += 1;
        if (correct) next[motif].corrections_correct += 1;
      }
      setDrillStats(next);
      try { localStorage.setItem("chess_coach_drill_stats", JSON.stringify(next)); } catch {}
    }

    function advanceToCorrection() {
      setDrillPhase("correction");
      setDrillHoveredSan(null);
    }

    function nextQuestion() {
      setDrillPhase("threat");
      setDrillThreatPicked(null);
      setDrillCorrectionPicked(null);
      setDrillHoveredSan(null);
      setDrillIndex(i => (i + 1) % total);
    }

    function buildArrows() {
      const arrows = [];
      if (isPicked) {
        const correctSq = sanToSquares(phaseData.fen, phaseData.correctSan);
        if (correctSq) arrows.push([correctSq.from, correctSq.to, "rgb(34,197,94)"]);
        if (phaseData.picked !== phaseData.correctSan) {
          const pickedSq = sanToSquares(phaseData.fen, phaseData.picked);
          if (pickedSq) arrows.push([pickedSq.from, pickedSq.to, "rgb(239,68,68)"]);
        }
      } else if (drillHoveredSan) {
        const sq = sanToSquares(phaseData.fen, drillHoveredSan);
        if (sq) arrows.push([sq.from, sq.to, "rgb(59,130,246)"]);
      }
      return arrows;
    }

    function buildSquareStyles() {
      const styles = {};
      // Purple context overlay only on the threat board (the user's bad move is
      // the position-setting move there). Hidden on correction board.
      if (phaseData.showUserMoveOverlay && current.fenBeforeUserMove && current.userSan) {
        const userSq = sanToSquares(current.fenBeforeUserMove, current.userSan);
        if (userSq) {
          styles[userSq.from] = { background: "rgba(168,85,247,.55)" };
          styles[userSq.to]   = { background: "rgba(168,85,247,.55)" };
        }
      }
      const sources = [];
      if (isPicked) {
        sources.push({ san: phaseData.correctSan, color: "rgba(34,197,94,.55)" });
        if (phaseData.picked !== phaseData.correctSan) {
          sources.push({ san: phaseData.picked, color: "rgba(239,68,68,.55)" });
        }
      } else if (drillHoveredSan) {
        sources.push({ san: drillHoveredSan, color: "rgba(59,130,246,.55)" });
      }
      for (const s of sources) {
        const sq = sanToSquares(phaseData.fen, s.san);
        if (sq) {
          styles[sq.from] = { background: s.color };
          styles[sq.to]   = { background: s.color };
        }
      }
      return styles;
    }

    return (
      <div style={{ maxWidth: 760, margin: "0 auto", padding: "20px 16px 60px" }}>
        <TabBar active={activeTab} onChange={setActiveTab} />

        {/* Header */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 8 }}>
          <h2 style={{ margin: 0 }}>Threat Drill</h2>
          <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>
            Q{drillIndex + 1} of {total}
          </span>
        </div>

        {/* Game / move context — what game, outcome, blunder/miss classification */}
        {(() => {
          const r = gameResult(current.gameResult);
          const labelLower = (current.label || "").toLowerCase();
          let labelClass = "good";
          if (labelLower.includes("blunder")) labelClass = "blunder";
          else if (labelLower.includes("mistake")) labelClass = "mistake";
          else if (labelLower.includes("inaccuracy")) labelClass = "inaccuracy";
          else if (labelLower.includes("miss")) labelClass = "miss";
          return (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", marginBottom: 12, fontSize: 12 }}>
              <span style={{ color: "var(--text-secondary)" }}>
                <strong>Game {current.gameIndex + 1}</strong> vs <strong>{current.opponentName || "?"}</strong>
              </span>
              <span className={r.cls} style={{ fontWeight: 600 }}>{r.label}</span>
              <span style={{ color: "var(--text-muted)" }}>·</span>
              <span style={{ color: "var(--text-secondary)" }}>Move <strong>{current.moveNumber}</strong></span>
              <span className={`badge badge-${labelClass}`}>{current.label || current.severity}</span>
              {current.motif && (
                <>
                  <span style={{ color: "var(--text-muted)" }}>·</span>
                  <span style={{ color: "var(--text-secondary)", fontFamily: "monospace", fontSize: 11 }}>
                    {current.motif}
                  </span>
                </>
              )}
              {current.opening && (
                <>
                  <span style={{ color: "var(--text-muted)" }}>·</span>
                  <span style={{ color: "var(--text-muted)", fontStyle: "italic" }}>{current.opening}</span>
                </>
              )}
            </div>
          );
        })()}

        {/* Phase indicator + question prompt */}
        {isThreatPhase ? (
          <p style={{ marginTop: 0, marginBottom: 18, fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.6 }}>
            <span style={{ color: "var(--text-muted)", fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5, marginRight: 8 }}>
              Step 1 of 2 · Threat
            </span><br/>
            You played <strong style={{ fontFamily: "monospace" }}>{current.userSan}</strong> <span style={{ color: "var(--text-muted)" }}>(highlighted in purple)</span>.
            It's {phaseData.opponentColor}'s turn. <strong>What is {phaseData.opponentColor}'s strongest reply?</strong>
          </p>
        ) : (
          <p style={{ marginTop: 0, marginBottom: 18, fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.6 }}>
            <span style={{ color: "var(--text-muted)", fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5, marginRight: 8 }}>
              Step 2 of 2 · Correction
            </span><br/>
            Knowing the opponent threatens <strong style={{ fontFamily: "monospace" }}>{current.correct_san}</strong> ({current.correct_desc}),
            rewind to before your move. <strong>What should you have played instead of {current.userSan}?</strong>
          </p>
        )}

        <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: 24, alignItems: "start" }}>
          {/* Board */}
          <div style={{ width: 360 }}>
            <Chessboard
              position={phaseData.fen}
              boardOrientation={current.userColor === "black" ? "black" : "white"}
              arePiecesDraggable={false}
              boardWidth={360}
              customArrows={buildArrows()}
              customSquareStyles={buildSquareStyles()}
            />
          </div>

          {/* Options + reveal */}
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {shuffled.map((opt, i) => {
              const picked = phaseData.picked === opt.san;
              const correct = opt.san === phaseData.correctSan;
              let bg = "var(--bg-card)", border = "1px solid var(--border-light)", color = "var(--text-primary)";
              if (isPicked) {
                if (correct) { bg = "rgba(34,197,94,.15)"; border = "1px solid var(--green)"; }
                else if (picked) { bg = "rgba(239,68,68,.15)"; border = "1px solid var(--red)"; }
                else { color = "var(--text-muted)"; }
              }
              const sq = sanToSquares(phaseData.fen, opt.san);
              const pieceLabel = sq ? `${({p:"pawn",n:"knight",b:"bishop",r:"rook",q:"queen",k:"king"})[sq.piece]} ${sq.from}→${sq.to}` : "";
              return (
                <button
                  key={opt.san}
                  onClick={() => pickOption(opt.san)}
                  onMouseEnter={() => !isPicked && setDrillHoveredSan(opt.san)}
                  onMouseLeave={() => !isPicked && setDrillHoveredSan(null)}
                  disabled={isPicked}
                  style={{
                    background: bg, border, color,
                    padding: "12px 14px", borderRadius: "var(--radius)",
                    textAlign: "left", fontSize: 14, fontFamily: "monospace",
                    cursor: isPicked ? "default" : "pointer",
                    display: "flex", justifyContent: "space-between", alignItems: "center",
                  }}
                >
                  <span>
                    {String.fromCharCode(65 + i)}.&nbsp; <strong>{opt.san}</strong>
                    {pieceLabel && (
                      <span style={{ marginLeft: 8, fontSize: 11, color: "var(--text-muted)", fontFamily: "inherit" }}>
                        {pieceLabel}
                      </span>
                    )}
                  </span>
                  {isPicked && (
                    <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
                      eval {opt.eval_cp >= 0 ? "+" : ""}{opt.eval_cp} cp
                    </span>
                  )}
                </button>
              );
            })}

            {isPicked && (() => {
              // Prefer Gemini's richer explanation; fall back to the mechanical
              // python-chess description if the batch call hasn't returned (or
              // failed). Loading state shows when current question is in flight.
              const llm = drillExplanations[current.fen_after] || {};
              const llmText = isThreatPhase ? llm.threat_explanation : llm.correction_explanation;
              const showLoading = drillLoadingExplanations && !llmText;
              const explanation = llmText || phaseData.correctDesc;
              return (
                <div style={{ marginTop: 8, padding: "12px 14px", background: "var(--bg-card-alt)", borderRadius: "var(--radius)" }}>
                  <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6, color: isCorrect ? "var(--green)" : "var(--orange)" }}>
                    {isCorrect ? "✓ Correct" : "✗ Best was " + phaseData.correctSan}
                  </div>
                  <div style={{ fontSize: 13, lineHeight: 1.5, color: "var(--text-secondary)" }}>
                    {explanation}
                    {showLoading && (
                      <div style={{ marginTop: 6, fontSize: 11, color: "var(--text-muted)", fontStyle: "italic" }}>
                        Coach is generating a richer explanation…
                      </div>
                    )}
                  </div>

                  <div style={{ marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--border-light)" }}>
                    {isThreatPhase && correction ? (
                      <button className="btn-primary" onClick={advanceToCorrection}>
                        Next: what should you have played? →
                      </button>
                    ) : (
                      <button className="btn-primary" onClick={nextQuestion}>
                        Next question →
                      </button>
                    )}
                  </div>
                </div>
              );
            })()}
          </div>
        </div>

        {/* Motif stats footer — threat vs correction broken out */}
        {Object.keys(drillStats).length > 0 && (
          <div style={{ marginTop: 28, padding: "14px 16px", background: "var(--bg-card)", borderRadius: "var(--radius-lg)" }}>
            <div style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: 10, fontWeight: 600 }}>YOUR DRILL PROGRESS</div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 14, fontSize: 12 }}>
              {Object.entries(drillStats).map(([motif, s]) => {
                const tt = s.threats_total || 0, tc = s.threats_correct || 0;
                const ct = s.corrections_total || 0, cc = s.corrections_correct || 0;
                const tPct = tt > 0 ? Math.round(tc / tt * 100) : null;
                const cPct = ct > 0 ? Math.round(cc / ct * 100) : null;
                return (
                  <div key={motif} style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                    <span style={{ fontFamily: "monospace", color: "var(--text-muted)", fontSize: 11 }}>{motif}</span>
                    {tPct !== null && <span>Threat spotted: <strong>{tPct}%</strong> ({tc}/{tt})</span>}
                    {cPct !== null && <span>Best move found: <strong>{cPct}%</strong> ({cc}/{ct})</span>}
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>
    );
  }

  // ══════════════════════════════════════════════════════════════════════════
  // ANALYZE TAB
  // ══════════════════════════════════════════════════════════════════════════

  if (activeTab === "analyze") {
    const weakness = selectedWeaknessId ? multiGameData?.common_weaknesses.find(w => w.id === selectedWeaknessId) : null;
    const gamesForWeakness = weakness
      ? weakness.occurrences.map(o => ({ ...o, game: multiGameData.games[o.game_index] })).filter(o => o.game)
      : [];
    const selectedGame = selectedGameIndex !== null ? multiGameData?.games[selectedGameIndex] : null;
    const selectedOccurrence = weakness?.occurrences.find(o => o.game_index === selectedGameIndex);
    const REVIEW_BOARD = 360;

    return (
      <div style={{ maxWidth: 1060, margin: "0 auto", padding: "20px 16px 60px", fontFamily: "inherit" }}>
        <TabBar active={activeTab} onChange={setActiveTab} />

        {/* ── Search bar ── */}
        <div style={{ display:"flex", gap:8, marginBottom:20, alignItems:"center", flexWrap:"wrap" }}>
          <input
            type="text" value={savedUsername} onChange={e => setSavedUsername(e.target.value)}
            placeholder="Chess.com username" style={{ width:180 }} disabled={analyzingGames}
            onKeyDown={e => e.key==="Enter" && analyzeMyGames(savedUsername)}
          />
          <button className="btn-primary" onClick={() => analyzeMyGames(savedUsername)} disabled={analyzingGames || !savedUsername.trim()}>
            {analyzingGames ? "Analyzing…" : "Analyze last 10 games"}
          </button>
          {multiGameData && !analyzingGames && (
            <>
              <span style={{ fontSize:12, color:"var(--text-muted)" }}>
                {multiGameData.games.length} games
                {analysisCachedAt && ` · cached ${new Date(analysisCachedAt).toLocaleDateString()}`}
              </span>
              <button onClick={() => analyzeMyGames(savedUsername, true)} style={{ fontSize:12, padding:"4px 10px" }}>
                Re-analyze
              </button>
            </>
          )}
        </div>

        {/* Loading */}
        {analyzingGames && (
          <div className="loading-state">
            <span className="spinner" style={{ width:28, height:28, borderWidth:3 }} />
            <div>
              <div style={{ fontWeight:600, marginBottom:4 }}>Analyzing your last 10 games…</div>
              <div style={{ fontSize:12, color:"var(--text-muted)" }}>Stockfish + Gemini · takes 2–3 minutes</div>
            </div>
          </div>
        )}

        {analyzeError && !analyzingGames && (
          <div style={{ background:"var(--red-bg)", border:"1px solid var(--red-border)", borderRadius:"var(--radius)", padding:"10px 14px", color:"var(--red)", fontSize:13 }}>
            {analyzeError}
          </div>
        )}

        {multiGameData && !analyzingGames && (
          <>
            {/* ── Player summary card ── */}
            {(multiGameData.player_stats || multiGameData.player_summary) && (
              <PlayerSummary username={savedUsername} data={multiGameData} />
            )}

            {/* ── Game detail (3-column) ── */}
            {selectedGame && weakness && (() => {
              const info = selectedGame.game_info;
              const { label: resLabel, cls: resCls } = gameResult(info.result);
              // Filter on severity (base label only — not composite "Blunder + Miss" strings)
              // or missed_opportunity, so we catch every flagged move.
              const badMoves = selectedGame.moves.filter(m =>
                ["Mistake","Blunder"].includes(m.severity) || m.missed_opportunity
              );
              return (
                <div>
                  <button className="btn-ghost" style={{ marginBottom:12, display:"flex", alignItems:"center", gap:4 }}
                    onClick={() => { setSelectedGameIndex(null); setReviewFen(null); setReviewMoveNumber(null); }}>
                    ← Back to {weakness.label}
                  </button>

                  {/* Game header */}
                  <div className="game-header" style={{ marginBottom:14 }}>
                    <div style={{ display:"flex", justifyContent:"space-between", alignItems:"flex-start", flexWrap:"wrap", gap:8 }}>
                      <div>
                        <div style={{ fontWeight:700, fontSize:15, marginBottom:3 }}>
                          {info.white} <span style={{ color:"var(--text-muted)", fontWeight:400 }}>vs</span> {info.black}
                          <span style={{ color:"var(--text-secondary)", fontWeight:400, fontSize:12, marginLeft:8 }}>({info.time_class})</span>
                        </div>
                        <div style={{ display:"flex", gap:12, fontSize:12, color:"var(--text-secondary)", flexWrap:"wrap" }}>
                          {info.date && <span>{info.date}</span>}
                          <span>You played <strong>{info.user_color}</strong></span>
                          {info.opening && <span>{info.opening}</span>}
                        </div>
                      </div>
                      <div style={{ display:"flex", gap:12, alignItems:"center" }}>
                        <span style={{ fontWeight:700, fontSize:14 }} className={resCls}>{resLabel}</span>
                        {info.url && <a href={info.url} target="_blank" rel="noreferrer" style={{ fontSize:12 }}>View on Chess.com ↗</a>}
                      </div>
                    </div>
                    <div style={{ marginTop:8, background:"var(--orange-bg)", border:"1px solid var(--orange-border)", borderRadius:"var(--radius-sm)", padding:"6px 10px", fontSize:12, color:"var(--orange)" }}>
                      <strong>{weakness.label}:</strong> {weakness.description}
                    </div>
                  </div>

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
                            <button key={i} onClick={() => selectMistakeForReview(m, selectedGame.moves)}
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

                    {/* Col 2: board + nav */}
                    <div style={{ flexShrink:0 }}>
                      {/* Position label / try-mode indicator */}
                      <div style={{ fontSize:11, marginBottom:5, minHeight:16, display:"flex", justifyContent:"space-between", alignItems:"center" }}>
                        <span style={{ color: tryCurrentFen ? "var(--blue)" : "var(--text-muted)" }}>
                          {tryCurrentFen
                            ? "Try mode — drag pieces to explore"
                            : reviewPositions[reviewPosIndex]?.label || "Select a mistake →"}
                        </span>
                        {tryCurrentFen && (
                          <span style={{ fontSize:10, color:"var(--blue)", fontWeight:600, letterSpacing:".05em", textTransform:"uppercase" }}>
                            Try Mode
                          </span>
                        )}
                      </div>

                      <Chessboard
                        position={tryCurrentFen || reviewPositions[reviewPosIndex]?.fen || "start"}
                        boardWidth={REVIEW_BOARD}
                        arePiecesDraggable={!!reviewMoveNumber && !tryLoading}
                        onPieceDrop={(from, to) => {
                          const baseFen = tryCurrentFen || reviewPositions[reviewPosIndex]?.fen;
                          if (!baseFen) return false;
                          onTryDrop(from, to, baseFen);
                          return true; // optimistic — board reverts if call fails
                        }}
                        boardOrientation={info.user_color === "black" ? "black" : "white"}
                        animationDuration={150}
                        customArrows={
                          !tryCurrentFen && reviewPositions[reviewPosIndex]?.bestMove
                            ? [[
                                reviewPositions[reviewPosIndex].bestMove.from,
                                reviewPositions[reviewPosIndex].bestMove.to,
                                "rgb(163,213,255)"
                              ]]
                            : []
                        }
                      />

                      {/* Review nav (hidden in try mode) */}
                      {!tryCurrentFen && reviewPositions.length > 0 && (
                        <div style={{ display:"flex", gap:6, marginTop:8, alignItems:"center", flexWrap:"wrap" }}>
                          <button onClick={() => setReviewPosIndex(i => Math.max(0, i-1))} disabled={reviewPosIndex===0} style={{ padding:"5px 12px", fontSize:12 }}>←</button>
                          <button onClick={() => setReviewPosIndex(i => Math.min(reviewPositions.length-1, i+1))} disabled={reviewPosIndex===reviewPositions.length-1} style={{ padding:"5px 12px", fontSize:12 }}>→</button>
                          <button onClick={() => setReviewPosIndex(0)} disabled={reviewPosIndex===0} style={{ padding:"5px 10px", fontSize:11 }}>Reset</button>
                          <span style={{ fontSize:11, color:"var(--text-muted)", marginLeft:2 }}>{reviewPosIndex+1}/{reviewPositions.length}</span>
                        </div>
                      )}

                      {tryLoading && (
                        <div style={{ display:"flex", alignItems:"center", gap:8, marginTop:8, fontSize:12, color:"var(--text-muted)" }}>
                          <span className="spinner" /> Evaluating…
                        </div>
                      )}

                      {reviewMoveNumber && !reviewFen && !reviewPositions.length && (
                        <div style={{ marginTop:10, background:"var(--red-bg)", border:"1px solid var(--red-border)", borderRadius:"var(--radius-sm)", padding:"8px 10px", fontSize:12, color:"var(--red)" }}>
                          Board data not available — click <strong>Re-analyze</strong>.
                        </div>
                      )}
                    </div>

                    {/* Col 3: try-mode panel or coach explanation */}
                    <div style={{ paddingTop:22 }}>
                      <div style={{ fontSize:11, fontWeight:700, letterSpacing:".06em", textTransform:"uppercase", color:"var(--text-muted)", marginBottom:10 }}>
                        {tryCurrentFen ? "Move Evaluation" : "Coach Analysis"}
                      </div>
                      {tryCurrentFen && tryResult ? (
                        <TryModePanel
                          result={tryResult}
                          explaining={tryExplaining}
                          explanation={tryExplanation}
                          onAnalyze={tryResult && !tryExplanation && !tryExplaining ? fetchTryExplanation : null}
                          onNext={onTryNext}
                          onUndo={onTryUndo}
                          onReset={resetTryMode}
                          canUndo={tryStack.length > 0}
                        />
                      ) : tryCurrentFen && !tryResult ? (
                        // Opponent just played — show prompt to try next move
                        <div style={{ display:"flex", flexDirection:"column", gap:10 }}>
                          <div style={{ fontSize:13, color:"var(--text-secondary)" }}>
                            Opponent played. Drag a piece to try your next move.
                          </div>
                          <div style={{ display:"flex", gap:6 }}>
                            <button onClick={onTryUndo} disabled={!tryStack.length} style={{ fontSize:12, padding:"5px 12px" }}>← Undo</button>
                            <button onClick={resetTryMode} style={{ fontSize:12, padding:"5px 12px", color:"var(--text-muted)" }}>Reset position</button>
                          </div>
                        </div>
                      ) : (
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
                      )}
                    </div>
                  </div>
                </div>
              );
            })()}

            {/* ── Weakness game list ── */}
            {weakness && !selectedGame && (
              <div>
                <button className="btn-ghost" style={{ marginBottom:14, display:"flex", alignItems:"center", gap:4 }} onClick={() => setSelectedWeaknessId(null)}>
                  ← Back to all weaknesses
                </button>
                <div style={{ marginBottom:16 }}>
                  <h3 style={{ marginBottom:4 }}>{weakness.label}</h3>
                  <p style={{ margin:"0 0 6px", fontSize:13, color:"var(--text-secondary)", lineHeight:1.6 }}>{weakness.description}</p>
                  <span style={{ fontSize:12, color:"var(--text-muted)" }}>Found in <strong style={{ color:"var(--text-primary)" }}>{weakness.game_count}</strong> of {multiGameData.games.length} games</span>
                </div>
                <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
                  {gamesForWeakness.map(({ game_index, move_numbers, game }) => {
                    const info = game.game_info;
                    const opponent = info.user_color === "white" ? info.black : info.white;
                    const { label: resLabel, cls: resCls } = gameResult(info.result);
                    return (
                      <button key={game_index} className="weakness-card"
                        onClick={() => { setSelectedGameIndex(game_index); setReviewFen(null); setReviewMoveNumber(null); }}
                        style={{ display:"flex", justifyContent:"space-between", alignItems:"center" }}>
                        <div>
                          <div style={{ fontWeight:600, marginBottom:2 }}>vs {opponent}</div>
                          <div style={{ fontSize:12, color:"var(--text-secondary)" }}>
                            {info.date && <span style={{ marginRight:6 }}>{info.date} ·</span>}
                            {info.time_class}{info.opening ? ` · ${info.opening}` : ""}
                          </div>
                        </div>
                        <div style={{ textAlign:"right" }}>
                          <div className={`${resCls}`} style={{ fontWeight:700, fontSize:13, marginBottom:2 }}>{resLabel}</div>
                          <div style={{ fontSize:11, color:"var(--text-muted)" }}>{move_numbers.length} move{move_numbers.length!==1?"s":""} flagged</div>
                        </div>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            {/* ── Weakness overview ── */}
            {!selectedWeaknessId && (
              <div>
                <div style={{ marginBottom:16 }}>
                  <h3 style={{ marginBottom:4 }}>Your recurring weaknesses</h3>
                  <p style={{ margin:0, fontSize:13, color:"var(--text-secondary)" }}>
                    Based on your last {multiGameData.games.length} games · click any card to drill down
                  </p>
                </div>
                <div style={{ display:"flex", flexDirection:"column", gap:10 }}>
                  {multiGameData.common_weaknesses.map((w, i) => {
                    const freq = Math.round((w.game_count / multiGameData.games.length) * 100);
                    const colors = ["var(--red)","var(--orange)","var(--yellow)","var(--green)"];
                    const color  = colors[Math.min(i, colors.length-1)];
                    return (
                      <button key={w.id} className="weakness-card" onClick={() => setSelectedWeaknessId(w.id)}>
                        <div style={{ display:"flex", justifyContent:"space-between", alignItems:"flex-start", marginBottom:6 }}>
                          <span style={{ fontWeight:700, fontSize:14 }}>{w.label}</span>
                          <span style={{ fontSize:12, color:"var(--text-muted)", whiteSpace:"nowrap", marginLeft:8 }}>
                            {w.game_count}/{multiGameData.games.length} games
                          </span>
                        </div>
                        <p style={{ margin:"0 0 8px", fontSize:13, color:"var(--text-secondary)", lineHeight:1.6 }}>{w.description}</p>
                        <div className="weakness-freq-bar">
                          <div className="weakness-freq-fill" style={{ width:`${freq}%`, background:color }} />
                        </div>
                      </button>
                    );
                  })}
                  {multiGameData.common_weaknesses.length === 0 && (
                    <div className="card" style={{ textAlign:"center", padding:"32px", color:"var(--text-secondary)" }}>
                      {multiGameData.weaknesses_error
                        ? "Weakness analysis couldn't run — the AI assistant hit its daily request limit. The games and stats above are still accurate; try the weakness analysis again after the limit resets (usually within a day)."
                        : "No significant recurring weaknesses found. You're playing well!"}
                    </div>
                  )}
                </div>
              </div>
            )}
          </>
        )}

        {/* Empty state */}
        {!multiGameData && !analyzingGames && !analyzeError && (
          <div style={{ textAlign:"center", padding:"60px 0", color:"var(--text-secondary)" }}>
            <div style={{ fontSize:48, marginBottom:12 }}>♟</div>
            <div style={{ fontWeight:600, marginBottom:6 }}>Enter your Chess.com username to get started</div>
            <div style={{ fontSize:13, color:"var(--text-muted)" }}>We'll fetch your last 10 games and identify your patterns</div>
          </div>
        )}
      </div>
    );
  }

  // ══════════════════════════════════════════════════════════════════════════
  // PUZZLE MODE
  // ══════════════════════════════════════════════════════════════════════════

  if (mode === "puzzle") {
    const puzzle = puzzles[puzzleIndex];
    const sideToMove = puzzleGame ? (puzzleGame.turn()==="w" ? "White" : "Black") : "?";
    if (puzzleComplete) {
      return (
        <div style={{ maxWidth:520, margin:"40px auto", fontFamily:"inherit", textAlign:"center" }}>
          <TabBar active={activeTab} onChange={setActiveTab} />
          <div style={{ fontSize:52, marginBottom:12 }}>{puzzleScore>=4?"🏆":puzzleScore>=2?"👍":"💪"}</div>
          <h2 style={{ marginBottom:6 }}>Puzzle Session Complete</h2>
          <p style={{ fontSize:18, color:"var(--text-secondary)", marginBottom:20 }}>
            You solved <strong style={{ color:"var(--text-primary)" }}>{puzzleScore}/{puzzles.length}</strong> puzzles
          </p>
          {analysisResult && (
            <p style={{ fontSize:13, color:"var(--text-muted)", marginBottom:20 }}>
              Focus area: <strong style={{ color:"var(--text-secondary)" }}>{(analysisResult.themes||[]).map(t=>THEME_LABELS[t]||t).join(", ")}</strong>
            </p>
          )}
          <div style={{ display:"flex", gap:10, justifyContent:"center" }}>
            <button className="btn-primary" onClick={() => startPuzzleSession(analysisResult?.themes||["crushing"])}>Practice Again</button>
            <button onClick={onReset}>New Game</button>
          </div>
        </div>
      );
    }
    return (
      <div style={{ maxWidth:560, margin:"20px auto", fontFamily:"inherit", padding:"0 12px" }}>
        <TabBar active={activeTab} onChange={setActiveTab} />
        <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:8 }}>
          <h2>Puzzle Training</h2>
          <span style={{ fontSize:13, color:"var(--text-secondary)" }}>{puzzleIndex+1}/{puzzles.length}</span>
        </div>
        <div style={{ width:"100%", height:5, background:"var(--bg-card)", borderRadius:3, marginBottom:14, overflow:"hidden" }}>
          <div style={{ height:5, borderRadius:3, background:"var(--blue)", width:`${(puzzleIndex/puzzles.length)*100}%`, transition:"width .3s" }} />
        </div>
        {analysisResult && (
          <div style={{ background:"var(--blue-bg)", border:"1px solid var(--blue-border)", borderRadius:"var(--radius)", padding:"7px 12px", marginBottom:12, fontSize:12, color:"var(--text-secondary)" }}>
            Focus: <strong style={{ color:"var(--text-primary)" }}>{(analysisResult.themes||[]).map(t=>THEME_LABELS[t]||t).join(", ")}</strong>
            {puzzle && <span style={{ color:"var(--text-muted)" }}> · Rating: {puzzle.rating}</span>}
          </div>
        )}
        <p style={{ margin:"0 0 8px", fontWeight:600 }}>{sideToMove} to move — find the best sequence.</p>
        <div style={{ position:"relative", width:BOARD_SIZE, height:BOARD_SIZE }}>
          <Chessboard
            position={puzzleHistory.length>0 ? puzzleHistory[puzzleHistoryIndex] : (puzzleGame?puzzleGame.fen():"start")}
            onPieceDrop={puzzleHistoryIndex===puzzleHistory.length-1 ? onPuzzleDrop : ()=>false}
            arePiecesDraggable={!puzzleSolved && puzzleHistoryIndex===puzzleHistory.length-1}
            boardWidth={BOARD_SIZE} animationDuration={200} boardOrientation={puzzleOrientation}
          />
        </div>
        <div style={{ marginTop:10, minHeight:28 }}>
          {puzzleMsg && <p style={{ color:puzzleSolved?"var(--green)":"var(--red)", fontWeight:600, margin:0 }}>{puzzleSolved?"✓ ":"✗ "}{puzzleMsg}</p>}
        </div>
        <div style={{ display:"flex", gap:8, marginTop:8, alignItems:"center" }}>
          {puzzleSolved && <button className="btn-primary" onClick={onNextPuzzle}>{puzzleIndex+1>=puzzles.length?"See Results":"Next Puzzle"}</button>}
          {puzzleFailed && !puzzleSolved && <button onClick={() => loadPuzzle(puzzle)} style={{ fontSize:12 }}>Restart puzzle</button>}
          <div style={{ display:"flex", gap:4, marginLeft:"auto" }}>
            <button onClick={() => setPuzzleHistoryIndex(i=>Math.max(0,i-1))} disabled={puzzleHistoryIndex===0} style={{ padding:"4px 10px" }}>←</button>
            <button onClick={() => setPuzzleHistoryIndex(i=>Math.min(puzzleHistory.length-1,i+1))} disabled={puzzleHistoryIndex===puzzleHistory.length-1} style={{ padding:"4px 10px" }}>→</button>
          </div>
          <button onClick={onReset}>New Game</button>
        </div>
      </div>
    );
  }

  // ══════════════════════════════════════════════════════════════════════════
  // PLAY TAB
  // ══════════════════════════════════════════════════════════════════════════

  // Build paired move list for notation panel
  const pairedMoves = useMemo(() => {
    const pairs = [];
    for (let i = 0; i < moveHistory.length; i += 2) {
      pairs.push({ num: Math.floor(i/2)+1, white: moveHistory[i], black: moveHistory[i+1]||null });
    }
    return pairs;
  }, [moveHistory]);

  return (
    <div style={{ maxWidth:940, margin:"0 auto", padding:"20px 12px 60px", fontFamily:"inherit" }}>
      <TabBar active={activeTab} onChange={setActiveTab} />

      {/* Controls row */}
      <div style={{ display:"flex", gap:8, marginBottom:10, flexWrap:"wrap", alignItems:"center" }}>
        {/* Import */}
        <input type="text" value={chesscomUsername} onChange={e => setChesscomUsername(e.target.value)}
          placeholder="Chess.com username" style={{ width:154 }} disabled={importingGame} />
        <button className="btn-primary" onClick={importFromChessDotCom} disabled={importingGame||!chesscomUsername.trim()} style={{ fontSize:12 }}>
          {importingGame ? "Importing…" : "Analyze last game"}
        </button>
        {importError && <span style={{ color:"var(--red)", fontSize:12 }}>{importError}</span>}

        <div style={{ width:1, height:24, background:"var(--border)", margin:"0 4px" }} />

        <select value={skill} onChange={e => setSkill(Number(e.target.value))} disabled={busy||gameOver}
          style={{ background:"var(--bg-card)", color:"var(--text-primary)", border:"1px solid var(--border-light)", borderRadius:"var(--radius)", padding:"5px 8px", fontSize:13 }}>
          <option value={1}>Skill 1 · &lt;400</option>
          <option value={2}>Skill 2 · 500</option>
          <option value={3}>Skill 3 · 800</option>
          <option value={4}>Skill 4 · 1100</option>
          <option value={5}>Skill 5 · 1500</option>
          <option value={6}>Skill 6 · 1900</option>
        </select>
        <button onClick={onHint} disabled={busy||gameOver}>{busy?"Thinking…":"Hint"}</button>
        <button onClick={onUndo} disabled={busy||gameOver||history.length<=1}>Undo</button>
        <button onClick={onRedo} disabled={busy||gameOver||redoStack.length===0}>Redo</button>
        <button onClick={onReset} disabled={busy}>Reset</button>

        <div style={{ marginLeft:"auto", position:"relative" }}>
          <button onClick={() => setSettingsOpen(o=>!o)} title="Settings"
            style={{ background:settingsOpen?"var(--bg-card-alt)":"none", padding:"5px 9px" }}>⚙</button>
          {settingsOpen && (
            <div style={{ position:"absolute", right:0, top:"calc(100% + 6px)", background:"var(--bg-card)", border:"1px solid var(--border)", borderRadius:"var(--radius-lg)", padding:"12px 14px", boxShadow:"var(--shadow-lg)", zIndex:100, minWidth:170, display:"flex", flexDirection:"column", gap:10 }}>
              <label style={{ display:"flex", alignItems:"center", gap:8, fontSize:13, cursor:"pointer", userSelect:"none" }}>
                <input type="checkbox" checked={coachAnalysis} onChange={e => setCoachAnalysis(e.target.checked)} />
                Coach analysis (per move)
              </label>
              <label style={{ display:"flex", alignItems:"center", gap:8, fontSize:13, cursor:hasSampleData?"pointer":"not-allowed", userSelect:"none", opacity:hasSampleData?1:.4 }}
                title={hasSampleData?"":"Play a game first"}>
                <input type="checkbox" checked={sampleMode} disabled={!hasSampleData}
                  onChange={e => { const on=e.target.checked; setSampleMode(on); if (on) loadSampleGame(); else onReset(); }} />
                Sample data
              </label>
            </div>
          )}
        </div>
      </div>

      <EvalBar white={cp.white} black={cp.black} />

      {/* Two-column: board | right panel */}
      <div style={{ display:"flex", gap:20, alignItems:"flex-start", marginTop:8 }}>

        {/* Board */}
        <div style={{ flexShrink:0 }}>
          <div style={{ display:"flex", justifyContent:"space-between", fontSize:12, color:"var(--text-secondary)", marginBottom:4 }}>
            <span>You: <strong>{score.user}</strong></span>
            <span>Computer: <strong>{score.coach}</strong></span>
          </div>
          <CapturedRow pieces={capturedPieces.byBlack} label="Coach captured" />
          <div style={{ position:"relative", width:BOARD_SIZE, height:BOARD_SIZE, marginTop:4 }}>
            <Chessboard
              position={game.fen()} onPieceDrop={onPieceDrop} onSquareClick={onSquareClickHandler}
              arePiecesDraggable={!gameOver} customSquareStyles={customSquareStyles}
              boardWidth={BOARD_SIZE} animationDuration={200}
            />
            {checkmatedKingSquare && <div style={overlayStyleForSquare(checkmatedKingSquare)}>Checkmate</div>}
          </div>
          <CapturedRow pieces={capturedPieces.byWhite} label="You captured" />
        </div>

        {/* Right panel */}
        <div style={{ flex:1, minWidth:0, display:"flex", flexDirection:"column", gap:12 }}>

          {/* Status message */}
          {msg && <div style={{ background:"var(--red-bg)", border:"1px solid var(--red-border)", borderRadius:"var(--radius)", padding:"8px 12px", color:"var(--red)", fontSize:13 }}>{msg}</div>}
          {coach && !gameOver && (
            <div style={{ background:"var(--blue-bg)", border:"1px solid var(--blue-border)", borderRadius:"var(--radius)", padding:"10px 13px", color:"var(--text-secondary)", fontSize:13, lineHeight:1.65, whiteSpace:"pre-line" }}>
              {coach}
            </div>
          )}

          {/* Ask coach */}
          {!gameOver && coachAnalysis && (
            <div style={{ display:"flex", gap:6 }}>
              <input type="text" placeholder="Ask the coach…" value={askText} onChange={e => setAskText(e.target.value)}
                disabled={asking||!lastSummary} style={{ flex:1 }} />
              <button disabled={asking||!askText.trim()||!lastSummary}
                onClick={async () => {
                  if (!lastSummary||!askText.trim()) return;
                  try { setAsking(true);
                    const res = await axios.post(`${API}/clarify`, { summary:lastSummary, question:askText.trim() }, { timeout:30000 });
                    if (res.data?.ok&&res.data?.answer) { setCoach(prev=>[prev,`Q: ${askText.trim()}`,`A: ${res.data.answer}`].filter(Boolean).join("\n")); setAskText(""); }
                  } catch {} finally { setAsking(false); }
                }}>{asking?"Asking…":"Ask"}</button>
            </div>
          )}

          {/* Move notation list */}
          {moveHistory.length > 0 && !gameOver && (
            <div className="card" style={{ padding:"10px 12px" }}>
              <div style={{ fontSize:11, fontWeight:700, letterSpacing:".06em", textTransform:"uppercase", color:"var(--text-muted)", marginBottom:8 }}>
                Moves
              </div>
              <div className="move-list scroll-panel" style={{ maxHeight:200 }}>
                {pairedMoves.map(pair => (
                  <div key={pair.num} className="move-row">
                    <span className="move-num">{pair.num}.</span>
                    <span className={`move-cell ${moveBadgeClass(pair.white?.label)}`}>{pair.white?.san || "…"}</span>
                    <span className={`move-cell ${moveBadgeClass(pair.black?.label)}`}>{pair.black?.san || ""}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Game over / analysis */}
          {gameOver && (
            <div className="card" style={{ padding:16 }}>
              {coach && (
                <div style={{ fontWeight:600, marginBottom:12, color:"var(--text-secondary)", fontSize:13 }}>{coach}</div>
              )}
              {analyzing && (
                <div style={{ display:"flex", alignItems:"center", gap:10, color:"var(--text-secondary)", fontSize:13 }}>
                  <span className="spinner" /> Analyzing your game…
                </div>
              )}
              {!analyzing && analysisResult && (
                <>
                  {!reviewMove ? (
                    <>
                      <h4 style={{ marginBottom:8, fontSize:14 }}>Game Analysis</h4>
                      <p style={{ margin:"0 0 10px", color:"var(--text-secondary)", fontSize:13, lineHeight:1.65 }}>{analysisResult.weakness_summary}</p>
                      {analysisResult.explanation && (
                        <p style={{ margin:"0 0 12px", fontSize:12, color:"var(--text-muted)", lineHeight:1.55 }}>{analysisResult.explanation}</p>
                      )}
                      <div style={{ display:"flex", gap:6, flexWrap:"wrap", marginBottom:14 }}>
                        {(analysisResult.themes||[]).map(t => (
                          <span key={t} style={{ background:"var(--blue-bg)", color:"var(--blue)", border:"1px solid var(--blue-border)", padding:"2px 10px", borderRadius:20, fontSize:12, fontWeight:600 }}>
                            {THEME_LABELS[t]||t}
                          </span>
                        ))}
                      </div>
                    </>
                  ) : (
                    <div style={{ marginBottom:12 }}>
                      <button className="btn-ghost" style={{ marginBottom:8, display:"block" }} onClick={exitReview}>← Back to analysis</button>
                      <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:10 }}>
                        <span style={{ fontWeight:700, fontSize:13 }}>Move {reviewMove.move_number}: <span style={{ fontFamily:"monospace" }}>{reviewMove.san}</span></span>
                        <MoveBadge label={reviewMove.label} />
                        <span style={{ fontSize:11, color:"var(--text-muted)" }}>({reviewMove.cp_delta>0?"+":""}{reviewMove.cp_delta} cp)</span>
                      </div>
                      <div style={{ display:"flex", gap:6, marginBottom:10 }}>
                        <button onClick={reviewUndo} disabled={reviewHistoryIndex<=0} style={{ padding:"4px 10px", fontSize:12 }}>← Prev</button>
                        <button onClick={reviewRedo} disabled={reviewHistoryIndex>=history.length-1} style={{ padding:"4px 10px", fontSize:12 }}>Next →</button>
                      </div>
                      {reviewExplaining && <div style={{ display:"flex", alignItems:"center", gap:8, color:"var(--text-muted)", fontSize:12 }}><span className="spinner" /> Analyzing…</div>}
                      {reviewExplanation && (
                        <div style={{ fontSize:13, color:"var(--text-secondary)", lineHeight:1.65 }}>
                          {typeof reviewExplanation==="string" ? reviewExplanation : reviewExplanation.summary}
                        </div>
                      )}
                    </div>
                  )}

                  {/* Key mistakes list */}
                  {(() => {
                    const PAGE_SIZE=5, mistakes=moveHistory.filter(m=>m.label!=="Good");
                    if (!mistakes.length) return null;
                    const totalPages=Math.ceil(mistakes.length/PAGE_SIZE), page=Math.min(mistakePage,totalPages-1);
                    const pageItems=mistakes.slice(page*PAGE_SIZE, page*PAGE_SIZE+PAGE_SIZE);
                    return (
                      <div style={{ marginBottom:12 }}>
                        <div style={{ fontSize:11, fontWeight:700, letterSpacing:".06em", textTransform:"uppercase", color:"var(--text-muted)", marginBottom:8 }}>
                          Key Mistakes
                        </div>
                        <div style={{ display:"flex", flexDirection:"column", gap:4 }}>
                          {pageItems.map((m,i) => (
                            <button key={i} onClick={() => handleReviewMove(m)}
                              className={`mistake-btn${reviewMove===m?" active":""}`}>
                              <span style={{ fontWeight:600, fontSize:13 }}>
                                Move {m.move_number}: <span style={{ fontFamily:"monospace" }}>{m.san}</span>
                              </span>
                              <div style={{ display:"flex", alignItems:"center", gap:6 }}>
                                <MoveBadge label={m.label} />
                                <span style={{ fontSize:11, color:"var(--text-muted)" }}>{m.cp_delta>0?"+":""}{m.cp_delta}cp</span>
                              </div>
                            </button>
                          ))}
                        </div>
                        {totalPages > 1 && (
                          <div style={{ display:"flex", gap:6, marginTop:6 }}>
                            <button onClick={()=>setMistakePage(p=>Math.max(0,p-1))} disabled={page===0} style={{ fontSize:12, padding:"3px 10px" }}>← Prev</button>
                            <button onClick={()=>setMistakePage(p=>Math.min(totalPages-1,p+1))} disabled={page===totalPages-1} style={{ fontSize:12, padding:"3px 10px" }}>Next →</button>
                          </div>
                        )}
                      </div>
                    );
                  })()}

                  {!reviewMove && (
                    <button className="btn-primary" style={{ width:"100%" }} onClick={() => startPuzzleSession(analysisResult.themes)} disabled={loadingPuzzles}>
                      {loadingPuzzles ? "Loading puzzles…" : "Practice These Puzzles"}
                    </button>
                  )}
                </>
              )}
              {!analyzing && !analysisResult && (
                <div>
                  <p style={{ color:"var(--red)", margin:"0 0 10px", fontSize:13 }}>
                    {analysisError ? `Analysis failed: ${analysisError}` : "Game analysis unavailable (requires Gemini API)."}
                  </p>
                  {analysisError && (
                    <button className="btn-primary" onClick={() => triggerGameAnalysis(moveHistory)}>Retry Analysis</button>
                  )}
                </div>
              )}
            </div>
          )}

          {/* Move list when game over */}
          {gameOver && moveHistory.length > 0 && (
            <div className="card" style={{ padding:"10px 12px" }}>
              <div style={{ fontSize:11, fontWeight:700, letterSpacing:".06em", textTransform:"uppercase", color:"var(--text-muted)", marginBottom:8 }}>
                Game Moves
              </div>
              <div className="move-list scroll-panel" style={{ maxHeight:180 }}>
                {pairedMoves.map(pair => (
                  <div key={pair.num} className="move-row">
                    <span className="move-num">{pair.num}.</span>
                    <span className={`move-cell ${moveBadgeClass(pair.white?.label)}`}>{pair.white?.san||"…"}</span>
                    <span className={`move-cell ${moveBadgeClass(pair.black?.label)}`}>{pair.black?.san||""}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

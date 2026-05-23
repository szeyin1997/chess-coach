import { useEffect, useMemo, useState, useCallback } from "react";
import axios from "axios";
import { Chess } from "chess.js";
import { Chessboard } from "react-chessboard";

const API = "http://127.0.0.1:8000";

// Bump when the shape of cached analysis data changes (new required fields, etc).
// Old cached entries from before the bump are treated as a cache miss and re-fetched.
// History: 1 = pre-classifier-unification (label only); 2 = adds severity + missed_opportunity;
//          3 = adds motif + tactical_summary per flagged move (cross-game LLM uses these).
const ANALYSIS_CACHE_VERSION = 3;

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
  if (l === "miss")             return "miss";
  return "good";
}

// ── Small reusable components ──────────────────────────────────────────────

function TabBar({ active, onChange }) {
  return (
    <div className="tabs">
      {["analyze","play"].map(tab => (
        <button key={tab} className={`tab-btn${active === tab ? " active" : ""}`} onClick={() => onChange(tab)}>
          {tab === "analyze" ? "Analyze My Games" : "Play vs Bot"}
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
      {explanation?.what_next?.length > 0 && !explaining && (
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

function ExplainPanel({ explanation, bestMove, explaining, posIndex, onAnalyze }) {
  if (explaining) {
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 10, color: "var(--text-secondary)", fontSize: 13 }}>
        <span className="spinner" /> Analyzing…
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
      {explanation?.what_next?.length > 0 && (
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

      {/* Expandable: strengths + Elo context */}
      <button
        className="btn-ghost"
        style={{ marginTop: 10, fontSize: 12, display: "flex", alignItems: "center", gap: 4 }}
        onClick={() => setExpanded(e => !e)}
      >
        {expanded ? "▲ Hide details" : "▼ Strengths & context"}
      </button>

      {expanded && (
        <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 10 }}>
          {summary.strengths?.length > 0 && (
            <div className="explain-better">
              <div className="explain-label" style={{ color: "var(--green)" }}>What you do well</div>
              <ul style={{ margin: "4px 0 0", paddingLeft: 16 }}>
                {summary.strengths.map((s, i) => <li key={i} style={{ marginBottom: 3 }}>{s}</li>)}
              </ul>
            </div>
          )}
          {summary.elo_context && (
            <div className="explain-tip">
              <div className="explain-label" style={{ color: "var(--blue)" }}>How you compare at your rating</div>
              {summary.elo_context}
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
    setReviewExplanation(null); setReviewBestMove(null); setReviewPositions([]); setReviewPosIndex(0);
    setReviewSelectedMove(move);
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

    setReviewPositions(positions); setReviewPosIndex(0); setReviewFen(fenBefore);

    // Fetch best-move hint (fast, always useful)
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
    // Gemini explanation is NOT triggered here — user clicks "Analyse with Coach"
  }

  async function fetchMistakeExplanation(move) {
    if (!move?.fen_before) return;
    let uci = null;
    try {
      const c = new Chess(move.fen_before);
      const match = c.moves({ verbose: true }).find(m => m.san.replace(/[+#]$/,"") === move.san.replace(/[+#]$/,""));
      if (match) uci = match.from + match.to + (match.promotion || "");
    } catch {}
    if (!uci) { setReviewExplanation({ summary: "Could not derive move notation." }); return; }
    setReviewExplaining(true);
    try {
      const sumRes = await axios.post(`${API}/summarize`, { fen: move.fen_before, uci, label: move.label }, { timeout: 60000 });
      if (!sumRes.data?.ok) {
        setReviewExplanation({ summary: `Analysis error: ${sumRes.data?.error || "unknown"}` });
      } else {
        const ai = sumRes.data?.ai_summary;
        setReviewExplanation(ai?.summary ? ai : { summary: "No explanation returned from coach." });
      }
    } catch (e) { setReviewExplanation({ summary: `Request failed: ${e?.message || "unknown error"}` }); }
    setReviewExplaining(false);
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
              if (aiText) { const v = ai?.verdict ? ` (${ai.verdict})` : ""; setCoach([evalText, `Coach${v}: ${aiText}`].filter(Boolean).join(" · ")); setLastSummary(ai); }
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
                        customSquareStyles={(() => {
                          const s = {};
                          if (!tryCurrentFen && reviewPosIndex === 0 && reviewBestMove) {
                            s[reviewBestMove.from] = { boxShadow:"inset 0 0 0 3px var(--green)" };
                            s[reviewBestMove.to]   = { boxShadow:"inset 0 0 0 4px var(--green)", background:"rgba(72,187,120,.2)" };
                          }
                          return s;
                        })()}
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
                          posIndex={reviewPosIndex}
                          onAnalyze={reviewSelectedMove && !reviewExplanation && !reviewExplaining
                            ? () => fetchMistakeExplanation(reviewSelectedMove)
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
                      No significant recurring weaknesses found. You're playing well!
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

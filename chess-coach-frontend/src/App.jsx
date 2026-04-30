import { useEffect, useMemo, useState, useCallback } from "react";
import axios from "axios";
import { Chess } from "chess.js";
import { Chessboard } from "react-chessboard";

const API = "http://127.0.0.1:8000"; // FastAPI

const THEME_LABELS = {
  mateIn1: "Checkmate in 1",
  mateIn2: "Checkmate in 2",
  mateIn3: "Checkmate in 3",
  mateIn4: "Checkmate in 4",
  mateIn5: "Checkmate in 5+",
  fork: "Forks",
  hangingPiece: "Hanging Pieces",
  pin: "Pins",
  skewer: "Skewers",
  discoveredAttack: "Discovered Attacks",
  crushing: "Winning Combinations",
  defensiveMove: "Defensive Resources",
};

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

  // Move history for post-game analysis
  const [moveHistory, setMoveHistory] = useState([]);

  // Post-game analysis state
  const [gameOver, setGameOver] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [analysisResult, setAnalysisResult] = useState(null); // { weakness_summary, themes, explanation }
  const [analysisError, setAnalysisError] = useState(null);

  // Coach analysis toggle — when off, skip per-move AI summaries
  const [coachAnalysis, setCoachAnalysis] = useState(false);

  // Settings panel
  const [settingsOpen, setSettingsOpen] = useState(false);

  // Sample game mode
  const [sampleMode, setSampleMode] = useState(false);
  const hasSampleData = !!localStorage.getItem("chess_coach_sample_game");

  // Mistake review state
  const [mistakePage, setMistakePage] = useState(0);
  const [reviewMove, setReviewMove] = useState(null); // the moveHistory entry being reviewed
  const [reviewHistoryIndex, setReviewHistoryIndex] = useState(null); // current position in history[] during review
  const [bestMoveHighlight, setBestMoveHighlight] = useState(null); // [fromSq, toSq]
  const [reviewExplanation, setReviewExplanation] = useState(null); // LLM explanation text
  const [reviewExplaining, setReviewExplaining] = useState(false);

  // Puzzle mode state
  const [mode, setMode] = useState("game"); // "game" | "puzzle"
  const [puzzles, setPuzzles] = useState([]);
  const [puzzleIndex, setPuzzleIndex] = useState(0);
  const [puzzleGame, setPuzzleGame] = useState(null); // Chess instance for current puzzle
  const [puzzleMoveIndex, setPuzzleMoveIndex] = useState(0); // index into solution moves (user moves only, skip first)
  const [puzzleSolved, setPuzzleSolved] = useState(false);
  const [puzzleFailed, setPuzzleFailed] = useState(false);
  const [puzzleScore, setPuzzleScore] = useState(0);
  const [loadingPuzzles, setLoadingPuzzles] = useState(false);
  const [puzzleMsg, setPuzzleMsg] = useState("");
  const [puzzleComplete, setPuzzleComplete] = useState(false); // all 5 done
  const [puzzleHistory, setPuzzleHistory] = useState([]); // FEN snapshots for undo/redo review
  const [puzzleHistoryIndex, setPuzzleHistoryIndex] = useState(0);
  const [puzzleOrientation, setPuzzleOrientation] = useState("white");

  const PIECE_SYMBOLS = {
    w: { p: "♙", n: "♘", b: "♗", r: "♖", q: "♕" },
    b: { p: "♟", n: "♞", b: "♝", r: "♜", q: "♛" },
  };

  const capturedPieces = useMemo(() => {
    const countPieces = (fen) => {
      const c = new Chess();
      c.load(fen);
      const board = c.board();
      const counts = { w: { p: 0, n: 0, b: 0, r: 0, q: 0 }, b: { p: 0, n: 0, b: 0, r: 0, q: 0 } };
      for (const row of board) {
        for (const sq of row) {
          if (!sq || sq.type === "k") continue;
          counts[sq.color][sq.type]++;
        }
      }
      return counts;
    };

    const byWhite = [];
    const byBlack = [];

    for (let i = 1; i < history.length; i++) {
      const prev = countPieces(history[i - 1]);
      const curr = countPieces(history[i]);
      for (const color of ["w", "b"]) {
        for (const t of ["p", "n", "b", "r", "q"]) {
          const diff = curr[color][t] - prev[color][t];
          if (diff < 0) {
            for (let d = 0; d < -diff; d++) {
              if (color === "b") byWhite.push({ color, type: t });
              else byBlack.push({ color, type: t });
            }
          }
        }
      }
    }
    return { byWhite, byBlack };
  }, [history]);

  const score = useMemo(() => {
    const pieceValues = { p: 1, n: 3, b: 3, r: 5, q: 9 };
    return {
      user: capturedPieces.byWhite.reduce((s, p) => s + (pieceValues[p.type] || 0), 0),
      coach: capturedPieces.byBlack.reduce((s, p) => s + (pieceValues[p.type] || 0), 0),
    };
  }, [capturedPieces]);

  const legalTargets = useMemo(() => {
    if (!selectedSquare) return [];
    try {
      const c = new Chess(game.fen());
      const moves = c.moves({ square: selectedSquare, verbose: true }) || [];
      return moves.map((m) => m.to);
    } catch {
      return [];
    }
  }, [selectedSquare, game.fen()]);

  useEffect(() => {
    console.log("App mounted; initial FEN:", game.fen());
  }, []);

  function CapturedRow({ pieces, label }) {
    if (!pieces.length) return (
      <div style={{ minHeight: 24, display: "flex", alignItems: "center", fontSize: 13, color: "#999" }}>
        {label}: —
      </div>
    );
    const order = { q: 0, r: 1, b: 2, n: 3, p: 4 };
    const sorted = [...pieces].sort((a, b) => (order[a.type] ?? 9) - (order[b.type] ?? 9));
    return (
      <div style={{ minHeight: 24, display: "flex", alignItems: "center", gap: 2, fontSize: 22, lineHeight: 1 }}>
        {sorted.map((p, i) => (
          <span key={i} title={`${p.color === "w" ? "White" : "Black"} ${p.type}`}>
            {PIECE_SYMBOLS[p.color][p.type]}
          </span>
        ))}
      </div>
    );
  }

  function EvalBar({ white, black, cap = 800 }) {
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, Number.isFinite(v) ? v : 0));
    const fmt = (v) => (v == null || isNaN(v) ? "—" : `${v > 0 ? "+" : ""}${Math.round(v)}`);
    const w = clamp(white ?? 0, -cap, cap);
    const p = (w + cap) / (2 * cap);
    const leftPct = `${((1 - p) * 100).toFixed(1)}%`;
    const rightPct = `${(p * 100).toFixed(1)}%`;
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 8, margin: "6px 0" }}>
        <div style={{ position: "relative", width: 260, height: 14, background: "#bbb", borderRadius: 7, overflow: "hidden" }}>
          <div title={`Black ${fmt(black)} cp`} style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: leftPct, background: "#1f2937" }} />
          <div title={`White ${fmt(white)} cp`} style={{ position: "absolute", right: 0, top: 0, bottom: 0, width: rightPct, background: "#f1f5f9" }} />
        </div>
        <div style={{ fontSize: 12, color: "#444", display: "flex", gap: 8 }}>
          <span>W: {fmt(white)} cp</span>
          <span>B: {fmt(black)} cp</span>
        </div>
      </div>
    );
  }

  async function onHint() {
    try {
      setBusy(true);
      const res = await axios.post(`${API}/hint`, { fen: game.fen() });
      setCoach(res.data?.idea ? `Hint: ${res.data.idea}` : "No hint.");
    } catch {
      setMsg("Failed to get hint.");
    } finally {
      setBusy(false);
    }
  }

  async function handleReviewMove(move) {
    const mistakeIdx = moveHistory.indexOf(move) * 2; // index in history[] for fen_before
    const g = new Chess();
    g.load(move.fen_before);
    setGame(g);
    setLastUserMove(null);
    setLastCoachMove(null);
    setBestMoveHighlight(null);
    setReviewMove(move);
    setReviewHistoryIndex(mistakeIdx);
    setReviewExplanation(null);

    // Fetch best move highlight
    try {
      const res = await axios.post(`${API}/hint`, { fen: move.fen_before });
      if (res.data?.best_uci) {
        setBestMoveHighlight([res.data.best_uci.slice(0, 2), res.data.best_uci.slice(2, 4)]);
      }
    } catch {}

    // Fetch LLM explanation
    if (move.uci && move.label && move.label !== "Good") {
      setReviewExplaining(true);
      try {
        const sres = await axios.post(`${API}/summarize`, {
          fen: move.fen_before,
          uci: move.uci,
          label: move.label,
        }, { timeout: 60000 });
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
    const g = new Chess();
    g.load(history[newIdx]);
    setGame(g);
    setReviewHistoryIndex(newIdx);
    setLastUserMove(null);
    setLastCoachMove(null);
  }

  function reviewRedo() {
    if (reviewHistoryIndex === null || reviewHistoryIndex >= history.length - 1) return;
    const newIdx = reviewHistoryIndex + 1;
    const g = new Chess();
    g.load(history[newIdx]);
    setGame(g);
    setReviewHistoryIndex(newIdx);
    setLastUserMove(null);
    setLastCoachMove(null);
  }

  function exitReview() {
    const finalFen = history[history.length - 1];
    const g = new Chess();
    g.load(finalFen);
    setGame(g);
    setReviewMove(null);
    setReviewHistoryIndex(null);
    setBestMoveHighlight(null);
    setReviewExplanation(null);
    setLastUserMove(null);
    setLastCoachMove(null);
    setCoach("");
  }

  // Trigger post-game analysis after game ends
  async function triggerGameAnalysis(moves) {
    if (!moves.length) return;
    setAnalyzing(true);
    setAnalysisError(null);
    try {
      const res = await axios.post(`${API}/analyze-game`, { moves }, { timeout: 60000 });
      if (res.data?.ok) {
        setAnalysisResult({
          weakness_summary: res.data.weakness_summary,
          themes: res.data.themes || [],
          explanation: res.data.explanation,
        });
      } else {
        setAnalysisError(res.data?.error || "Analysis returned an error.");
      }
    } catch (e) {
      console.warn("Game analysis failed:", e);
      setAnalysisError(e?.message || "Request failed.");
    } finally {
      setAnalyzing(false);
    }
  }

  async function onPieceDrop(from, to) {
    if (gameOver) return false;
    const fenBefore = game.fen();
    const temp = new Chess(game.fen());
    const fromPiece = (() => { try { return temp.get(from); } catch { return null; } })();
    const willPromote = !!(
      fromPiece &&
      fromPiece.type === "p" && (
        (fromPiece.color === "w" && to[1] === "8") ||
        (fromPiece.color === "b" && to[1] === "1")
      )
    );
    const promotion = willPromote ? "q" : undefined;
    const moved = temp.move({ from, to, ...(promotion ? { promotion } : {}) });
    if (!moved) {
      setMsg("Illegal move.");
      return false;
    }

    try {
      setBusy(true);
      setMsg("");
      setCoach("");
      setRedoStack([]);

      const res = await axios.post(`${API}/play`, {
        fen: game.fen(),
        uci: from + to,
        ...(promotion ? { promotion } : {}),
        skill: Number(skill),
        summary: false,
      });

      if (!res.data?.ok) {
        setMsg(res.data?.error || "Move rejected.");
        return false;
      }

      // Append to move history for post-game analysis
      const userMove = res.data.user;
      const newMoveHistory = [...moveHistory, {
        san: userMove?.san,
        uci: userMove?.uci,
        label: userMove?.label,
        cp_delta: userMove?.delta_cp,
        fen_before: fenBefore,
        move_number: moveHistory.length + 1,
      }];
      setMoveHistory(newMoveHistory);

      if (res.data.fen_after_user) {
        const g1 = new Chess();
        g1.load(res.data.fen_after_user);
        setGame(g1);
        setHistory((h) => [...h, g1.fen()]);
        setRedoStack([]);
        setLastUserMove([from, to]);
        if (res.data.cp_after_user) {
          setCp({ white: res.data.cp_after_user.white, black: res.data.cp_after_user.black });
        }
      }

      await new Promise((r) => setTimeout(r, 800));

      if (res.data.fen_after_coach) {
        const g2 = new Chess();
        g2.load(res.data.fen_after_coach);
        setGame(g2);
        setHistory((h) => [...h, g2.fen()]);
        setRedoStack([]);
        const coachUci = res.data.coach?.uci;
        if (coachUci && coachUci.length >= 4) {
          setLastCoachMove([coachUci.slice(0, 2), coachUci.slice(2, 4)]);
        } else {
          setLastCoachMove(null);
        }
      }

      if (res.data.cp_after_coach) {
        setCp({ white: res.data.cp_after_coach.white, black: res.data.cp_after_coach.black });
      }

      const latestFen = res.data.fen_after_coach || res.data.fen_after_user;
      if (latestFen) {
        const end = new Chess();
        end.load(latestFen);
        if (end.isCheckmate() || end.isStalemate() || end.isDraw() || res.data.game_over) {
          const endMsg = end.isCheckmate() ? "Checkmate" : end.isStalemate() ? "Stalemate" : "Draw";
          setCoach(`Game over — ${endMsg}. Analyzing your game...`);
          setGameOver(true);
          // Build the final history snapshot (setHistory is async so we build it manually)
          const historySnapshot = [
            ...history,
            ...(res.data.fen_after_user ? [res.data.fen_after_user] : []),
            ...(res.data.fen_after_coach ? [res.data.fen_after_coach] : []),
          ];
          localStorage.setItem("chess_coach_sample_game", JSON.stringify({
            moveHistory: newMoveHistory,
            history: historySnapshot,
            finalFen: latestFen,
          }));
          triggerGameAnalysis(newMoveHistory);
          return true;
        }
      }

      if (coachAnalysis) {
        const label = res.data.user?.label;
        const delta = res.data.user?.delta_cp;
        const idea = res.data.user?.idea;
        const evalText = label != null && delta != null ? `Your move: ${label} (${delta > 0 ? "+" : ""}${delta} cp)` : "";

        if (label !== "Good") {
          setCoach([evalText, "Coach: Summarizing…"].filter(Boolean).join(" • "));
          (async () => {
            try {
              const sres = await axios.post(
                `${API}/summarize`,
                { fen: fenBefore, uci: from + to, ...(promotion ? { promotion } : {}), label },
                { timeout: 60000 }
              );
              const ai = sres.data?.ai_summary;
              const aiText = ai?.summary || ai?.summary_text;
              if (aiText) {
                const v = ai?.verdict ? ` (${ai.verdict})` : "";
                setCoach([evalText, `Coach${v}: ${aiText}`].filter(Boolean).join(" • "));
                setLastSummary(ai);
              } else if (sres.data?.error) {
                setCoach([evalText, idea ? `Idea: ${idea}` : ""].filter(Boolean).join(" • "));
                setLastSummary(null);
              }
            } catch {
              setCoach([evalText, idea ? `Idea: ${idea}` : ""].filter(Boolean).join(" • "));
              setLastSummary(null);
            }
          })();
        } else {
          setCoach([evalText, idea ? `Idea: ${idea}` : ""].filter(Boolean).join(" • "));
          setLastSummary(null);
        }
      }

      return true;
    } catch (err) {
      console.error("/play request failed", err);
      const status = err?.response?.status;
      const detail = err?.response?.data || err?.message || "Unknown error";
      setMsg(`Backend error${status ? ` (${status})` : ""}. ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
      return false;
    } finally {
      setBusy(false);
    }
  }

  function loadSampleGame() {
    const raw = localStorage.getItem("chess_coach_sample_game");
    if (!raw) return;
    const { moveHistory: savedMoves, history: savedHistory, finalFen } = JSON.parse(raw);
    const g = new Chess();
    g.load(finalFen);
    setGame(g);
    setHistory(savedHistory);
    setMoveHistory(savedMoves);
    setRedoStack([]);
    setLastUserMove(null);
    setLastCoachMove(null);
    setSelectedSquare(null);
    setGameOver(true);
    setAnalyzing(false);
    setAnalysisResult(null);
    setAnalysisError(null);
    setMistakePage(0);
    setReviewMove(null);
    setReviewHistoryIndex(null);
    setBestMoveHighlight(null);
    setReviewExplanation(null);
    setCoach("Sample game loaded. Analyzing...");
    triggerGameAnalysis(savedMoves);
  }

  function onReset() {
    const g = new Chess();
    setGame(g);
    setMsg("");
    setCoach("");
    setHistory([g.fen()]);
    setRedoStack([]);
    setLastUserMove(null);
    setLastCoachMove(null);
    setSelectedSquare(null);
    setMoveHistory([]);
    setGameOver(false);
    setAnalyzing(false);
    setAnalysisResult(null);
    setAnalysisError(null);
    setMistakePage(0);
    setReviewMove(null);
    setReviewHistoryIndex(null);
    setBestMoveHighlight(null);
    setReviewExplanation(null);
    setSampleMode(false);
    setMode("game");
    // Tell backend to clear its game history too
    axios.post(`${API}/new-game`).catch(() => {});
  }

  function onUndo() {
    if (busy || gameOver) return;
    setHistory((h) => {
      if (h.length <= 1) return h;
      const current = h[h.length - 1];
      const prev = h[h.length - 2];
      setRedoStack((r) => [...r, current]);
      const g = new Chess();
      g.load(prev);
      setGame(g);
      return h.slice(0, -1);
    });
    setMoveHistory((m) => m.slice(0, -1));
    setMsg("");
    setCoach("");
    setLastUserMove(null);
    setLastCoachMove(null);
    setSelectedSquare(null);
  }

  function onRedo() {
    if (busy || gameOver) return;
    setRedoStack((r) => {
      if (!r.length) return r;
      const next = r[r.length - 1];
      setHistory((h) => [...h, next]);
      const g = new Chess();
      g.load(next);
      setGame(g);
      return r.slice(0, -1);
    });
    setMsg("");
    setCoach("");
    setLastUserMove(null);
    setLastCoachMove(null);
    setSelectedSquare(null);
  }

  // ─── Puzzle mode ────────────────────────────────────────────────────────────

  async function startPuzzleSession(themes) {
    setLoadingPuzzles(true);
    try {
      const res = await axios.post(`${API}/puzzles`, { themes, count: 5 }, { timeout: 15000 });
      if (res.data?.ok && res.data.puzzles?.length) {
        setPuzzles(res.data.puzzles);
        setPuzzleIndex(0);
        setPuzzleScore(0);
        setPuzzleComplete(false);
        loadPuzzle(res.data.puzzles[0]);
        setMode("puzzle");
      } else {
        setMsg("No puzzles found for these themes. Try again later.");
      }
    } catch (e) {
      setMsg("Failed to load puzzles.");
    } finally {
      setLoadingPuzzles(false);
    }
  }

  function loadPuzzle(puzzle) {
    // The Lichess FEN is the position BEFORE the opponent's first move.
    // moves is a space-separated UCI list: first move is opponent's, rest are the solution.
    const moves = puzzle.moves.split(" ");
    const g = new Chess(puzzle.fen);
    // Play the opponent's first move automatically
    if (moves.length > 0) {
      try {
        g.move({ from: moves[0].slice(0, 2), to: moves[0].slice(2, 4), promotion: moves[0][4] || undefined });
      } catch {}
    }
    setPuzzleGame(g);
    setPuzzleMoveIndex(1); // next expected move index in the moves array
    setPuzzleSolved(false);
    setPuzzleFailed(false);
    setPuzzleMsg("");
    setPuzzleHistory([g.fen()]);
    setPuzzleHistoryIndex(0);
    setPuzzleOrientation(g.turn() === "b" ? "black" : "white");
  }

  function onPuzzleDrop(from, to) {
    if (!puzzleGame || puzzleSolved) return false;
    const puzzle = puzzles[puzzleIndex];
    const solutionMoves = puzzle.moves.split(" ");
    const expectedUci = solutionMoves[puzzleMoveIndex];

    if (!expectedUci) return false;

    const fromPiece = (() => { try { return puzzleGame.get(from); } catch { return null; } })();
    const willPromote = !!(
      fromPiece && fromPiece.type === "p" &&
      ((fromPiece.color === "w" && to[1] === "8") || (fromPiece.color === "b" && to[1] === "1"))
    );
    const promotion = willPromote ? (expectedUci[4] || "q") : undefined;
    const playedUci = from + to + (promotion || "");

    // Clear previous failed state on every new attempt
    if (puzzleFailed) { setPuzzleFailed(false); setPuzzleMsg(""); }

    // Check if played move matches expected
    const expectedBase = expectedUci.slice(0, 4);
    if (from + to !== expectedBase) {
      setPuzzleMsg("Incorrect — try another move.");
      setPuzzleFailed(true);
      return false;
    }

    // Apply user's move
    const newG = new Chess(puzzleGame.fen());
    try {
      newG.move({ from, to, promotion });
    } catch {
      setPuzzleMsg("Illegal move.");
      return false;
    }

    setPuzzleFailed(false);
    const nextMoveIndex = puzzleMoveIndex + 1;

    // Helper: append a FEN to the puzzle history and advance the index
    const appendHistory = (fen) => {
      setPuzzleHistory((h) => [...h, fen]);
      setPuzzleHistoryIndex((i) => i + 1);
    };

    // Check if puzzle is complete (no more moves)
    if (nextMoveIndex >= solutionMoves.length) {
      setPuzzleGame(newG);
      setPuzzleMoveIndex(nextMoveIndex);
      setPuzzleSolved(true);
      setPuzzleScore((s) => s + 1);
      setPuzzleMsg("Correct!");
      appendHistory(newG.fen());
      return true;
    }

    // Play the opponent's reply automatically
    const opponentUci = solutionMoves[nextMoveIndex];
    try {
      newG.move({
        from: opponentUci.slice(0, 2),
        to: opponentUci.slice(2, 4),
        promotion: opponentUci[4] || undefined,
      });
    } catch {}

    setPuzzleGame(newG);
    setPuzzleMoveIndex(nextMoveIndex + 1);
    appendHistory(newG.fen());

    // Check if solved after opponent reply (no more moves left)
    if (nextMoveIndex + 1 >= solutionMoves.length) {
      setPuzzleSolved(true);
      setPuzzleScore((s) => s + 1);
      setPuzzleMsg("Correct!");
    }

    return true;
  }

  function onNextPuzzle() {
    const nextIndex = puzzleIndex + 1;
    if (nextIndex >= puzzles.length) {
      setPuzzleComplete(true);
      return;
    }
    setPuzzleIndex(nextIndex);
    loadPuzzle(puzzles[nextIndex]);
  }

  // ─── Square highlights ───────────────────────────────────────────────────────

  const customSquareStyles = useMemo(() => {
    if (mode === "puzzle") return {};
    const styles = {};
    const mark = (sq, color) => { styles[sq] = { boxShadow: `inset 0 0 0 3px ${color}` }; };
    const shade = (sq, color) => { styles[sq] = { ...(styles[sq] || {}), background: color }; };
    if (selectedSquare) mark(selectedSquare, "#68d391");
    if (legalTargets?.length) for (const t of legalTargets) shade(t, "rgba(255, 105, 180, 0.35)");
    const atMistakePos = reviewMove && reviewHistoryIndex === moveHistory.indexOf(reviewMove) * 2;
    if (bestMoveHighlight && atMistakePos) { mark(bestMoveHighlight[0], "#68d391"); mark(bestMoveHighlight[1], "#38a169"); }
    return styles;
  }, [selectedSquare, legalTargets, bestMoveHighlight, reviewMove, reviewHistoryIndex, mode]);

  const checkmatedKingSquare = useMemo(() => {
    if (mode === "puzzle") return null;
    try {
      const c = new Chess(game.fen());
      if (!c.isCheckmate()) return null;
      const loser = c.turn();
      const files = "abcdefgh";
      const board = c.board();
      for (let r = 0; r < 8; r++) {
        for (let f = 0; f < 8; f++) {
          const sq = board[r][f];
          if (sq && sq.type === "k" && sq.color === loser) return `${files[f]}${8 - r}`;
        }
      }
    } catch { return null; }
    return null;
  }, [game.fen(), mode]);

  const BOARD_SIZE = 480;
  const squarePixel = BOARD_SIZE / 8;

  const overlayStyleForSquare = (square) => {
    if (!square) return {};
    const files = "abcdefgh";
    const file = square[0];
    const rank = Number(square[1]);
    const col = files.indexOf(file);
    const row = 8 - rank;
    return {
      position: "absolute", left: col * squarePixel, top: row * squarePixel,
      width: squarePixel, height: squarePixel,
      background: "rgba(220, 38, 38, 0.6)",
      display: "flex", alignItems: "center", justifyContent: "center",
      color: "#fff", fontWeight: 800, textTransform: "uppercase",
      textShadow: "0 1px 2px rgba(0,0,0,0.6)", zIndex: 5, pointerEvents: "none",
    };
  };

  async function onSquareClickHandler(square) {
    if (busy || gameOver) return;
    const temp = new Chess(game.fen());
    const piece = temp.get(square);
    if (!selectedSquare) {
      if (piece && piece.color === temp.turn()) setSelectedSquare(square);
      return;
    }
    if (piece && piece.color === temp.turn()) {
      setSelectedSquare(square);
      return;
    }
    const ok = await onPieceDrop(selectedSquare, square);
    if (ok) setSelectedSquare(null);
  }

  // ─── Render ──────────────────────────────────────────────────────────────────

  if (mode === "puzzle") {
    const puzzle = puzzles[puzzleIndex];
    const sideToMove = puzzleGame ? (puzzleGame.turn() === "w" ? "White" : "Black") : "?";

    if (puzzleComplete) {
      return (
        <div style={{ maxWidth: 520, margin: "24px auto", fontFamily: "system-ui, sans-serif" }}>
          <h2>Puzzle Session Complete!</h2>
          <div style={{ fontSize: 48, margin: "20px 0", textAlign: "center" }}>
            {puzzleScore >= 4 ? "🏆" : puzzleScore >= 2 ? "👍" : "💪"}
          </div>
          <p style={{ fontSize: 20, textAlign: "center" }}>
            You solved <strong>{puzzleScore} / {puzzles.length}</strong> puzzles
          </p>
          {analysisResult && (
            <p style={{ color: "#555", fontStyle: "italic", textAlign: "center" }}>
              Focus area: <strong>{(analysisResult.themes || []).map(t => THEME_LABELS[t] || t).join(", ")}</strong>
            </p>
          )}
          <div style={{ display: "flex", gap: 12, justifyContent: "center", marginTop: 20 }}>
            <button onClick={() => startPuzzleSession(analysisResult?.themes || ["crushing"])}>
              Practice Again
            </button>
            <button onClick={onReset} style={{ background: "#f0f0f0" }}>
              New Game
            </button>
          </div>
        </div>
      );
    }

    return (
      <div style={{ maxWidth: 520, margin: "24px auto", fontFamily: "system-ui, sans-serif" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          <h2 style={{ margin: 0 }}>Puzzle Training</h2>
          <span style={{ fontSize: 14, color: "#666" }}>
            {puzzleIndex + 1} / {puzzles.length}
          </span>
        </div>

        {/* Progress bar */}
        <div style={{ width: "100%", height: 6, background: "#e2e8f0", borderRadius: 3, marginBottom: 12 }}>
          <div style={{
            height: 6, borderRadius: 3, background: "#4299e1",
            width: `${((puzzleIndex) / puzzles.length) * 100}%`,
            transition: "width 0.3s",
          }} />
        </div>

        {analysisResult && (
          <div style={{ background: "#ebf8ff", border: "1px solid #bee3f8", borderRadius: 6, padding: "8px 12px", marginBottom: 12, fontSize: 13 }}>
            Focus: <strong>{(analysisResult.themes || []).map(t => THEME_LABELS[t] || t).join(", ")}</strong>
            {puzzle && <span style={{ color: "#718096" }}> — Puzzle rating: {puzzle.rating}</span>}
          </div>
        )}

        <p style={{ margin: "0 0 8px", fontWeight: 600, color: "#2d3748" }}>
          {sideToMove} to move — find the best sequence.
        </p>

        <div style={{ position: "relative", width: BOARD_SIZE, height: BOARD_SIZE }}>
          <Chessboard
            position={puzzleHistory.length > 0 ? puzzleHistory[puzzleHistoryIndex] : (puzzleGame ? puzzleGame.fen() : "start")}
            onPieceDrop={puzzleHistoryIndex === puzzleHistory.length - 1 ? onPuzzleDrop : () => false}
            arePiecesDraggable={!puzzleSolved && puzzleHistoryIndex === puzzleHistory.length - 1}
            boardWidth={BOARD_SIZE}
            animationDuration={200}
            boardOrientation={puzzleOrientation}
          />
        </div>

        <div style={{ marginTop: 10, minHeight: 32 }}>
          {puzzleMsg && (
            <p style={{
              color: puzzleSolved ? "#276749" : "#c53030",
              fontWeight: 600,
              margin: 0,
            }}>
              {puzzleSolved ? "✓ " : "✗ "}{puzzleMsg}
            </p>
          )}
        </div>

        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          {puzzleSolved && (
            <button
              onClick={onNextPuzzle}
              style={{ background: "#276749", color: "#fff", border: "none", padding: "8px 16px", borderRadius: 4, cursor: "pointer", fontWeight: 600 }}
            >
              {puzzleIndex + 1 >= puzzles.length ? "See Results" : "Next Puzzle"}
            </button>
          )}
          {puzzleFailed && !puzzleSolved && (
            <button onClick={() => { loadPuzzle(puzzle); }} style={{ background: "#f0f0f0", fontSize: 12 }}>
              Restart puzzle
            </button>
          )}
          <div style={{ display: "flex", gap: 4, marginLeft: puzzleFailed ? 8 : "auto", alignItems: "center" }}>
            <button
              onClick={() => setPuzzleHistoryIndex((i) => Math.max(0, i - 1))}
              disabled={puzzleHistoryIndex === 0}
              style={{ padding: "4px 10px", fontSize: 13 }}
            >
              ←
            </button>
            <button
              onClick={() => setPuzzleHistoryIndex((i) => Math.min(puzzleHistory.length - 1, i + 1))}
              disabled={puzzleHistoryIndex === puzzleHistory.length - 1}
              style={{ padding: "4px 10px", fontSize: 13 }}
            >
              →
            </button>
          </div>
          <button onClick={onReset} style={{ marginLeft: "auto" }}>
            New Game
          </button>
        </div>
      </div>
    );
  }

  // ─── Normal game mode ─────────────────────────────────────────────────────

  return (
    <div style={{ maxWidth: 820, margin: "24px auto", fontFamily: "system-ui, sans-serif", padding: "0 12px" }}>
      <h2 style={{ marginBottom: 8 }}>AI Chess Coach</h2>

      {/* Controls row */}
      <div style={{ display: "flex", gap: 8, marginBottom: 8, flexWrap: "wrap", alignItems: "center" }}>
        <select value={skill} onChange={(e) => setSkill(Number(e.target.value))} disabled={busy || gameOver}>
          <option value={1}>1 - &lt;400</option>
          <option value={2}>2 - 500</option>
          <option value={3}>3 - 800</option>
          <option value={4}>4 - 1100</option>
          <option value={5}>5 - 1500</option>
          <option value={6}>6 - 1900</option>
        </select>
        <button onClick={onHint} disabled={busy || gameOver} style={{ whiteSpace: "nowrap", minWidth: "10ch" }}>
          {busy ? "Thinking…" : "Get\u00A0Hint"}
        </button>
        <button onClick={onUndo} disabled={busy || gameOver || history.length <= 1}>Undo</button>
        <button onClick={onRedo} disabled={busy || gameOver || redoStack.length === 0}>Redo</button>
        <button onClick={onReset} disabled={busy}>Reset</button>
        <div style={{ marginLeft: "auto", position: "relative" }}>
          <button
            onClick={() => setSettingsOpen((o) => !o)}
            title="Settings"
            style={{
              background: settingsOpen ? "#e2e8f0" : "none",
              border: "1px solid #cbd5e0",
              borderRadius: 6,
              padding: "4px 8px",
              cursor: "pointer",
              fontSize: 16,
              lineHeight: 1,
            }}
          >
            ⚙
          </button>
          {settingsOpen && (
            <div style={{
              position: "absolute", right: 0, top: "calc(100% + 4px)",
              background: "#fff", border: "1px solid #e2e8f0", borderRadius: 8,
              padding: "10px 14px", boxShadow: "0 4px 12px rgba(0,0,0,0.1)",
              zIndex: 100, minWidth: 160, display: "flex", flexDirection: "column", gap: 10,
            }}>
              <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, cursor: "pointer", userSelect: "none", whiteSpace: "nowrap" }}>
                <input
                  type="checkbox"
                  checked={coachAnalysis}
                  onChange={(e) => setCoachAnalysis(e.target.checked)}
                />
                Coach analysis
              </label>
              <label style={{
                display: "flex", alignItems: "center", gap: 8, fontSize: 13,
                cursor: hasSampleData ? "pointer" : "not-allowed",
                userSelect: "none", opacity: hasSampleData ? 1 : 0.45,
                whiteSpace: "nowrap",
              }}
                title={hasSampleData ? "" : "Play a game first to save sample data"}
              >
                <input
                  type="checkbox"
                  checked={sampleMode}
                  disabled={!hasSampleData}
                  onChange={(e) => {
                    const on = e.target.checked;
                    setSampleMode(on);
                    if (on) loadSampleGame();
                    else onReset();
                  }}
                />
                Sample data
              </label>
            </div>
          )}
        </div>
      </div>

      <EvalBar white={cp.white} black={cp.black} cap={800} />

      {/* Two-column layout: board left, coach panel right */}
      <div style={{ display: "flex", gap: 20, alignItems: "flex-start", marginTop: 4 }}>

        {/* Left: board */}
        <div style={{ flexShrink: 0 }}>
          <div style={{ display: "flex", gap: 16, fontSize: 13, color: "#555", marginBottom: 4 }}>
            <span>You: {score.user}</span>
            <span>Computer: {score.coach}</span>
          </div>
          <CapturedRow pieces={capturedPieces.byBlack} label="Coach captured" />
          <div style={{ position: "relative", width: BOARD_SIZE, height: BOARD_SIZE }}>
            <Chessboard
              position={game.fen()}
              onPieceDrop={onPieceDrop}
              onSquareClick={onSquareClickHandler}
              arePiecesDraggable={!gameOver}
              customSquareStyles={customSquareStyles}
              boardWidth={BOARD_SIZE}
              animationDuration={200}
            />
            {checkmatedKingSquare && (
              <div style={overlayStyleForSquare(checkmatedKingSquare)}>Checkmate</div>
            )}
          </div>
          <CapturedRow pieces={capturedPieces.byWhite} label="You captured" />
        </div>

        {/* Right: coach panel */}
        <div style={{ flex: 1, minWidth: 0, paddingTop: 24 }}>
          {msg && <p style={{ color: "#c00", margin: "0 0 8px", fontSize: 14 }}>{msg}</p>}

          {coach && (
            <p style={{ color: "#055", margin: "0 0 12px", fontSize: 14, whiteSpace: "pre-line", lineHeight: 1.6 }}>
              {coach}
            </p>
          )}

          {/* Ask coach a question (only when coach analysis is on) */}
          {!gameOver && coachAnalysis && (
            <div style={{ display: "flex", gap: 6, marginBottom: 16 }}>
              <input
                type="text"
                placeholder="Ask: what do you mean by…"
                value={askText}
                onChange={(e) => setAskText(e.target.value)}
                disabled={asking || !lastSummary}
                style={{ flex: 1, fontSize: 13 }}
              />
              <button
                disabled={asking || !askText.trim() || !lastSummary}
                onClick={async () => {
                  if (!lastSummary || !askText.trim()) return;
                  try {
                    setAsking(true);
                    const res = await axios.post(`${API}/clarify`, {
                      summary: lastSummary,
                      question: askText.trim(),
                    }, { timeout: 30000 });
                    if (res.data?.ok && res.data?.answer) {
                      setCoach((prev) => [prev, `Q: ${askText.trim()}`, `A: ${res.data.answer}`].filter(Boolean).join("\n"));
                      setAskText("");
                    }
                  } catch {}
                  finally { setAsking(false); }
                }}
              >{asking ? "Asking…" : "Ask"}</button>
            </div>
          )}

          {/* Post-game weakness panel */}
          {gameOver && (
            <div style={{
              padding: 14, background: "#f7fafc",
              border: "1px solid #e2e8f0", borderRadius: 8,
            }}>
              {analyzing && (
                <p style={{ color: "#555", margin: 0, fontSize: 14 }}>Analyzing your game...</p>
              )}
              {!analyzing && analysisResult && (
                <>
                  {!reviewMove ? (
                    <>
                      <h3 style={{ margin: "0 0 8px", fontSize: 15, color: "#2d3748" }}>Game Analysis</h3>
                      <p style={{ margin: "0 0 8px", color: "#4a5568", fontSize: 14, lineHeight: 1.6 }}>{analysisResult.weakness_summary}</p>
                      {analysisResult.explanation && (
                        <p style={{ margin: "0 0 12px", fontSize: 13, color: "#718096", lineHeight: 1.5 }}>{analysisResult.explanation}</p>
                      )}
                      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 12 }}>
                        {(analysisResult.themes || []).map((t) => (
                          <span key={t} style={{
                            background: "#bee3f8", color: "#2b6cb0",
                            padding: "2px 10px", borderRadius: 12, fontSize: 13, fontWeight: 600,
                          }}>
                            {THEME_LABELS[t] || t}
                          </span>
                        ))}
                      </div>
                    </>
                  ) : (
                    <div style={{ marginBottom: 10 }}>
                      <button
                        onClick={exitReview}
                        style={{
                          background: "none", border: "none", color: "#3182ce",
                          fontSize: 13, cursor: "pointer", padding: "0 0 8px",
                          display: "block", fontWeight: 600,
                        }}
                      >
                        ← Back to analysis
                      </button>
                      {/* Move label */}
                      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
                        <span style={{ fontWeight: 700, fontSize: 14 }}>
                          Move {reviewMove.move_number}: <span style={{ fontFamily: "monospace" }}>{reviewMove.san}</span>
                        </span>
                        <span style={{
                          fontSize: 12, fontWeight: 600, padding: "1px 8px", borderRadius: 10,
                          background: reviewMove.label === "Blunder" ? "#fed7d7" : reviewMove.label === "Mistake" ? "#feebc8" : "#fefcbf",
                          color: reviewMove.label === "Blunder" ? "#c53030" : reviewMove.label === "Mistake" ? "#c05621" : "#744210",
                        }}>
                          {reviewMove.label} ({reviewMove.cp_delta > 0 ? "+" : ""}{reviewMove.cp_delta} cp)
                        </span>
                      </div>
                      {/* Navigation */}
                      <div style={{ display: "flex", gap: 6, marginBottom: 10 }}>
                        <button
                          onClick={reviewUndo}
                          disabled={reviewHistoryIndex <= 0}
                          style={{ padding: "4px 12px", fontSize: 13, cursor: reviewHistoryIndex > 0 ? "pointer" : "not-allowed" }}
                        >
                          ← Prev
                        </button>
                        <button
                          onClick={reviewRedo}
                          disabled={reviewHistoryIndex >= history.length - 1}
                          style={{ padding: "4px 12px", fontSize: 13, cursor: reviewHistoryIndex < history.length - 1 ? "pointer" : "not-allowed" }}
                        >
                          Next →
                        </button>
                        <span style={{ fontSize: 12, color: "#718096", alignSelf: "center", marginLeft: 4 }}>
                          {reviewHistoryIndex === moveHistory.indexOf(reviewMove) * 2
                            ? "Position before mistake (green = best move)"
                            : `${reviewHistoryIndex - moveHistory.indexOf(reviewMove) * 2} move${Math.abs(reviewHistoryIndex - moveHistory.indexOf(reviewMove) * 2) !== 1 ? "s" : ""} ${reviewHistoryIndex > moveHistory.indexOf(reviewMove) * 2 ? "after" : "before"}`}
                        </span>
                      </div>
                      {/* LLM explanation */}
                      {reviewExplaining && (
                        <p style={{ fontSize: 13, color: "#718096", margin: "0 0 6px", fontStyle: "italic" }}>
                          Analyzing mistake...
                        </p>
                      )}
                      {reviewExplanation && (
                        <div style={{
                          background: "#fffaf0", border: "1px solid #fbd38d",
                          borderRadius: 6, padding: "8px 10px", fontSize: 13, color: "#744210", lineHeight: 1.6,
                        }}>
                          {reviewExplanation}
                        </div>
                      )}
                    </div>
                  )}

                  {/* Key Mistakes list */}
                  {(() => {
                    const PAGE_SIZE = 5;
                    const mistakes = moveHistory.filter(m => m.label !== "Good");
                    if (!mistakes.length) return null;
                    const totalPages = Math.ceil(mistakes.length / PAGE_SIZE);
                    const page = Math.min(mistakePage, totalPages - 1);
                    const pageItems = mistakes.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);
                    return (
                      <div style={{ marginBottom: 12 }}>
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
                          <h4 style={{ margin: 0, fontSize: 13, color: "#2d3748" }}>Key Mistakes — click to review</h4>
                          {totalPages > 1 && (
                            <span style={{ fontSize: 12, color: "#718096" }}>
                              {page + 1} / {totalPages}
                            </span>
                          )}
                        </div>
                        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                          {pageItems.map((m, i) => {
                            const isBlunder = m.label === "Blunder";
                            const isMistake = m.label === "Mistake";
                            const isActive = reviewMove === m;
                            return (
                              <button
                                key={i}
                                onClick={() => handleReviewMove(m)}
                                style={{
                                  display: "flex", justifyContent: "space-between", alignItems: "center",
                                  background: isActive ? "#ebf8ff" : "#fff",
                                  border: `1px solid ${isActive ? "#63b3ed" : "#e2e8f0"}`,
                                  borderRadius: 6, padding: "5px 10px", cursor: "pointer",
                                  textAlign: "left", fontSize: 13,
                                }}
                              >
                                <span style={{ fontWeight: 600 }}>
                                  Move {m.move_number}: <span style={{ fontFamily: "monospace" }}>{m.san}</span>
                                </span>
                                <span style={{
                                  color: isBlunder ? "#c53030" : isMistake ? "#c05621" : "#744210",
                                  fontWeight: 600, fontSize: 12,
                                }}>
                                  {m.label} ({m.cp_delta > 0 ? "+" : ""}{m.cp_delta} cp)
                                </span>
                              </button>
                            );
                          })}
                        </div>
                        {totalPages > 1 && (
                          <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
                            <button
                              onClick={() => setMistakePage(p => Math.max(0, p - 1))}
                              disabled={page === 0}
                              style={{ fontSize: 12, padding: "3px 10px", cursor: page > 0 ? "pointer" : "not-allowed" }}
                            >
                              ← Prev
                            </button>
                            <button
                              onClick={() => setMistakePage(p => Math.min(totalPages - 1, p + 1))}
                              disabled={page === totalPages - 1}
                              style={{ fontSize: 12, padding: "3px 10px", cursor: page < totalPages - 1 ? "pointer" : "not-allowed" }}
                            >
                              Next →
                            </button>
                          </div>
                        )}
                      </div>
                    );
                  })()}
                  )}

                  {!reviewMove && (
                    <button
                      onClick={() => startPuzzleSession(analysisResult.themes)}
                      disabled={loadingPuzzles}
                      style={{
                        background: "#3182ce", color: "#fff", border: "none",
                        padding: "8px 16px", borderRadius: 6, cursor: "pointer",
                        fontWeight: 600, fontSize: 14, width: "100%",
                      }}
                    >
                      {loadingPuzzles ? "Loading puzzles…" : "Practice These Puzzles"}
                    </button>
                  )}
                </>
              )}
              {!analyzing && !analysisResult && (
                <div>
                  <p style={{ color: "#c53030", margin: "0 0 8px", fontSize: 14 }}>
                    {analysisError
                      ? `Analysis failed: ${analysisError}`
                      : "Game analysis unavailable (requires Gemini API)."}
                  </p>
                  {analysisError && (
                    <button
                      onClick={() => triggerGameAnalysis(moveHistory)}
                      style={{
                        background: "#3182ce", color: "#fff", border: "none",
                        padding: "6px 14px", borderRadius: 6, cursor: "pointer",
                        fontWeight: 600, fontSize: 13,
                      }}
                    >
                      Retry Analysis
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

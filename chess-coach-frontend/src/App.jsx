import { useEffect, useMemo, useState } from "react";
import axios from "axios";
import { Chess } from "chess.js";
import { Chessboard } from "react-chessboard";

const API = "http://127.0.0.1:8000"; // FastAPI


//export default means this is the function React will load when it starts your app.
export default function App() {
  //game = current chess board state
  //setGame = way to replace it (e.g. after moves, reset)
  const [game, setGame] = useState(() => new Chess());

  //starting message is ""
  const [msg, setMsg] = useState("");
  const [coach, setCoach] = useState("");
  const [lastSummary, setLastSummary] = useState(null);
  const [askText, setAskText] = useState("");
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  // Stockfish Skill Level (1–6). See backend presets.
  const [skill, setSkill] = useState(3);
  // History holds a timeline of FENs; last item is current position
  const [history, setHistory] = useState(() => [new Chess().fen()]);
  const [redoStack, setRedoStack] = useState([]); // stack of FENs for redo
  // Track last moves to highlight squares
  const [lastUserMove, setLastUserMove] = useState(null);   // [from, to]
  const [lastCoachMove, setLastCoachMove] = useState(null); // [from, to]
  const [selectedSquare, setSelectedSquare] = useState(null); // for click-to-move
  // Legal targets for the currently selected piece
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
  // Engine CP evaluation for current position (from both POVs)
  const [cp, setCp] = useState({ white: null, black: null });

  const PIECE_SYMBOLS = {
    w: { p: "♙", n: "♘", b: "♗", r: "♖", q: "♕" },
    b: { p: "♟", n: "♞", b: "♝", r: "♜", q: "♛" },
  };

  // Returns { byWhite: [{color,type},...], byBlack: [{color,type},...] }
  // byWhite = black pieces captured by the user; byBlack = white pieces captured by the coach.
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

    const byWhite = []; // user (White) captured these black pieces
    const byBlack = []; // coach (Black) captured these white pieces

    for (let i = 1; i < history.length; i++) {
      const prev = countPieces(history[i - 1]);
      const curr = countPieces(history[i]);
      for (const color of ["w", "b"]) {
        for (const t of ["p", "n", "b", "r", "q"]) {
          const diff = curr[color][t] - prev[color][t];
          if (diff < 0) {
            // A piece of `color` disappeared — captured by the other side
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

  // Derived material score based on captures across history (kept in addition to CP)
  const score = useMemo(() => {
    const pieceValues = { p: 1, n: 3, b: 3, r: 5, q: 9 };
    return {
      user: capturedPieces.byWhite.reduce((s, p) => s + (pieceValues[p.type] || 0), 0),
      coach: capturedPieces.byBlack.reduce((s, p) => s + (pieceValues[p.type] || 0), 0),
    };
  }, [capturedPieces]);

  // Mount log to verify console output
  useEffect(() => {
    console.log("App mounted; initial FEN:", game.fen());
  }, []);

  // Row of captured piece symbols
  function CapturedRow({ pieces, label }) {
    if (!pieces.length) return (
      <div style={{ minHeight: 24, display: "flex", alignItems: "center", fontSize: 13, color: "#999" }}>
        {label}: —
      </div>
    );
    // Sort by value descending (queen first)
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

  // Lightweight evaluation bar showing white vs black with cp cap
  function EvalBar({ white, black, cap = 800 }) {
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, Number.isFinite(v) ? v : 0));
    const fmt = (v) => (v == null || isNaN(v) ? "—" : `${v > 0 ? "+" : ""}${Math.round(v)}`);
    const w = clamp(white ?? 0, -cap, cap);
    const p = (w + cap) / (2 * cap); // 0 -> black winning, 1 -> white winning (from white POV)
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

//check hint from backend
  async function onHint() {
    try {
      setBusy(true);
      //send POST request to backend /hint endpoint with current FEN
      const res = await axios.post(`${API}/hint`, { fen: game.fen() });
      //If res.data.idea exists, show it as a hint with a 💡 icon. Else, "no hint"
      setCoach(res.data?.idea ? `💡 Hint: ${res.data.idea}` : "No hint.");
    } catch {
      setMsg("Failed to get hint.");
    } finally {
      setBusy(false);
    }
  }



  // Called when a piece is dropped; must return true (keep) or false (snap back)
  
  async function onPieceDrop(from, to) {
    // Check legality locally (good UX)
    const fenBefore = game.fen();
    const temp = new Chess(game.fen());
    // Detect if this is a pawn promotion (pawn reaching last rank)
    const fromPiece = (() => { try { return temp.get(from); } catch { return null; } })();
    const willPromote = !!(
      fromPiece &&
      fromPiece.type === "p" && (
        (fromPiece.color === "w" && to[1] === "8") ||
        (fromPiece.color === "b" && to[1] === "1")
      )
    );
    const promotion = willPromote ? "q" : undefined; // auto-queen
    const moved = temp.move({ from, to, ...(promotion ? { promotion } : {}) });
    if (!moved) {
      setMsg("❌ Illegal move.");
      return false;
    }

    try {
      setBusy(true);
      setMsg("");
      setCoach("");
      // User is making a new move; any future redo is invalid now
      setRedoStack([]);

      // Ask backend to score the move and play a reply
      const res = await axios.post(`${API}/play`, {
        fen: game.fen(),   // FEN BEFORE your move
        uci: from + to,
        ...(promotion ? { promotion } : {}),
        // Send Stockfish skill (int). Backend falls back to Elo if needed.
        skill: Number(skill),
        // Do not block on AI; fetch summary separately
        summary: false,
      });

      if (!res.data?.ok) {
        setMsg(res.data?.error || "Move rejected.");
        return false;
      }

      // Update to user position (half-move) and record in history
      if (res.data.fen_after_user) {
        //load new chess board
        const g1 = new Chess();
        //Load the FEN string from the backend into that new Chess object.
        g1.load(res.data.fen_after_user);
        //Update React’s state (game) to this new object.
        setGame(g1);
        // Record this half-move in history
        setHistory((h) => [...h, g1.fen()]);
        setRedoStack([]);
        setLastUserMove([from, to]);
        // Show CP after user's move if provided
        if (res.data.cp_after_user) {
          setCp({
            white: res.data.cp_after_user.white,
            black: res.data.cp_after_user.black,
          });
        }
      }

      ///PAUSE HERE: brief delay before showing coach reply
      await new Promise((r) => setTimeout(r, 800));

      // Apply coach reply (half-move) and record in history
      if (res.data.fen_after_coach) {
        const g2 = new Chess();
        g2.load(res.data.fen_after_coach);
        setGame(g2);
        setHistory((h) => [...h, g2.fen()]);
        setRedoStack([]);
        // Parse coach move UCI (e.g., e7e5, e7e8q)
        const coachUci = res.data.coach?.uci;
        if (coachUci && coachUci.length >= 4) {
          setLastCoachMove([coachUci.slice(0, 2), coachUci.slice(2, 4)]);
        } else {
          setLastCoachMove(null);
        }
      }

      // Update CP after coach reply if provided
      if (res.data.cp_after_coach) {
        setCp({
          white: res.data.cp_after_coach.white,
          black: res.data.cp_after_coach.black,
        });
      }

      // After applying moves, detect local game end to avoid early/false positives
      const latestFen = res.data.fen_after_coach || res.data.fen_after_user;
      if (latestFen) {
        const end = new Chess();
        end.load(latestFen);
        if (end.isCheckmate()) {
          setCoach("🏁 Checkmate");
          return true;
        }
        if (end.isStalemate()) {
          setCoach("🏁 Stalemate");
          return true;
        }
        if (end.isDraw()) {
          setCoach("🏁 Draw");
          return true;
        }
      }

      // Show evaluation label/delta and brief guidance when game continues
      const label = res.data.user?.label;
      const delta = res.data.user?.delta_cp;
      const plan = res.data.coach?.plan;
      const idea = res.data.user?.idea;
      const evalText = label != null && delta != null ? `📝 Your move: ${label} (Δ ${delta} cp)` : "";

      // Only fetch AI summary for non-Good moves per backend thresholds
      if (label !== "Good") {
        const loadingLine = [evalText, "🤖 Coach: Summarizing…"].filter(Boolean).join(" • ");
        setCoach(loadingLine);

        // Fire-and-forget: fetch AI summary without blocking UX (short timeout)
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
              const line = [
                evalText,
                `🤖 Coach${v}: ${aiText}`,
              ].filter(Boolean).join(" • ");
              setCoach(line);
              setLastSummary(ai);
            } else if (sres.data?.error) {
              console.warn("AI summary error:", sres.data.error);
              const fallback = [evalText, idea ? `💡 Idea: ${idea}` : ""].filter(Boolean).join(" • ");
              setCoach(fallback);
              setLastSummary(null);
            }
          } catch (e) {
            console.warn("AI summary request failed", e);
            const fallback = [evalText, idea ? `💡 Idea: ${idea}` : ""].filter(Boolean).join(" • ");
            setCoach(fallback);
            setLastSummary(null);
          }
        })();
      } else {
        // For good/non-negative moves, skip LLM and show immediate guidance
        const fallback = [evalText, idea ? `💡 Idea: ${idea}` : ""].filter(Boolean).join(" • ");
        setCoach(fallback);
        setLastSummary(null);
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
  }

  function onUndo() {
    if (busy) return;
    // Need at least 2 positions to step back (can't go before initial)
    setHistory((h) => {
      if (h.length <= 1) return h;
      const current = h[h.length - 1];
      const prev = h[h.length - 2];
      // Move current to redo, step back to prev
      setRedoStack((r) => [...r, current]);
      const g = new Chess();
      g.load(prev);
      setGame(g);
      return h.slice(0, -1);
    });
    setMsg("");
    setCoach("");
    setLastUserMove(null);
    setLastCoachMove(null);
    setSelectedSquare(null);
  }

  function onRedo() {
    if (busy) return;
    setRedoStack((r) => {
      if (!r.length) return r;
      const next = r[r.length - 1];
      // Advance one half-move: push to history
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

  // Build square highlight styles for last moves, selection, and legal targets
  const customSquareStyles = useMemo(() => {
    const styles = {};
    const mark = (sq, color) => {
      styles[sq] = {
        boxShadow: `inset 0 0 0 3px ${color}`,
      };
    };
    const shade = (sq, color) => {
      styles[sq] = {
        ...(styles[sq] || {}),
        background: color,
      };
    };
    if (selectedSquare) {
      mark(selectedSquare, "#68d391"); // green for selection
    }
    // Pink overlay for allowed destination squares
    if (legalTargets && legalTargets.length) {
      for (const t of legalTargets) shade(t, "rgba(255, 105, 180, 0.35)"); // hotpink overlay
    }
    if (lastUserMove) {
      mark(lastUserMove[0], "#f6e05e"); // yellow
      mark(lastUserMove[1], "#f6e05e");
    }
    if (lastCoachMove) {
      mark(lastCoachMove[0], "#63b3ed"); // blue
      mark(lastCoachMove[1], "#63b3ed");
    }
    return styles;
  }, [lastUserMove, lastCoachMove, selectedSquare, legalTargets]);

  // Detect checkmate and compute the checkmated king's square
  const checkmatedKingSquare = useMemo(() => {
    try {
      const c = new Chess(game.fen());
      if (!c.isCheckmate()) return null;
      const loser = c.turn(); // side to move in a mate position is checkmated
      const files = "abcdefgh";
      const board = c.board(); // row 0 -> rank 8, col 0 -> file a
      for (let r = 0; r < 8; r++) {
        for (let f = 0; f < 8; f++) {
          const sq = board[r][f];
          if (sq && sq.type === "k" && sq.color === loser) {
            return `${files[f]}${8 - r}`;
          }
        }
      }
    } catch {
      return null;
    }
    return null;
  }, [game.fen()]);

  const BOARD_SIZE = 480; // keep in sync with Chessboard boardWidth
  const squarePixel = BOARD_SIZE / 8;
  const overlayStyleForSquare = (square) => {
    if (!square) return {};
    const files = "abcdefgh";
    const file = square[0];
    const rank = Number(square[1]);
    const col = files.indexOf(file);
    const row = 8 - rank; // row 0 is top
    return {
      position: "absolute",
      left: col * squarePixel,
      top: row * squarePixel,
      width: squarePixel,
      height: squarePixel,
      background: "rgba(220, 38, 38, 0.6)", // red overlay
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      color: "#fff",
      fontWeight: 800,
      textTransform: "uppercase",
      textShadow: "0 1px 2px rgba(0,0,0,0.6)",
      zIndex: 5,
      pointerEvents: "none",
    };
  };

  // Click-to-move handler
  async function onSquareClickHandler(square) {
    if (busy) return;
    const temp = new Chess(game.fen());
    const piece = temp.get(square);

    // No selection yet: select own piece only
    if (!selectedSquare) {
      if (piece && piece.color === temp.turn()) setSelectedSquare(square);
      return;
    }

    // If clicking another own piece, switch selection (do not deselect)
    if (piece && piece.color === temp.turn()) {
      setSelectedSquare(square);
      return;
    }

    // Otherwise attempt the move from selectedSquare to clicked square
    const ok = await onPieceDrop(selectedSquare, square);
    if (ok) {
      // Clear selection after a successful move
      setSelectedSquare(null);
    } else {
      // Keep current selection on failed move
      // (user can choose another destination or switch to another piece)
    }
  }

  return (
    <div style={{ maxWidth: 520, margin: "24px auto", fontFamily: "system-ui, sans-serif" }}>
      <h2 style={{ marginBottom: 12 }}>AI Chess Coach</h2>
      <div style={{ display: "flex", flexDirection: "column", gap: 4, margin: "6px 0 12px", fontWeight: 600 }}>
        <div style={{ display: "flex", gap: 16 }}>
          <span>You: {score.user}</span>
          <span>Computer: {score.coach}</span>
        </div>
        <div style={{ display: "flex", gap: 16, fontWeight: 500 }}>
          <span>You (White): {cp.white !== null ? `${cp.white} cp` : "—"}</span>
          <span>Computer (Black): {cp.black !== null ? `${cp.black} cp` : "—"}</span>
        </div>
      </div>

      <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
        <select value={skill} onChange={(e) => setSkill(Number(e.target.value))} disabled={busy}>
          <option value={1}>1 - &lt;400</option>
          <option value={2}>2 - 500</option>
          <option value={3}>3 - 800</option>
          <option value={4}>4 - 1100</option>
          <option value={5}>5 - 1500</option>
          <option value={6}>6 - 1900</option>
        </select>
        <button
          onClick={onHint}
          disabled={busy}
          style={{ whiteSpace: "nowrap", minWidth: "10ch" }}
        >
          {busy ? "Thinking…" : "Get\u00A0Hint"}
        </button>
        <button onClick={onUndo} disabled={busy || history.length <= 1}>Undo</button>
        <button onClick={onRedo} disabled={busy || redoStack.length === 0}>Redo</button>
        <button onClick={onReset} disabled={busy}>Reset</button>
      </div>

      {/* Evaluation bar (cp, capped at 800) */}
      <EvalBar white={cp.white} black={cp.black} cap={800} />

      {/* Pieces captured by the coach (Black) — shown above the board on the opponent's side */}
      <CapturedRow pieces={capturedPieces.byBlack} label="Coach captured" />

      <div style={{ position: "relative", width: BOARD_SIZE, height: BOARD_SIZE }}>
        <Chessboard
          position={game.fen()}
          onPieceDrop={onPieceDrop}
          onPieceDragBegin={(piece, sourceSquare) => console.log("onPieceDragBegin", { piece, sourceSquare })}
          onPieceDragEnd={(piece, sourceSquare, targetSquare) => console.log("onPieceDragEnd", { piece, sourceSquare, targetSquare })}
          onSquareClick={onSquareClickHandler}
          arePiecesDraggable={true}
          customSquareStyles={customSquareStyles}
          boardWidth={BOARD_SIZE}
          animationDuration={200}
        />
        {checkmatedKingSquare && (
          <div style={overlayStyleForSquare(checkmatedKingSquare)}>Checkmate</div>
        )}
      </div>

      {/* Pieces captured by you (White) — shown below the board on your side */}
      <CapturedRow pieces={capturedPieces.byWhite} label="You captured" />

      {msg && <p style={{ color: "#c00", marginTop: 10 }}>{msg}</p>}
      {coach && <p style={{ color: "#055", marginTop: 6 }}>{coach}</p>}
      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
        <input
          type="text"
          placeholder="Ask: what do you mean by…"
          value={askText}
          onChange={(e) => setAskText(e.target.value)}
          disabled={asking || !lastSummary}
          style={{ flex: 1 }}
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
              } else if (res.data?.error) {
                console.warn("Clarify error:", res.data.error);
              }
            } catch (e) {
              console.warn("Clarify request failed", e);
            } finally {
              setAsking(false);
            }
          }}
        >{asking ? "Asking…" : "Ask"}</button>
      </div>
    </div>
  );
}

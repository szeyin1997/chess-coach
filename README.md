# Chess Coach

An AI-powered chess improvement tool that identifies your recurring weaknesses and trains you on them — the equivalent of Chess.com's paid Game Review feature, built with Stockfish and Google Gemini.

---

## Why I built this

I wanted to get better at chess but hit a wall: the useful coaching features on Chess.com are locked behind a $30/month subscription. So I built my own.

The first version gave move-by-move AI commentary ("Nc3 was a mistake because of the bishop pair"). I used it for a while and realized it wasn't actually helping me improve. The feedback felt like noise.

The insight: **chess improvement happens at the pattern level, not the move level.** You don't get better by knowing a specific move was wrong. You get better by realizing "I always hang pieces when I'm attacking" or "I lose the thread positionally after move 20." That's what coaches and premium tools identify — recurring patterns across a full game.

This project is my attempt to build that.

---

## What it does

**Play a game** against Stockfish at adjustable skill levels (roughly 400–1900 Elo).

**Post-game analysis** — after the game ends, Gemini reads the full annotated game and identifies 1–2 recurring weakness patterns (not just individual mistakes).

**Targeted puzzle training** — puzzles from the Lichess database (~330MB, 30k+ beginner-to-intermediate puzzles) are matched to your detected weakness themes.

**Mistake review** — click any blunder or mistake to replay the position, see the best move highlighted, and read an AI explanation of what went wrong.

---

## Tech stack

| Layer | Tech |
|---|---|
| Frontend | React 18 + Vite |
| Backend | Python, FastAPI |
| Chess engine | Stockfish (via python-chess) |
| AI coaching | Google Gemini API (OpenAI fallback) |
| Puzzle database | SQLite, sourced from Lichess open data |

---

## Running locally

**Prerequisites:** Python 3.10+, Node.js 18+, Stockfish installed (`brew install stockfish`)

```bash
# Clone the repo
git clone https://github.com/szeyin1997/chess-coach.git
cd chess-coach

# Set up backend
cd chess-coach-backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Add your Gemini API key
echo "GEMINI_API_KEY=your_key_here" > .env

# Download the puzzle database (~330MB, one-time)
python download_puzzles.py

# Run both servers
cd ..
bash run-dev.sh
```

Open **http://localhost:5173**

---

## What I learned

Building this taught me more about AI product design than any tutorial:

- **Prompt design changes outcomes significantly.** The same Gemini model gives generic, useless feedback with a naive prompt and genuinely insightful pattern analysis with a structured one. The model isn't the product — the prompt engineering is.
- **Per-move AI feedback is the wrong abstraction.** I shipped it, used it, and found it unhelpful. The right unit of analysis is a full game, not individual moves.
- **LLM latency is a UX problem.** Gemini responses take 3–8 seconds. I learned to design around it (async calls, progressive disclosure) rather than just waiting.

---

## What's next

- Import PGN from real Chess.com / Lichess games instead of playing against the bot
- Track weakness patterns across multiple games over time
- Replace the monolithic App.jsx with a proper component structure

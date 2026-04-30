"""
download_puzzles.py — One-time script to fetch a beginner-friendly subset of
the Lichess puzzle database and store it in a local SQLite file (puzzles.db).

Usage:
    cd chess-coach-backend
    python download_puzzles.py

The script streams the zstd-compressed CSV directly from database.lichess.org,
filters on-the-fly, and writes matching rows to puzzles.db.
Expected output: ~30K–80K puzzles, ~50 MB on disk.
"""

import csv
import io
import sqlite3
import sys
import urllib.request
from pathlib import Path

import zstandard as zstd

PUZZLE_URL = "https://database.lichess.org/lichess_db_puzzle.csv.zst"
DB_PATH = Path(__file__).with_name("puzzles.db")

# Tactical themes we care about for beginner gap training
TARGET_THEMES = {
    "mateIn1", "mateIn2", "mateIn3", "mateIn4", "mateIn5",
    "fork", "hangingPiece", "pin", "skewer",
    "discoveredAttack", "crushing", "defensiveMove",
}

RATING_MIN = 400
RATING_MAX = 1100  # slightly above 1000 to have headroom


def create_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS puzzles (
            id          TEXT PRIMARY KEY,
            fen         TEXT NOT NULL,
            moves       TEXT NOT NULL,
            rating      INTEGER NOT NULL,
            themes      TEXT NOT NULL,
            game_url    TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rating ON puzzles(rating)")
    conn.commit()


def matches(themes_str: str) -> bool:
    themes = set(themes_str.split())
    return bool(themes & TARGET_THEMES)


def download_and_import() -> None:
    print(f"Connecting to {PUZZLE_URL} …")
    print("(This will stream several hundred MB — it may take a few minutes.)")

    conn = sqlite3.connect(DB_PATH)
    create_table(conn)

    inserted = 0
    skipped = 0
    batch: list = []
    BATCH_SIZE = 500

    def flush():
        nonlocal inserted
        conn.executemany(
            "INSERT OR IGNORE INTO puzzles (id, fen, moves, rating, themes, game_url) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            batch,
        )
        conn.commit()
        inserted += len(batch)
        batch.clear()

    with urllib.request.urlopen(PUZZLE_URL) as response:
        dctx = zstd.ZstdDecompressor()
        text_buffer = ""

        with dctx.stream_reader(response) as reader:
            while True:
                chunk = reader.read(1 << 16)  # 64 KB chunks
                if not chunk:
                    break
                text_buffer += chunk.decode("utf-8", errors="replace")

                lines = text_buffer.split("\n")
                text_buffer = lines[-1]  # keep incomplete last line

                for line in lines[:-1]:
                    line = line.strip()
                    if not line or line.startswith("PuzzleId"):
                        continue  # skip header

                    parts = line.split(",")
                    if len(parts) < 8:
                        skipped += 1
                        continue

                    puzzle_id = parts[0]
                    fen = parts[1]
                    moves = parts[2]
                    try:
                        rating = int(parts[3])
                    except ValueError:
                        skipped += 1
                        continue
                    themes_str = parts[7] if len(parts) > 7 else ""
                    game_url = parts[8] if len(parts) > 8 else ""

                    if not (RATING_MIN <= rating <= RATING_MAX):
                        skipped += 1
                        continue
                    if not matches(themes_str):
                        skipped += 1
                        continue

                    batch.append((puzzle_id, fen, moves, rating, themes_str, game_url))
                    if len(batch) >= BATCH_SIZE:
                        flush()
                        print(f"  Inserted {inserted:,} puzzles so far …", end="\r", flush=True)

        # Flush remaining
        if batch:
            flush()

    conn.close()
    print(f"\nDone. Inserted {inserted:,} puzzles (skipped {skipped:,}) → {DB_PATH}")


if __name__ == "__main__":
    download_and_import()

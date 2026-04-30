"""
puzzle_db.py — SQLite query helper for the Lichess puzzle subset.

Expects puzzles.db to exist in the same directory (created by download_puzzles.py).
"""

import random
import sqlite3
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).with_name("puzzles.db")

# Known tactical themes supported by the puzzle trainer
KNOWN_THEMES = [
    "mateIn1", "mateIn2", "mateIn3", "mateIn4", "mateIn5",
    "fork", "hangingPiece", "pin", "skewer",
    "discoveredAttack", "crushing", "defensiveMove",
]

# In-memory cache: theme -> list of puzzle dicts (loaded once at startup)
_CACHE_PER_THEME = 2000  # puzzles to keep per theme
_theme_cache: dict[str, list[dict]] = {}
_fallback_cache: list[dict] = []


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"puzzles.db not found at {DB_PATH}. "
            "Run `python download_puzzles.py` first."
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _build_cache() -> None:
    """Load puzzle samples per theme into memory once at startup."""
    global _theme_cache, _fallback_cache
    if _theme_cache:
        return  # already built
    logger.info("Building puzzle theme cache from DB (one-time)…")
    conn = _connect()
    try:
        for theme in KNOWN_THEMES:
            rows = conn.execute(
                "SELECT id, fen, moves, rating, themes, game_url "
                "FROM puzzles WHERE themes LIKE ? AND rating BETWEEN 400 AND 1100 "
                f"LIMIT {_CACHE_PER_THEME}",
                (f"%{theme}%",),
            ).fetchall()
            _theme_cache[theme] = [dict(r) for r in rows]
            logger.info("  %s: %d puzzles cached", theme, len(_theme_cache[theme]))

        # Fallback pool (no theme filter)
        rows = conn.execute(
            f"SELECT id, fen, moves, rating, themes, game_url "
            f"FROM puzzles WHERE rating BETWEEN 400 AND 1100 LIMIT {_CACHE_PER_THEME}",
        ).fetchall()
        _fallback_cache = [dict(r) for r in rows]
    finally:
        conn.close()
    logger.info("Puzzle cache ready.")


def get_puzzles_by_themes(
    themes: list[str],
    count: int = 5,
    rating_min: int = 400,
    rating_max: int = 1100,
) -> list[dict]:
    """Return `count` random puzzles whose themes overlap with `themes`.

    Falls back to any puzzle in the rating range if no theme matches found.
    Uses the in-memory cache for instant lookups.
    """
    _build_cache()

    if not themes:
        pool = _fallback_cache
    else:
        seen_ids: set[str] = set()
        pool = []
        for theme in themes:
            for p in _theme_cache.get(theme, []):
                if p["id"] not in seen_ids:
                    seen_ids.add(p["id"])
                    pool.append(p)
        if not pool:
            pool = _fallback_cache

    return random.sample(pool, min(count, len(pool)))


def db_stats() -> Optional[dict]:
    """Return basic stats about the puzzle database (for health checks)."""
    try:
        conn = _connect()
        total = conn.execute("SELECT COUNT(*) FROM puzzles").fetchone()[0]
        conn.close()
        return {"total_puzzles": total, "db_path": str(DB_PATH)}
    except Exception as e:
        return {"error": str(e)}

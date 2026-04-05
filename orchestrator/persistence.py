"""SQLite persistence layer for DRNT orchestrator.

Manages a single SQLite database via aiosqlite.  Tables are created
idempotently on first connect.  Other modules obtain the database path
via ``get_db_path()`` and open their own (synchronous) connections.
"""

from __future__ import annotations

import logging
import os

import aiosqlite

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/data/drnt.db"

_db_path: str | None = None
_db: aiosqlite.Connection | None = None


async def init_db(db_path: str | None = None) -> None:
    """Open the database and create tables if they don't exist."""
    global _db_path, _db

    _db_path = db_path or os.environ.get("DRNT_DB_PATH", DEFAULT_DB_PATH)

    db_dir = os.path.dirname(_db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    try:
        _db = await aiosqlite.connect(_db_path)

        await _db.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                data JSON,
                status TEXT,
                created_at TEXT,
                idempotency_key TEXT UNIQUE
            )"""
        )

        await _db.execute(
            """CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value JSON,
                updated_at TEXT
            )"""
        )

        await _db.commit()
        logger.info("Database initialized at %s", _db_path)
    except Exception:
        logger.error("Database init failed at %s", _db_path, exc_info=True)
        _db_path = None
        if _db is not None:
            try:
                await _db.close()
            except Exception:
                pass
        _db = None
        raise


async def close_db() -> None:
    """Close the database connection and reset module state."""
    global _db, _db_path
    if _db is not None:
        try:
            await _db.close()
        except Exception:
            pass
        _db = None
    _db_path = None


def get_db_path() -> str | None:
    """Return the current database path, or None if not initialized."""
    return _db_path

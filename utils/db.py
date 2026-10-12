"""Async SQLite storage.

The database file lives at DB_PATH (default ./data/bot.db). On Railway, mount a
volume at /data and set DB_PATH=/data/bot.db so data survives redeploys.

Schema changes go in MIGRATIONS: append a new list of statements, never edit an
old one. The applied version is tracked with SQLite's `PRAGMA user_version`.
All timestamps are stored as Unix epoch seconds (UTC).
"""

import logging
import os
import time
from typing import Any, Iterable, Optional

import aiosqlite

log = logging.getLogger("bot.db")

MIGRATIONS: list[list[str]] = [
    # v1: initial schema
    [
        """CREATE TABLE warns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            mod_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )""",
        "CREATE INDEX idx_warns_user ON warns (guild_id, user_id)",
        """CREATE TABLE giveaways (
            message_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            prize TEXT NOT NULL,
            winners INTEGER NOT NULL DEFAULT 1,
            ends_at INTEGER NOT NULL,
            ended INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX idx_giveaways_due ON giveaways (ended, ends_at)",
        """CREATE TABLE challenges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            theme TEXT NOT NULL,
            channel_id INTEGER,
            thread_id INTEGER,
            message_id INTEGER,
            opens_at INTEGER NOT NULL,
            closes_at INTEGER NOT NULL,
            winner_id INTEGER
        )""",
        """CREATE TABLE prompt_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            channel_id INTEGER,
            message_id INTEGER,
            thread_id INTEGER,
            posted_at INTEGER NOT NULL
        )""",
        """CREATE TABLE temp_roles (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            remove_at INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id, role_id)
        )""",
        "CREATE INDEX idx_temp_roles_due ON temp_roles (remove_at)",
    ],
    # v2: scheduled content (Prompt of the Day, Remix Friday, digest)
    [
        "ALTER TABLE prompt_log ADD COLUMN guild_id INTEGER",
        "ALTER TABLE prompt_log ADD COLUMN item_id INTEGER",
        "ALTER TABLE prompt_log ADD COLUMN theme TEXT",
        "ALTER TABLE prompt_log ADD COLUMN judged INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE challenges ADD COLUMN item_id INTEGER",
        "ALTER TABLE challenges ADD COLUMN reminded INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE challenges ADD COLUMN closed INTEGER NOT NULL DEFAULT 0",
        # Rotation of built-in content: one row per used item, cleared when all are used.
        """CREATE TABLE content_usage (
            kind TEXT NOT NULL,
            item_id INTEGER NOT NULL,
            used_at INTEGER NOT NULL,
            PRIMARY KEY (kind, item_id)
        )""",
        # Makes scheduled jobs idempotent (e.g. one prompt per day, even after a restart).
        """CREATE TABLE job_runs (
            job TEXT NOT NULL,
            run_key TEXT NOT NULL,
            ran_at INTEGER NOT NULL,
            PRIMARY KEY (job, run_key)
        )""",
        # Message counts per channel per day, for the digest's "most active channel".
        """CREATE TABLE channel_activity (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, channel_id, day)
        )""",
    ],
    # v3: giveaways
    [
        "ALTER TABLE giveaways ADD COLUMN host_id INTEGER",
        "ALTER TABLE giveaways ADD COLUMN required_role_id INTEGER",
        "ALTER TABLE giveaways ADD COLUMN winner_ids TEXT NOT NULL DEFAULT ''",  # comma-separated, all draws
        "ALTER TABLE giveaways ADD COLUMN created_at INTEGER",
        """CREATE TABLE giveaway_entries (
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            entered_at INTEGER NOT NULL,
            PRIMARY KEY (message_id, user_id)
        )""",
    ],
    # v4: events with RSVP buttons
    [
        """CREATE TABLE events (
            message_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            starts_at INTEGER NOT NULL,
            host_id INTEGER NOT NULL,
            reminded INTEGER NOT NULL DEFAULT 0,
            closed INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )""",
        "CREATE INDEX idx_events_open ON events (closed, starts_at)",
        """CREATE TABLE event_rsvps (
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('going', 'maybe')),
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (message_id, user_id)
        )""",
    ],
]


def now() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        folder = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(folder, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self._migrate()
        log.info("Database ready at %s", os.path.abspath(self.path))

    async def _migrate(self) -> None:
        async with self.conn.execute("PRAGMA user_version") as cur:
            (version,) = await cur.fetchone()
        for target, statements in enumerate(MIGRATIONS[version:], start=version + 1):
            log.info("Applying database migration v%d", target)
            try:
                await self.conn.execute("BEGIN")  # make DDL + version bump atomic
                for sql in statements:
                    await self.conn.execute(sql)
                await self.conn.execute(f"PRAGMA user_version = {target}")
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    # ------------------------------------------------------------ primitives
    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a write statement and commit. Returns lastrowid."""
        cur = await self.conn.execute(sql, tuple(params))
        await self.conn.commit()
        return cur.lastrowid

    async def execute_count(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a write statement and commit. Returns the number of rows changed."""
        cur = await self.conn.execute(sql, tuple(params))
        await self.conn.commit()
        return cur.rowcount

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> Optional[aiosqlite.Row]:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return list(await cur.fetchall())

    # ----------------------------------------------------------------- warns
    async def add_warn(self, guild_id: int, user_id: int, mod_id: int, reason: str) -> int:
        return await self.execute(
            "INSERT INTO warns (guild_id, user_id, mod_id, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, mod_id, reason, now()),
        )

    async def get_warns(self, guild_id: int, user_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM warns WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC, id DESC",
            (guild_id, user_id),
        )

    async def count_warns(self, guild_id: int, user_id: int) -> int:
        row = await self.fetchone("SELECT COUNT(*) FROM warns WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        return row[0]

    # ------------------------------------------------------------ temp roles
    async def add_temp_role(self, guild_id: int, user_id: int, role_id: int, remove_at: int) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO temp_roles (guild_id, user_id, role_id, remove_at) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, role_id, remove_at),
        )

    async def due_temp_roles(self, at: Optional[int] = None) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM temp_roles WHERE remove_at <= ?", (at or now(),))

    async def delete_temp_role(self, guild_id: int, user_id: int, role_id: int) -> None:
        await self.execute(
            "DELETE FROM temp_roles WHERE guild_id = ? AND user_id = ? AND role_id = ?", (guild_id, user_id, role_id)
        )

    # ---------------------------------------------------------- job runs
    async def job_ran(self, job: str, run_key: str) -> bool:
        return await self.fetchone("SELECT 1 FROM job_runs WHERE job = ? AND run_key = ?", (job, run_key)) is not None

    async def mark_job(self, job: str, run_key: str) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO job_runs (job, run_key, ran_at) VALUES (?, ?, ?)", (job, run_key, now())
        )

    # ----------------------------------------------------- content usage
    async def used_ids(self, kind: str) -> set[int]:
        rows = await self.fetchall("SELECT item_id FROM content_usage WHERE kind = ?", (kind,))
        return {r[0] for r in rows}

    async def last_used_id(self, kind: str) -> Optional[int]:
        row = await self.fetchone(
            "SELECT item_id FROM content_usage WHERE kind = ? ORDER BY used_at DESC, rowid DESC LIMIT 1", (kind,)
        )
        return row[0] if row else None

    async def mark_used(self, kind: str, item_id: int) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO content_usage (kind, item_id, used_at) VALUES (?, ?, ?)", (kind, item_id, now())
        )

    async def reset_usage(self, kind: str) -> None:
        await self.execute("DELETE FROM content_usage WHERE kind = ?", (kind,))

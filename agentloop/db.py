"""SQLite runtime state: goals, sessions, turns, inbox events, notes, meta-chat messages, detached jobs."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS goals(
  ws TEXT, id TEXT, status TEXT, meta_work TEXT, first_started REAL, started REAL, ended REAL,
  meta_turns INT DEFAULT 0, worker_turns INT DEFAULT 0, tokens INT DEFAULT 0,
  final_turn INT DEFAULT 0, idle_streak INT DEFAULT 0,
  fail_streak INT DEFAULT 0, active_s REAL DEFAULT 0, outcome TEXT,
  PRIMARY KEY(ws, id));
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, ws TEXT, goal TEXT, kind TEXT, parent TEXT, role TEXT,
  label TEXT, agent TEXT, model TEXT, effort TEXT, autocompact TEXT, vendor_id TEXT,
  cwd TEXT, branch TEXT, status TEXT, turns INT DEFAULT 0, tokens INT DEFAULT 0,
  context INT DEFAULT 0, token TEXT,
  merged INT DEFAULT 0, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS turns(
  id INTEGER PRIMARY KEY, session TEXT, n INT, started REAL, ended REAL, status TEXT,
  start_commit TEXT, end_commit TEXT, exit_code INT, tokens INT, final TEXT);
CREATE TABLE IF NOT EXISTS inbox(
  id INTEGER PRIMARY KEY, ws TEXT, goal TEXT, ts REAL, kind TEXT, payload TEXT,
  deferred INT DEFAULT 0, consumed INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY, ws TEXT, goal TEXT, ts REAL, text TEXT, author TEXT,
  status TEXT, mode TEXT, source_session TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, session TEXT, ts REAL, role TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, session TEXT, ws TEXT, goal TEXT, cmd TEXT, log TEXT, unit TEXT, pid INT,
  grant_info TEXT, started REAL, ended REAL, status TEXT, result TEXT, reported INT DEFAULT 0);
"""
# Columns added after the first release: (table, column, type)
ADDED_COLUMNS = [("goals", "first_started", "REAL"), ("goals", "base", "TEXT"), ("goals", "pushed", "REAL"),
                 ("goals", "pr", "TEXT"), ("notes", "seq", "INT")]
# Columns no longer kept: the platform does not track spend, and GPUs go to jobs (`loop run --vram`), not sessions
DROPPED_COLUMNS = [("goals", "cost"), ("sessions", "cost"), ("turns", "cost"), ("sessions", "gpu")]


class DB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            # Databases from before the mission -> goal rename
            tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "missions" in tables and "goals" not in tables:
                self.conn.execute("ALTER TABLE missions RENAME TO goals")
                for table in ("sessions", "inbox", "notes"):
                    self.conn.execute(f"ALTER TABLE {table} RENAME COLUMN mission TO goal")
            self.conn.executescript(SCHEMA)
            for table, col, typ in ADDED_COLUMNS:
                if col not in [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")]:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            # Notes are numbered per goal; ones from before that get their number in the order they were added
            self.conn.execute("UPDATE notes SET seq=(SELECT COUNT(*) FROM notes o WHERE o.ws=notes.ws "
                              "AND o.goal=notes.goal AND o.id<=notes.id) WHERE seq IS NULL")
            for table, col in DROPPED_COLUMNS:
                if col in [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")]:
                    self.conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")

    def q(self, sql: str, *args) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, *args) -> dict | None:
        rows = self.q(sql, *args)
        return rows[0] if rows else None

    def x(self, sql: str, *args) -> int:
        with self.lock:
            return self.conn.execute(sql, args).lastrowid

    # Convenience helpers used across the engine
    def update(self, table: str, key: dict, **fields) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        where = " AND ".join(f"{k}=?" for k in key)
        self.x(f"UPDATE {table} SET {sets} WHERE {where}", *fields.values(), *key.values())

    def session(self, sid: str) -> dict | None:
        return self.one("SELECT * FROM sessions WHERE id=?", sid)

    def goal(self, ws: str, gid: str) -> dict | None:
        return self.one("SELECT * FROM goals WHERE ws=? AND id=?", ws, gid)

    def push_inbox(self, ws: str, gid: str, kind: str, payload: dict, deferred: bool = False) -> int:
        return self.x("INSERT INTO inbox(ws, goal, ts, kind, payload, deferred) VALUES(?,?,?,?,?,?)",
                      ws, gid, time.time(), kind, json.dumps(payload), int(deferred))

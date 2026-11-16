"""Where the facility's events and journal lines are kept: one SQLite file, written only by appending.

The database itself refuses edits: an UPDATE on any table, or a DELETE on the facility's own stream, aborts. A visitor works on
a stream of their own over the facility's base stream, and only those streams are ever removed. Every write happens inside one
`BEGIN IMMEDIATE` transaction, so a reader inside it sees the state the write is checked against."""
from __future__ import annotations

import json
import pickle
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .books import Books, fold, lines

BASE = "base"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY,
  stream TEXT NOT NULL,
  kind TEXT NOT NULL,
  at TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  source TEXT NOT NULL,
  command TEXT,
  key TEXT NOT NULL,
  caused_by INTEGER,
  payload TEXT NOT NULL,
  UNIQUE(stream, key),
  UNIQUE(stream, id)
);
CREATE TABLE IF NOT EXISTS journal_lines(
  id INTEGER PRIMARY KEY,
  stream TEXT NOT NULL,
  event_id INTEGER NOT NULL,
  account TEXT NOT NULL,
  cents INTEGER NOT NULL,
  charge TEXT,
  FOREIGN KEY(stream, event_id) REFERENCES events(stream, id)
);
CREATE TABLE IF NOT EXISTS effects(
  stream TEXT NOT NULL,
  effect TEXT NOT NULL,
  event_id INTEGER NOT NULL,
  PRIMARY KEY(stream, effect),
  FOREIGN KEY(stream, event_id) REFERENCES events(stream, id)
);
CREATE INDEX IF NOT EXISTS events_by_stream ON events(stream, id);
CREATE INDEX IF NOT EXISTS lines_by_event ON journal_lines(stream, event_id);
"""
# the database, not the code, keeps the books append-only
TRIGGERS = "".join(
    f"""
CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} BEGIN SELECT RAISE(ABORT, '{t} are append-only'); END;
CREATE TRIGGER IF NOT EXISTS {t}_no_base_delete BEFORE DELETE ON {t} WHEN OLD.stream = 'base'
  BEGIN SELECT RAISE(ABORT, 'the facility''s own stream is never deleted'); END;"""
    for t in ("events", "journal_lines", "effects"))


class Unbalanced(Exception):
    """An event whose journal lines do not sum to zero; nothing of it is written."""


@dataclass
class Event:
    kind: str
    at: datetime                    # business time, when it happened at the facility (timezone-aware)
    payload: dict
    key: str                        # repeat-safe key, unique within a stream
    source: str = "billing"
    command: str | None = None
    caused_by: int | None = None
    effects: tuple[str, ...] = ()   # business effects that may happen once per stream ("reverse:C12")
    id: int | None = None
    stream: str = BASE
    recorded_at: datetime | None = field(default=None, compare=False)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA + TRIGGERS)
        self._base: tuple[int | None, bytes] | None = None     # (last base event id, the folded base state, pickled)

    @contextmanager
    def write(self):
        """One write transaction; everything inside it commits together or not at all."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self
            self.db.execute("COMMIT")
        except BaseException:
            # a failed COMMIT rolls back too, so the shared connection is never left inside a transaction
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def append(self, stream: str, events: list[Event], now: datetime | None = None) -> list[Event]:
        """Append events and their journal lines. Call inside write(). An event whose lines do not balance, a key already used
        in the stream, or a business effect already taken in the stream raises, and the caller's transaction rolls back."""
        now = now or datetime.now(timezone.utc)
        for e in events:
            posted = lines(e.kind, e.payload)
            if sum(c for _, c, _ in posted) != 0:
                raise Unbalanced(f"{e.kind} {e.key}: lines sum to {sum(c for _, c, _ in posted)}")
            cur = self.db.execute(
                "INSERT INTO events(stream, kind, at, recorded_at, source, command, key, caused_by, payload) VALUES (?,?,?,?,?,?,?,?,?)",
                (stream, e.kind, _iso(e.at), _iso(now), e.source, e.command, e.key, e.caused_by, json.dumps(e.payload, sort_keys=True)))
            e.id, e.stream, e.recorded_at = cur.lastrowid, stream, now
            self.db.executemany("INSERT INTO journal_lines(stream, event_id, account, cents, charge) VALUES (?,?,?,?,?)",
                                [(stream, e.id, a, c, ch) for a, c, ch in posted])
            self.db.executemany("INSERT INTO effects(stream, effect, event_id) VALUES (?,?,?)", [(stream, f, e.id) for f in e.effects])
        return events

    def events(self, stream: str = BASE) -> list[Event]:
        """The facility's events, then the visitor stream's own, in the order they were written."""
        streams = (BASE,) if stream == BASE else (BASE, stream)
        rows = self.db.execute(
            f"SELECT id, stream, kind, at, recorded_at, source, command, key, caused_by, payload FROM events WHERE stream IN ({','.join('?' * len(streams))}) "
            "ORDER BY stream != 'base', id", streams).fetchall()
        return [Event(kind=k, at=datetime.fromisoformat(at), payload=json.loads(p), key=key, source=src, command=cmd, caused_by=cb, id=i,
                      stream=s, recorded_at=datetime.fromisoformat(rec)) for i, s, k, at, rec, src, cmd, key, cb, p in rows]

    def stream_events(self, stream: str) -> list[Event]:
        """Only the stream's own events (not the facility's base events under it)."""
        return [e for e in self.events(stream) if e.stream == stream] if stream == BASE else self._rows(stream)

    def _rows(self, stream: str) -> list[Event]:
        rows = self.db.execute("SELECT id, stream, kind, at, recorded_at, source, command, key, caused_by, payload FROM events WHERE stream = ? "
                               "ORDER BY id", (stream,)).fetchall()
        return [Event(kind=k, at=datetime.fromisoformat(at), payload=json.loads(p), key=key, source=src, command=cmd, caused_by=cb, id=i,
                      stream=s, recorded_at=datetime.fromisoformat(rec)) for i, s, k, at, rec, src, cmd, key, cb, p in rows]

    def books(self, stream: str = BASE) -> Books:
        """The folded state of a stream: the facility's year is folded once and kept as a snapshot (rebuilt only when the base
        stream grows), and a visitor's own events are applied on top of a fresh copy of it."""
        last = self.db.execute("SELECT MAX(id) FROM events WHERE stream = 'base'").fetchone()[0]
        if self._base is None or self._base[0] != last:
            self._base = (last, pickle.dumps(fold(self._rows(BASE))))
        books = pickle.loads(self._base[1])
        if stream != BASE:
            for e in self._rows(stream):
                books.apply(e.kind, e.at, e.payload)
        return books

    def journal(self, stream: str = BASE) -> list[tuple[int, str, int, str | None]]:
        return self.db.execute("SELECT event_id, account, cents, charge FROM journal_lines WHERE stream = ? ORDER BY id", (stream,)).fetchall()

    def drop_stream(self, stream: str) -> None:
        """Remove a visitor's scratch stream, lines and effects first. The base stream cannot be dropped (the triggers refuse)."""
        if stream == BASE:
            raise ValueError("the facility's own stream is never deleted")
        with self.write():
            for t in ("journal_lines", "effects", "events"):
                self.db.execute(f"DELETE FROM {t} WHERE stream = ?", (stream,))

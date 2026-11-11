"""Background SQLite WAL writer for longitudinal mouse samples."""
from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path
import queue
import sqlite3
import sys
import threading
from time import monotonic, time_ns
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

_STOP = object()
_QUEUE_MAXSIZE = 4096
_COMMIT_INTERVAL_S = 0.25
_THREAD_PRIORITY_LOWEST = -2
_DB_FILENAME = "iograph_mouse.sqlite"
_CSV_HEADER = "x,y,time\n"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id     INTEGER PRIMARY KEY,
  started_at_us  INTEGER NOT NULL,
  ended_at_us    INTEGER,
  app_version    TEXT,
  host           TEXT
);

CREATE TABLE IF NOT EXISTS samples (
  session_id  INTEGER NOT NULL REFERENCES sessions(session_id),
  t_wall_us   INTEGER NOT NULL,
  t_mono_us   INTEGER NOT NULL,
  x           INTEGER,
  y           INTEGER
);

CREATE INDEX IF NOT EXISTS idx_samples_wall ON samples(t_wall_us);
CREATE INDEX IF NOT EXISTS idx_samples_session_mono ON samples(session_id, t_mono_us);
"""


def wall_us_now() -> int:
    return int(time_ns() // 1000)


def default_sqlite_path(directory: Path) -> Path:
    return Path(directory) / _DB_FILENAME


def format_csv_row(x: int | float | None, y: int | float | None, t_ms: int) -> str:
    if x is None or y is None:
        return f",,{int(t_ms)}\n"
    return f"{int(round(x))},{int(round(y))},{int(t_ms)}\n"


def _set_current_thread_low_priority() -> None:
    try:
        if sys.platform.startswith("win"):
            kernel32 = ctypes.windll.kernel32
            kernel32.SetThreadPriority(kernel32.GetCurrentThread(), _THREAD_PRIORITY_LOWEST)
            return
        os.nice(10)
    except Exception:
        logger.debug("Unable to lower SQLite writer thread priority", exc_info=True)


def export_session_csv_text(db_path: Path, session_id: int) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT x, y, t_mono_us FROM samples WHERE session_id = ? ORDER BY t_mono_us ASC, rowid ASC",
            (int(session_id),),
        ).fetchall()
    finally:
        conn.close()
    lines = [_CSV_HEADER.rstrip("\n")]
    for x, y, t_mono_us in rows:
        t_ms = int(t_mono_us) // 1000
        if x is None or y is None:
            lines.append(f",,{t_ms}")
        else:
            lines.append(f"{int(x)},{int(y)},{t_ms}")
    ## END for x, y, t_mono_us in rows....

    return "\n".join(lines) + "\n"


def export_session_csv_to_path(db_path: Path, session_id: int, csv_path: Path) -> int:
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    written = 0
    try:
        cursor = conn.execute(
            "SELECT x, y, t_mono_us FROM samples WHERE session_id = ? ORDER BY t_mono_us ASC, rowid ASC",
            (int(session_id),),
        )
        with csv_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(_CSV_HEADER)
            for x, y, t_mono_us in cursor:
                handle.write(format_csv_row(x, y, int(t_mono_us) // 1000))
                written += 1
            ## END for x, y, t_mono_us in cursor....
    finally:
        conn.close()
    return written


class SqliteStreamWriter:
    """Append compact mouse samples to a SQLite WAL database on a background thread."""

    def __init__(self) -> None:
        self._queue: queue.Queue[object] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._count_lock = threading.Lock()
        self._meta_lock = threading.Lock()
        self._path: Path | None = None
        self._session_id: int | None = None
        self._written = 0
        self._pending_idle: tuple[int, int] | None = None  # (t_wall_us, t_mono_us)
        self._active = False
        self._error: str | None = None
        self._started_at_us: int | None = None
        self._app_version: str | None = None
        self._host: str | None = None

    @property
    def is_open(self) -> bool:
        return self._active

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def session_id(self) -> int | None:
        with self._meta_lock:
            return self._session_id

    @property
    def last_error(self) -> str | None:
        return self._error

    def records_written(self) -> int:
        with self._count_lock:
            return self._written

    def start(self, path: Path, *, started_at_us: int | None = None, app_version: str | None = None, host: str | None = None, initial_rows: Sequence[Any] | Iterable[Any] = ()) -> Path:
        self.stop(end_session=True)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        started = int(started_at_us if started_at_us is not None else wall_us_now())
        self._path = path
        self._started_at_us = started
        self._app_version = app_version
        self._host = host
        self._written = 0
        self._pending_idle = None
        self._error = None
        self._stop.clear()
        self._queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        initial_events = self._prepare_initial_events(initial_rows)
        self._thread = threading.Thread(
            target=self._run,
            name="iograph-sqlite-stream",
            args=(path, started, app_version, host, initial_events),
            daemon=True,
        )
        try:
            self._thread.start()
        except Exception:
            self._active = False
            self._thread = None
            self._queue = None
            self._path = None
            raise
        self._active = True
        return path

    def accept(self, x: float | None, y: float | None, t_wall_us: int, t_mono_us: int, *, appended: bool) -> None:
        if not self._active:
            return
        if x is None or y is None:
            self._pending_idle = (int(t_wall_us), int(t_mono_us))
            return
        if self._pending_idle is not None:
            idle_wall, idle_mono = self._pending_idle
            self._enqueue(("idle", idle_wall, idle_mono))
            self._pending_idle = None
        self._enqueue(("sample", int(round(x)), int(round(y)), int(t_wall_us), int(t_mono_us)))

    def flush_pending_idle(self) -> None:
        if not self._active:
            return
        if self._pending_idle is None:
            return
        idle_wall, idle_mono = self._pending_idle
        self._enqueue(("idle", idle_wall, idle_mono))
        self._pending_idle = None

    def sync(self, timeout: float = 2.0) -> None:
        """Flush pending idle and wait until queued writes are committed."""
        if not self._active:
            return
        self.flush_pending_idle()
        done = threading.Event()
        self._enqueue(("sync", done))
        done.wait(timeout=timeout)

    def stop(self, timeout: float = 2.0, *, end_session: bool = True) -> None:
        self.flush_pending_idle()
        was_active = self._active
        self._active = False
        self._stop.set()
        queued = self._queue
        if queued is not None:
            try:
                queued.put_nowait(_STOP)
            except queue.Full:
                try:
                    queued.get_nowait()
                    queued.put_nowait(_STOP)
                except (queue.Empty, queue.Full):
                    pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        self._queue = None
        self._pending_idle = None
        if not was_active and not end_session:
            return
        if end_session:
            with self._meta_lock:
                self._session_id = None

    def export_csv_text(self, session_id: int | None = None) -> str:
        self.sync()
        path = self._path
        sid = session_id if session_id is not None else self.session_id
        if path is None or sid is None:
            return _CSV_HEADER
        return export_session_csv_text(path, sid)

    def export_csv_to_path(self, csv_path: Path, session_id: int | None = None) -> int:
        self.sync()
        path = self._path
        sid = session_id if session_id is not None else self.session_id
        if path is None or sid is None:
            Path(csv_path).write_text(_CSV_HEADER, encoding="utf-8")
            return 0
        return export_session_csv_to_path(path, sid, Path(csv_path))

    def _prepare_initial_events(self, initial_rows: Sequence[Any] | Iterable[Any]) -> list[tuple]:
        events: list[tuple] = []
        last_idle: tuple[int, int] | None = None
        for sample in initial_rows:
            if isinstance(sample, dict):
                x = sample.get("x")
                y = sample.get("y")
                t_ms = int(sample.get("t", 0) or 0)
                t_wall = sample.get("t_wall_us")
                t_mono = sample.get("t_mono_us")
                t_wall_us = int(t_wall) if t_wall is not None else wall_us_now()
                t_mono_us = int(t_mono) if t_mono is not None else max(0, t_ms) * 1000
            else:
                x, y, t_ms = sample[0], sample[1], int(sample[2])
                t_wall_us = wall_us_now()
                t_mono_us = max(0, t_ms) * 1000
            if x is None or y is None:
                last_idle = (t_wall_us, t_mono_us)
                continue
            if last_idle is not None:
                events.append(("idle", last_idle[0], last_idle[1]))
                last_idle = None
            events.append(("sample", int(round(float(x))), int(round(float(y))), t_wall_us, t_mono_us))
        ## END for sample in initial_rows....

        if last_idle is not None:
            self._pending_idle = last_idle
        return events

    def _enqueue(self, item: object) -> None:
        queued = self._queue
        if queued is None or not self._active:
            return
        while True:
            try:
                queued.put_nowait(item)
                return
            except queue.Full:
                try:
                    queued.get_nowait()
                except queue.Empty:
                    return

    def _run(self, path: Path, started_at_us: int, app_version: str | None, host: str | None, initial_events: list[tuple]) -> None:
        _set_current_thread_low_priority()
        conn: sqlite3.Connection | None = None
        open_idle_rowid: int | None = None
        last_commit = monotonic()
        written = 0
        try:
            conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA_SQL)
            cur = conn.execute(
                "INSERT INTO sessions (started_at_us, ended_at_us, app_version, host) VALUES (?, NULL, ?, ?)",
                (int(started_at_us), app_version, host),
            )
            session_id = int(cur.lastrowid)
            conn.commit()
            with self._meta_lock:
                self._session_id = session_id

            if initial_events:
                holder: dict[str, int | None] = {"rowid": None}
                written += self._apply_events(conn, session_id, initial_events, holder)
                open_idle_rowid = holder["rowid"]
                conn.commit()
                last_commit = monotonic()
                with self._count_lock:
                    self._written = written

            while not self._stop.is_set():
                batch = self._drain_batch()
                if not batch:
                    now = monotonic()
                    if conn is not None and (now - last_commit) >= _COMMIT_INTERVAL_S:
                        conn.commit()
                        last_commit = now
                    continue
                sync_events: list[threading.Event] = []
                events: list[tuple] = []
                for item in batch:
                    if isinstance(item, tuple) and item and item[0] == "sync":
                        sync_events.append(item[1])
                        continue
                    if isinstance(item, tuple):
                        events.append(item)
                ## END for item in batch....

                if events:
                    holder = {"rowid": open_idle_rowid}
                    written += self._apply_events(conn, session_id, events, holder)
                    open_idle_rowid = holder["rowid"]
                    with self._count_lock:
                        self._written = written
                now = monotonic()
                if sync_events or (now - last_commit) >= _COMMIT_INTERVAL_S:
                    conn.commit()
                    last_commit = now
                for sync_event in sync_events:
                    sync_event.set()
                ## END for sync_event in sync_events....
            ## END while not self._stop.is_set()....

            leftover_batch = self._drain_remaining_raw()
            sync_events = []
            leftover: list[tuple] = []
            for item in leftover_batch:
                if isinstance(item, tuple) and item and item[0] == "sync":
                    sync_events.append(item[1])
                    continue
                if isinstance(item, tuple):
                    leftover.append(item)
            ## END for item in leftover_batch....

            if leftover:
                holder = {"rowid": open_idle_rowid}
                written += self._apply_events(conn, session_id, leftover, holder)
                with self._count_lock:
                    self._written = written
            ended_at = wall_us_now()
            conn.execute("UPDATE sessions SET ended_at_us = ? WHERE session_id = ?", (ended_at, session_id))
            conn.commit()
            for sync_event in sync_events:
                sync_event.set()
            ## END for sync_event in sync_events....

            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                logger.debug("SQLite wal_checkpoint failed", exc_info=True)
        except Exception as exc:
            self._error = str(exc)
            logger.warning("SQLite stream writer failed: %s", exc)
        finally:
            self._active = False
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _apply_events(self, conn: sqlite3.Connection, session_id: int, events: list[tuple], open_idle_rowid_holder: dict[str, int | None]) -> int:
        written = 0
        open_idle_rowid = open_idle_rowid_holder.get("rowid")
        for event in events:
            kind = event[0]
            if kind == "idle":
                _, t_wall_us, t_mono_us = event
                if open_idle_rowid is not None:
                    conn.execute(
                        "UPDATE samples SET t_wall_us = ?, t_mono_us = ? WHERE rowid = ?",
                        (int(t_wall_us), int(t_mono_us), int(open_idle_rowid)),
                    )
                else:
                    cur = conn.execute(
                        "INSERT INTO samples (session_id, t_wall_us, t_mono_us, x, y) VALUES (?, ?, ?, NULL, NULL)",
                        (session_id, int(t_wall_us), int(t_mono_us)),
                    )
                    open_idle_rowid = int(cur.lastrowid)
                    written += 1
                continue
            if kind == "sample":
                _, x, y, t_wall_us, t_mono_us = event
                open_idle_rowid = None
                conn.execute(
                    "INSERT INTO samples (session_id, t_wall_us, t_mono_us, x, y) VALUES (?, ?, ?, ?, ?)",
                    (session_id, int(t_wall_us), int(t_mono_us), int(x), int(y)),
                )
                written += 1
        ## END for event in events....

        open_idle_rowid_holder["rowid"] = open_idle_rowid
        return written

    def _drain_batch(self) -> list[object]:
        queued = self._queue
        if queued is None:
            return []
        try:
            item = queued.get(timeout=0.05)
        except queue.Empty:
            return []
        if item is _STOP:
            return []
        batch = [item]
        while True:
            try:
                item = queued.get_nowait()
            except queue.Empty:
                break
            if item is _STOP:
                break
            batch.append(item)
        return batch

    def _drain_remaining_raw(self) -> list[object]:
        queued = self._queue
        if queued is None:
            return []
        leftover: list[object] = []
        while True:
            try:
                item = queued.get_nowait()
            except queue.Empty:
                break
            if item is _STOP:
                continue
            leftover.append(item)
        return leftover

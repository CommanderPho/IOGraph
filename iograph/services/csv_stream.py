"""Background CSV stream writer for live mouse samples."""
from __future__ import annotations

from datetime import datetime
import ctypes
import logging
import os
from pathlib import Path
import queue
import sys
import threading
from time import monotonic
from typing import Any, Iterable, Sequence, TextIO

logger = logging.getLogger(__name__)

_STOP = object()
_QUEUE_MAXSIZE = 4096
_FSYNC_INTERVAL_S = 1.0
_THREAD_PRIORITY_LOWEST = -2
_CSV_HEADER = "x,y,time\n"


def format_csv_row(x: float | None, y: float | None, t: int) -> str:
    if x is None or y is None:
        return f",,{int(t)}\n"
    return f"{int(round(x))},{int(round(y))},{int(t)}\n"


def sample_to_row(sample: Any) -> tuple[float | None, float | None, int]:
    if isinstance(sample, dict):
        x = sample.get("x")
        y = sample.get("y")
        t_raw = sample.get("t", 0)
    else:
        x, y, t_raw = sample
    try:
        t = int(t_raw)
    except (TypeError, ValueError):
        t = 0
    if x is None or y is None:
        return (None, None, t)
    try:
        return (float(x), float(y), t)
    except (TypeError, ValueError):
        return (None, None, t)


def unique_csv_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix or ".csv"
    parent = path.parent
    for index in range(1, 10000):
        candidate = parent / f"{stem}_{index:03d}{suffix}"
        if not candidate.exists():
            return candidate
    ## END for index in range(1, 10000)....

    raise OSError(f"Unable to allocate unique CSV path near {path}")


def stream_csv_path(directory: Path, started_at: datetime | None = None) -> Path:
    stamp = (started_at or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
    return unique_csv_path(directory / f"IOGraphica - {stamp}.csv")


def _set_current_thread_low_priority() -> None:
    try:
        if sys.platform.startswith("win"):
            kernel32 = ctypes.windll.kernel32
            kernel32.SetThreadPriority(kernel32.GetCurrentThread(), _THREAD_PRIORITY_LOWEST)
            return
        os.nice(10)
    except Exception:
        logger.debug("Unable to lower CSV writer thread priority", exc_info=True)


class CsvStreamWriter:
    """Append compact x,y,time rows on a low-priority background thread."""

    def __init__(self) -> None:
        self._queue: queue.Queue[object] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._io_lock = threading.Lock()
        self._count_lock = threading.Lock()
        self._file: TextIO | None = None
        self._path: Path | None = None
        self._written = 0
        self._pending_idle_t: int | None = None
        self._active = False
        self._error: str | None = None
        self._last_fsync = 0.0

    @property
    def is_open(self) -> bool:
        return self._active

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def last_error(self) -> str | None:
        return self._error

    def records_written(self) -> int:
        with self._count_lock:
            return self._written

    def start(self, path: Path, initial_rows: Sequence[Any] | Iterable[Any] = ()) -> Path:
        self.stop()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("w", encoding="utf-8", newline="\n")
        try:
            handle.write(_CSV_HEADER)
            handle.flush()
        except Exception:
            handle.close()
            raise

        initial_lines, pending_idle_t = self._prepare_initial_lines(initial_rows)
        self._file = handle
        self._path = path
        self._written = 0
        self._pending_idle_t = pending_idle_t
        self._error = None
        self._stop.clear()
        self._queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._last_fsync = monotonic()
        self._thread = threading.Thread(
            target=self._run,
            name="iograph-csv-stream",
            args=(initial_lines,),
            daemon=True,
        )
        try:
            self._thread.start()
        except Exception:
            self._active = False
            self._close_file()
            self._thread = None
            self._queue = None
            raise
        self._active = True
        return path

    def accept(self, x: float | None, y: float | None, t: int, *, appended: bool) -> None:
        if not self._active:
            return
        if x is None or y is None:
            self._pending_idle_t = int(t)
            return
        if self._pending_idle_t is not None:
            self._enqueue(format_csv_row(None, None, self._pending_idle_t))
            self._pending_idle_t = None
        self._enqueue(format_csv_row(x, y, t))

    def flush_pending_idle(self) -> None:
        if not self._active:
            return
        if self._pending_idle_t is None:
            return
        self._enqueue(format_csv_row(None, None, self._pending_idle_t))
        self._pending_idle_t = None

    def stop(self, timeout: float = 2.0) -> None:
        self.flush_pending_idle()
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
        self._close_file()
        self._thread = None
        self._queue = None
        self._pending_idle_t = None

    def _prepare_initial_lines(self, initial_rows: Sequence[Any] | Iterable[Any]) -> tuple[list[str], int | None]:
        lines: list[str] = []
        last_idle_t: int | None = None
        for sample in initial_rows:
            x, y, t = sample_to_row(sample)
            if x is None or y is None:
                last_idle_t = t
                continue
            if last_idle_t is not None:
                lines.append(format_csv_row(None, None, last_idle_t))
                last_idle_t = None
            lines.append(format_csv_row(x, y, t))
        ## END for sample in initial_rows....

        return lines, last_idle_t

    def _enqueue(self, line: str) -> None:
        queued = self._queue
        if queued is None or not self._active:
            return
        while True:
            try:
                queued.put_nowait(line)
                return
            except queue.Full:
                try:
                    queued.get_nowait()
                except queue.Empty:
                    return

    def _run(self, initial_lines: list[str]) -> None:
        _set_current_thread_low_priority()
        try:
            if initial_lines:
                self._write_lines(initial_lines, fsync=True)
            while not self._stop.is_set():
                batch = self._drain_batch()
                if not batch:
                    continue
                self._write_lines(batch)
            leftover = self._drain_remaining()
            if leftover:
                self._write_lines(leftover, fsync=True)
        except Exception as exc:
            self._error = str(exc)
            logger.warning("CSV stream writer failed: %s", exc)
        finally:
            self._active = False
            self._close_file()

    def _drain_batch(self) -> list[str]:
        queued = self._queue
        if queued is None:
            return []
        try:
            item = queued.get(timeout=0.05)
        except queue.Empty:
            return []
        if item is _STOP:
            return []
        batch = [item] if isinstance(item, str) else []
        while True:
            try:
                item = queued.get_nowait()
            except queue.Empty:
                break
            if item is _STOP:
                break
            if isinstance(item, str):
                batch.append(item)
        return batch

    def _drain_remaining(self) -> list[str]:
        queued = self._queue
        if queued is None:
            return []
        leftover: list[str] = []
        while True:
            try:
                item = queued.get_nowait()
            except queue.Empty:
                break
            if item is _STOP:
                continue
            if isinstance(item, str):
                leftover.append(item)
        return leftover

    def _write_lines(self, lines: list[str], *, fsync: bool = False) -> None:
        if not lines:
            return
        with self._io_lock:
            handle = self._file
            if handle is None:
                return
            for line in lines:
                handle.write(line)
            ## END for line in lines....

            handle.flush()
            with self._count_lock:
                self._written += len(lines)
            now = monotonic()
            if fsync or (now - self._last_fsync) >= _FSYNC_INTERVAL_S:
                os.fsync(handle.fileno())
                self._last_fsync = now

    def _close_file(self) -> None:
        with self._io_lock:
            handle = self._file
            self._file = None
            if handle is None:
                return
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except Exception:
                pass
            try:
                handle.close()
            except Exception:
                pass

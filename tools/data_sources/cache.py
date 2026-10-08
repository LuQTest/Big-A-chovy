"""Bounded in-memory and atomic JSON cache helpers.

New cache files follow ``A_SHARE_STATE_DIR`` when set.  With no override they
live under the historical scripts directory and remain ignored by the project.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any
import uuid
from contextlib import contextmanager
from functools import wraps

try:
    import fcntl
except ImportError:  # pragma: no cover - platform-specific import
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - platform-specific import
    msvcrt = None


def _msvcrt_lock_available() -> bool:
    return msvcrt is not None and all(
        getattr(msvcrt, name, None) is not None
        for name in ("locking", "LK_LOCK", "LK_UNLCK")
    )


def _prepare_msvcrt_lock_file(handle) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() < 1:
        handle.seek(0)
        handle.write(b"\0")
        handle.flush()
        os.fsync(handle.fileno())
    handle.seek(0)


def cache_root() -> Path:
    override = os.environ.get("A_SHARE_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2] / "daily-stock-analysis" / "scripts"


def cache_path(namespace: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in namespace).strip("_") or "data"
    return cache_root() / f".a_share_data_{safe}.json"


def atomic_write_json(path: Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


@dataclass
class CacheEntry:
    value: Any
    stored_at: float
    expires_at: float | None = None
    data_date: str | None = None


_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: dict[str, threading.Event] = {}


@contextmanager
def coalesced_request(key: str):
    """Let concurrent non-forced calls share one cache-filling request."""
    with _INFLIGHT_LOCK:
        event = _INFLIGHT.get(key)
        owner = event is None
        if owner:
            event = threading.Event()
            _INFLIGHT[key] = event
    if not owner:
        event.wait()
    try:
        yield owner
    finally:
        if owner:
            with _INFLIGHT_LOCK:
                _INFLIGHT.pop(key, None)
                event.set()


def coalesced_fetch(namespace: str):
    """Decorator for source fetches whose normal path already checks cache."""
    def decorator(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if kwargs.get("force"):
                return function(*args, **kwargs)
            payload = {"args": [str(value) for value in args[1:]], "kwargs": {str(key): str(value) for key, value in sorted(kwargs.items())}}
            key = f"{namespace}:{JsonCache.key(payload)}"
            with coalesced_request(key):
                return function(*args, **kwargs)
        return wrapped
    return decorator


def result_cache_value(result: Any) -> dict[str, Any]:
    """Persist the complete Result envelope, not only its data payload."""
    return {"_result": result.to_dict()}


def result_from_cache(value: Any, *, default_source: str, default_source_url: str, cache_hit: bool = True) -> Any | None:
    """Restore a cached Result; legacy data-only entries are deliberately ignored."""
    if not isinstance(value, dict) or not isinstance(value.get("_result"), dict):
        return None
    payload = value["_result"]
    status = payload.get("status")
    if not isinstance(status, str):
        return None
    try:
        from .contracts import Result

        return Result(
            status=status,
            data=payload.get("data"),
            source=str(payload.get("source") or default_source),
            source_url=str(payload.get("source_url") or default_source_url),
            fetched_at=str(payload.get("fetched_at") or ""),
            data_date=payload.get("data_date"),
            as_of=payload.get("as_of"),
            freshness="cached",
            warnings=list(payload.get("warnings") or []),
            error=payload.get("error"),
            schema_version=str(payload.get("schema_version") or "a-share-data.v1"),
            cache={**(payload.get("cache") or {}), "hit": cache_hit},
            request_count=int(payload.get("request_count") or 0),
        )
    except (TypeError, ValueError, KeyError):
        return None


class JsonCache:
    """A tiny process-safe JSON cache with atomic writes and stale visibility."""

    def __init__(self, namespace: str, *, path: Path | None = None):
        self.path = path or cache_path(namespace)
        self._lock = threading.RLock()
        self._loaded = False
        self._payload: dict[str, Any] = {}

    def _read_disk(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except (OSError, ValueError, TypeError):
            pass
        return {}

    def _load(self) -> None:
        if not self._loaded:
            self._payload = self._read_disk()
            self._loaded = True

    @contextmanager
    def _file_lock(self, *, exclusive: bool):
        """Coordinate read-modify-write across cache instances/processes."""
        if fcntl is None and not _msvcrt_lock_available():
            raise RuntimeError("当前平台没有可用的跨进程文件锁，拒绝访问共享 JSON 缓存")
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return

            # Windows has no shared byte-range lock, so serialize reads too;
            # the cache is tiny and correctness is more important than this
            # distinction.
            _prepare_msvcrt_lock_file(handle)
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as exc:
                raise RuntimeError("无法获取 Windows 缓存跨进程文件锁") from exc
            try:
                yield
            finally:
                pending_error = sys.exc_info()
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError as exc:
                    if pending_error[0] is None:
                        raise RuntimeError("无法释放 Windows 缓存跨进程文件锁") from exc

    @staticmethod
    def key(value: Any) -> str:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str, *, ttl: float | None = None, now: float | None = None, allow_stale: bool = False) -> CacheEntry | None:
        with self._lock:
            with self._file_lock(exclusive=False):
                # Another JsonCache instance/process may have published a
                # newer key since this object was constructed.
                self._payload = self._read_disk()
                self._loaded = True
            entry = self._payload.get(key)
            if not isinstance(entry, dict) or "value" not in entry:
                return None
            stored = float(entry.get("stored_at") or 0)
            expires = entry.get("expires_at")
            if ttl is not None:
                expires = stored + float(ttl)
            current = time.time() if now is None else float(now)
            fresh = expires is None or current <= float(expires)
            if not fresh and not allow_stale:
                return None
            return CacheEntry(entry.get("value"), stored, float(expires) if expires is not None else None, entry.get("data_date"))

    def set(self, key: str, value: Any, *, ttl: float | None = None, data_date: str | None = None) -> None:
        with self._lock:
            with self._file_lock(exclusive=True):
                # Read while holding the same lock as the eventual replace so
                # concurrent instances cannot lose each other's keys.
                self._payload = self._read_disk()
                self._loaded = True
                stored = time.time()
                self._payload[key] = {
                    "value": value,
                    "stored_at": stored,
                    "expires_at": stored + float(ttl) if ttl is not None else None,
                    "data_date": data_date,
                }
                atomic_write_json(self.path, self._payload)

    def delete(self, key: str) -> None:
        with self._lock:
            with self._file_lock(exclusive=True):
                self._payload = self._read_disk()
                self._loaded = True
                if key in self._payload:
                    self._payload.pop(key, None)
                    atomic_write_json(self.path, self._payload)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            with self._file_lock(exclusive=False):
                self._payload = self._read_disk()
                self._loaded = True
            return {"path": str(self.path), "entries": len(self._payload)}

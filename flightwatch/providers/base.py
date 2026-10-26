"""数据源接口与公共工具（限速、磁盘缓存）。"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from typing import Any, Optional, Protocol

from ..models import Leg


def _cache_dir() -> str:
    """挑一个可写的缓存目录。

    - 打包版（PyInstaller）不能写 _MEIPASS，优先用户数据目录，其次临时目录；
    - 普通运行写在仓库旁边的 data/cache；
    - 任何一个候选目录不可写就自动降级，最后兜底到临时目录。
    可用 FLIGHTWATCH_DATA_DIR 强制指定。
    """
    candidates: list[str] = []
    env = os.environ.get("FLIGHTWATCH_DATA_DIR")
    if env:
        candidates.append(os.path.join(env, "cache"))
    if getattr(sys, "frozen", False):
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(os.path.join(local, "flightwatch", "cache"))
        candidates.append(os.path.join(tempfile.gettempdir(), "flightwatch", "cache"))
    else:
        candidates.append(
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "cache")
        )

    for path in candidates:
        try:
            path = os.path.abspath(path)          # 万一是相对路径，先转绝对
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".write_probe")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
            return path
        except OSError:
            continue
    return os.path.abspath(tempfile.mkdtemp(prefix="flightwatch-cache-"))


CACHE_DIR = _cache_dir()


class Provider(Protocol):
    name: str
    live: bool

    def legs(self, origin: str, dest: str, date: str, cabin: str) -> list[Leg]:
        ...


class RateLimiter:
    """简单的匀速限速器：保证两次请求之间至少间隔 min_interval 秒。"""

    def __init__(self, min_interval: float = 0.8):
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last = time.monotonic()


class DiskCache:
    """按 key 缓存 JSON，带 TTL。默认 30 分钟，避免重复请求外部接口。"""

    def __init__(self, namespace: str, ttl_seconds: float = 1800, cache_dir: Optional[str] = None):
        self.dir = os.path.join(cache_dir or CACHE_DIR, namespace)
        os.makedirs(self.dir, exist_ok=True)
        self.ttl = ttl_seconds

    def _path(self, key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        return os.path.join(self.dir, digest + ".json")

    def get(self, key: str) -> Optional[Any]:
        path = self._path(key)
        try:
            if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < self.ttl:
                with open(path, encoding="utf-8") as f:
                    payload = json.load(f)
                return payload.get("value")
        except (OSError, ValueError):
            return None
        return None

    def set(self, key: str, value: Any) -> None:
        path = self._path(key)
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"value": value, "ts": time.time()}, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            pass

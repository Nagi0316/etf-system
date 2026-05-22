"""
cache.py — 執行緒安全記憶體快取（替代 Redis，零外部依賴）
"""
import time, threading
from typing import Any, Optional


class MemCache:
    def __init__(self):
        self._d: dict = {}
        self._lk = threading.RLock()

    def get(self, k: str) -> Optional[Any]:
        with self._lk:
            e = self._d.get(k)
            if not e:
                return None
            if time.time() > e[1]:
                del self._d[k]
                return None
            return e[0]

    def set(self, k: str, v: Any, ttl: int = 180):
        with self._lk:
            self._d[k] = (v, time.time() + ttl)

    def delete(self, k: str):
        with self._lk:
            self._d.pop(k, None)

    def delete_prefix(self, prefix: str):
        with self._lk:
            for k in [k for k in self._d if k.startswith(prefix)]:
                del self._d[k]

    def evict(self):
        now = time.time()
        with self._lk:
            stale = [k for k, v in self._d.items() if now > v[1]]
            for k in stale:
                del self._d[k]


cache = MemCache()

CACHE_TTL_RANK   = 180    # 排行榜 3 分鐘
CACHE_TTL_DETAIL = 600    # 詳情 10 分鐘
CACHE_TTL_SEARCH = 60     # 搜尋 1 分鐘
CACHE_TTL_FX     = 300    # 匯率 5 分鐘

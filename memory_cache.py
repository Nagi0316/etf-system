"""容量受控、執行緒安全的程序內記憶體快取。"""

import threading
import time
from typing import Any, Optional


class MemCache:
    def __init__(self, max_entries: int = 4096):
        self._d: dict = {}
        self._lk = threading.RLock()
        self._max_entries = max(1, int(max_entries))

    def _evict_expired_locked(self, now: float) -> None:
        stale = [key for key, value in self._d.items() if now > value[1]]
        for key in stale:
            del self._d[key]

    def get(self, k: str) -> Optional[Any]:
        with self._lk:
            e = self._d.get(k)
            if not e:
                return None
            if time.monotonic() > e[1]:
                del self._d[k]
                return None
            return e[0]

    def set(self, k: str, v: Any, ttl: int = 180) -> None:
        if ttl <= 0:
            self.delete(k)
            return

        now = time.monotonic()
        with self._lk:
            if k not in self._d and len(self._d) >= self._max_entries:
                self._evict_expired_locked(now)
                if len(self._d) >= self._max_entries:
                    # 優先移除最快過期的項目，避免攻擊者用大量 IP / 查詢鍵
                    # 讓程序內快取無限制成長。
                    soonest_expiring = min(
                        self._d,
                        key=lambda key: self._d[key][1],
                    )
                    del self._d[soonest_expiring]
            self._d[k] = (v, now + ttl)

    def delete(self, k: str) -> None:
        with self._lk:
            self._d.pop(k, None)

    def delete_prefix(self, prefix: str) -> None:
        with self._lk:
            for k in [k for k in self._d if k.startswith(prefix)]:
                del self._d[k]

    def evict(self) -> None:
        now = time.monotonic()
        with self._lk:
            self._evict_expired_locked(now)

    def __len__(self) -> int:
        with self._lk:
            return len(self._d)


cache = MemCache()

CACHE_TTL_RANK   = 600    # 排行榜 10 分鐘（原 3 分鐘，頻繁 miss 反而增加 DB 壓力）
CACHE_TTL_DETAIL = 600    # 詳情 10 分鐘
CACHE_TTL_SEARCH = 60     # 搜尋 1 分鐘
CACHE_TTL_FX     = 300    # 匯率 5 分鐘

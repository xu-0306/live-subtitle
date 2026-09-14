from __future__ import annotations

from collections import OrderedDict
from typing import Dict, Generic, Optional, Tuple, TypeVar
import threading

K = TypeVar("K")
V = TypeVar("V")


class LRUCache(Generic[K, V]):
    def __init__(self, max_size: int) -> None:
        self.max_size = max(0, int(max_size))
        self._data: Dict[K, V] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: K) -> Tuple[Optional[V], bool]:
        if self.max_size <= 0:
            return None, False
        with self._lock:
            if key not in self._data:
                return None, False
            value = self._data.pop(key)
            self._data[key] = value
            return value, True

    def set(self, key: K, value: V) -> Optional[V]:
        if self.max_size <= 0:
            return None
        evicted: Optional[V] = None
        with self._lock:
            if key in self._data:
                evicted = self._data.pop(key)
            self._data[key] = value
            while len(self._data) > self.max_size:
                _, evicted = self._data.popitem(last=False)
        return evicted

    def values(self) -> list[V]:
        with self._lock:
            return list(self._data.values())

    def clear(self) -> list[V]:
        with self._lock:
            values = list(self._data.values())
            self._data.clear()
            return values

    def __len__(self) -> int:
        return len(self._data)

"""工具文本投影缓存，每个 tier 独立限制条数与字符串驻留字节。"""

from collections import OrderedDict
from sys import getsizeof
from threading import Lock


class TextTransformMemo:
    def __init__(self, max_entries: int, max_bytes: int):
        self._entries: OrderedDict[int, tuple[str, str, int]] = OrderedDict()
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._used_bytes = 0
        self._lock = Lock()

    def get(self, text: str) -> str | None:
        key = id(text)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry[0] is not text:
                return None
            self._entries.move_to_end(key)
            return entry[1]

    def put(self, text: str, result: str) -> str:
        size = getsizeof(text)
        if result is not text:
            size += getsizeof(result)
        if size <= self._max_bytes:
            key = id(text)
            with self._lock:
                previous = self._entries.pop(key, None)
                if previous is not None:
                    self._used_bytes -= previous[2]
                self._entries[key] = (text, result, size)
                self._used_bytes += size
                while len(self._entries) > self._max_entries or self._used_bytes > self._max_bytes:
                    _, evicted = self._entries.popitem(last=False)
                    self._used_bytes -= evicted[2]
        return result

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._used_bytes = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

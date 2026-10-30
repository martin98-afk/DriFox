# -*- coding: utf-8 -*-
"""同步元数据记账器：把配置写入翻译成 LWW 条目表。

与 UI 解耦：只接收「某个配置项路径从旧值变成新值」，不依赖 Qt 信号。
真实接线见 ``bind_settings``（对 Settings 的全部 ConfigItem 挂 valueChanged）。

集合型配置项（见 lww.COLLECTION_KEYS）做前后 diff：新增条目打 t、
消失条目写墓碑、值变化的条目打 t。未登记的配置项整块作为一个条目。
"""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from typing import Dict, List, Optional

from app.core.sync.lww import (
    COLLECTION_KEYS,
    Entry,
    LamportClock,
    element_key,
    is_tombstone,
    live,
    tombstone,
)

META_VERSION = 1

# 同步元数据自身存放的配置项路径：不得进入条目表
META_ITEM_PATHS = frozenset({"System.SyncMeta"})


def value_hash(value: object) -> str:
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        raw = repr(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SyncMetaRecorder:
    def __init__(self) -> None:
        self.state: Dict[str, object] = {"version": META_VERSION, "clock": 0, "entries": {}}
        # path（group.name）→ 该配置项当前值，用于 diff 出集合条目的增删
        self.values: Dict[str, object] = {}
        self._clock = LamportClock(0)
        self._lock = threading.RLock()
        self._suppress_depth = 0

    # ── 生命周期 ─────────────────────────────────────────

    def attach(self, config: Dict[str, object], state: object) -> None:
        """装载现状：config 是 app.config 结构，state 是已存的元数据（JSON 串或 dict）。

        不自动补条目：补 t 的时机由调用方决定（见 ``seed_missing``）。合并场景下
        必须先 ``observe_clock(远端 clock)`` 再 seed，否则本地补出来的 t 会低于
        远端已有序号，导致「首次升级时远端全胜、本地独有改动被覆盖」。
        """
        with self._lock:
            self.values = _item_values(config)
            loaded = load_state(state)
            self.state = {
                "version": META_VERSION,
                "clock": int(loaded.get("clock") or 0),
                "entries": dict(loaded.get("entries") or {}),
            }
            self._clock = LamportClock(int(self.state["clock"] or 0))
            for entry in self._entries().values():
                self._clock.observe(entry.get("t"))

    def observe_clock(self, value: object) -> None:
        """把外部（远端）的 Lamport 计数纳入本机时钟，保证后续 tick 高于它。"""
        with self._lock:
            self._clock.observe(value)
            self._sync_clock()

    def seed_missing(self) -> None:
        """为还没有元数据的条目补 t（首次升级视为当前时刻的新增）。

        已存在元数据的条目一律不动，否则每次启动都会把 ts 顶到最新，
        导致其他设备的真实修改永远输给本机「什么都没改但 ts 更新」。
        """
        with self._lock:
            entries = self._entries()
            for path, value in self.values.items():
                kind = COLLECTION_KEYS.get(path)
                if kind == "dict":
                    for k, v in (value.items() if isinstance(value, dict) else []):
                        epath = f"{path}.{k}"
                        if epath not in entries:
                            entries[epath] = live(value_hash(v), self._clock.tick())
                    continue
                if kind == "list":
                    for elem in value if isinstance(value, list) else []:
                        epath = f"{path}.{element_key(elem)}"
                        if epath not in entries:
                            entries[epath] = live(value_hash(elem), self._clock.tick())
                    continue
                if path not in entries:
                    entries[path] = live(value_hash(value), self._clock.tick())
            self.state["entries"] = entries
            self._sync_clock()

    @contextmanager
    def suppress(self):
        """抑制记账（下载合并写回内存时必须用，否则会把远端改动记成本机修改）。"""
        with self._lock:
            self._suppress_depth += 1
        try:
            yield
        finally:
            with self._lock:
                self._suppress_depth -= 1

    def begin_suppress(self) -> None:
        with self._lock:
            self._suppress_depth += 1

    def end_suppress(self) -> None:
        with self._lock:
            if self._suppress_depth > 0:
                self._suppress_depth -= 1

    def reset_suppress(self) -> None:
        """异常兜底：抑制计数归零，防止一次异常后记账器永久静默。"""
        with self._lock:
            self._suppress_depth = 0

    # ── 记录 ─────────────────────────────────────────────

    def on_item_changed(self, path: str, old: object, new: object) -> None:
        if self._suppress_depth:
            return
        with self._lock:
            kind = COLLECTION_KEYS.get(path)
            if kind == "dict":
                self._diff_dict(path, old, new)
            elif kind == "list":
                self._diff_list(path, old, new)
            else:
                self._touch(path, new)
            self.values[path] = new
            self._sync_clock()

    def _touch(self, path: str, value: object) -> None:
        self._entries()[path] = live(value_hash(value), self._clock.tick())

    def _diff_dict(self, path: str, old: object, new: object) -> None:
        old_d = old if isinstance(old, dict) else {}
        new_d = new if isinstance(new, dict) else {}
        for k, v in new_d.items():
            if old_d.get(k) != v:
                self._touch(f"{path}.{k}", v)
        for k, v in old_d.items():
            if k not in new_d:
                self._entries()[f"{path}.{k}"] = tombstone(
                    value_hash(v), self._clock.tick()
                )

    def _diff_list(self, path: str, old: object, new: object) -> None:
        old_l: List[object] = list(old) if isinstance(old, list) else []
        new_l: List[object] = list(new) if isinstance(new, list) else []
        old_map = {element_key(e): e for e in old_l}
        new_map = {element_key(e): e for e in new_l}
        for k, v in new_map.items():
            if old_map.get(k) != v:
                self._touch(f"{path}.{k}", v)
        for k, v in old_map.items():
            if k not in new_map:
                self._entries()[f"{path}.{k}"] = tombstone(
                    value_hash(v), self._clock.tick()
                )

    def _sync_clock(self) -> None:
        self.state["clock"] = self._clock.value

    # ── 序列化 ───────────────────────────────────────────

    def _entries(self) -> Dict[str, Entry]:
        entries = self.state.get("entries")
        if not isinstance(entries, dict):
            entries = {}
            self.state["entries"] = entries
        return entries  # type: ignore[return-value]

    def entries(self) -> Dict[str, Entry]:
        with self._lock:
            return {
                str(k): dict(v)
                for k, v in self._entries().items()
                if isinstance(v, dict)
            }

    def dump(self) -> str:
        with self._lock:
            self._sync_clock()
            return json.dumps(self.state, ensure_ascii=False, sort_keys=True)

    def replace_entries(self, entries: Dict[str, Entry], clock: int) -> None:
        """合并完成后回写条目表（调用方负责给出合并后的 clock）。"""
        with self._lock:
            self.state["entries"] = {str(k): dict(v) for k, v in entries.items()}
            self._clock = LamportClock(max(int(clock or 0), self._clock.value))
            self._sync_clock()


def _item_values(config: object) -> Dict[str, object]:
    out: Dict[str, object] = {}
    if not isinstance(config, dict):
        return out
    for group, items in config.items():
        if not isinstance(items, dict):
            continue
        for name, value in items.items():
            path = f"{group}.{name}"
            if path in META_ITEM_PATHS:
                # 元数据自身不进条目表：否则每次 dump 都改变自身内容 →
                # 下次 seed 发现 hash 变了 → 打新 t → 又改变内容，无限自激
                continue
            out[path] = value
    return out


def load_state(raw: object) -> Dict[str, object]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    entries = raw.get("entries")
    return {
        "version": META_VERSION,
        "clock": int(raw.get("clock") or 0),
        "entries": entries if isinstance(entries, dict) else {},
    }


def merge_clock(local: object, remote: object) -> int:
    """合并两份元数据的 Lamport 计数：取大者。"""
    try:
        a = int((load_state(local) or {}).get("clock") or 0)
    except Exception:
        a = 0
    try:
        b = int((load_state(remote) or {}).get("clock") or 0)
    except Exception:
        b = 0
    return max(a, b)


def is_tombstone_entry(entry: Optional[Entry]) -> bool:
    return is_tombstone(entry)


# ── Settings 接线 ───────────────────────────────────────

_recorder_singleton: Optional[SyncMetaRecorder] = None


def bind_settings(cfg) -> SyncMetaRecorder:
    """把记账器接到 Settings 的全部 ConfigItem 上（幂等，全局单例）。

    只负责 app.config 内存侧的变化采集；合并下载写回内存时由调用方
    ``begin_suppress`` / ``end_suppress`` 抑制，避免把远端内容记成本机改动。
    """
    global _recorder_singleton
    if _recorder_singleton is not None:
        return _recorder_singleton

    from qfluentwidgets.common.config import ConfigItem

    rec = SyncMetaRecorder()
    rec.attach(cfg.toDict(serialize=False), getattr(cfg, "sync_meta").value)
    rec.seed_missing()

    for name in dir(type(cfg)):
        item = getattr(type(cfg), name)
        if not isinstance(item, ConfigItem) or item is getattr(cfg, "sync_meta", None):
            continue
        item.valueChanged.connect(_make_handler(rec, item))

    _recorder_singleton = rec
    return rec


def _make_handler(rec: SyncMetaRecorder, item) -> object:
    def _handler(_new):
        try:
            path = item.key
            rec.on_item_changed(path, rec.values.get(path), item.value)
        except Exception:
            pass

    # 保持引用：Qt 信号连接不持有 Python 侧强引用时可能被回收
    item._drifox_sync_handler = _handler
    return _handler


def reset_recorder_singleton() -> None:
    """测试用：清空全局单例（生产路径不调用）。"""
    global _recorder_singleton
    _recorder_singleton = None

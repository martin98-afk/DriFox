# -*- coding: utf-8 -*-
"""云同步的 LWW（Last Write Wins）+ 墓碑合并。

每个同步条目带一份元数据 ``{"h": 内容指纹, "t": Lamport 计数, "d": 墓碑标记}``。
合并逐条目比 ``t``，大者胜；胜者为墓碑时删除。与设备时钟无关，
也不需要「上一次一致版本」（base）。

设计取舍：
- 删除是显式数据（墓碑），不靠「缺失」推断，所以换机、清缓存后仍能传播。
- 不认识的条目（老版本客户端不认识的新配置项）缺失即视为本地新增，
  最坏结果是多保留一条，不会误删。
"""

from __future__ import annotations

import json
from typing import Dict, List, Tuple

Entry = Dict[str, object]

TOMBSTONE_KEY = "d"


class LamportClock:
    """逻辑时钟：只保证因果递增，不依赖系统时间。"""

    def __init__(self, start: int = 0) -> None:
        self._t = int(start or 0)

    def observe(self, other: object) -> None:
        try:
            self._t = max(self._t, int(other or 0))
        except (TypeError, ValueError):
            pass

    def tick(self) -> int:
        self._t += 1
        return self._t

    @property
    def value(self) -> int:
        return self._t


def entry_ts(entry: Entry | None) -> int:
    """条目时间戳（缺失/非法返回 0 或 -1，供合并与取值共用）。"""
    if not isinstance(entry, dict):
        return -1
    try:
        return int(entry.get("t") or 0)
    except (TypeError, ValueError):
        return 0


_t = entry_ts


def _is_tombstone(entry: Entry | None) -> bool:
    return isinstance(entry, dict) and bool(entry.get(TOMBSTONE_KEY))


def merge_entries(
    local: Dict[str, Entry],
    remote: Dict[str, Entry],
) -> Tuple[Dict[str, Entry], List[str]]:
    """逐条目 LWW 合并，返回 (合并结果, 冲突条目路径)。

    冲突判定：t 相同但 h 不同。此时本地胜出，远端内容由调用方留档。
    """
    merged: Dict[str, Entry] = {}
    conflicts: List[str] = []
    for key in set(local) | set(remote):
        le = local.get(key)
        re_ = remote.get(key)
        if le is None:
            merged[key] = dict(re_ or {})
            continue
        if re_ is None:
            merged[key] = dict(le)
            continue
        lt, rt = _t(le), _t(re_)
        if lt > rt:
            merged[key] = dict(le)
        elif rt > lt:
            merged[key] = dict(re_)
        else:
            if str(le.get("h") or "") != str(re_.get("h") or ""):
                conflicts.append(key)
            merged[key] = dict(le)
    return merged, conflicts


def tombstone(h: str, t: int) -> Entry:
    return {"h": h, "t": t, TOMBSTONE_KEY: True}


def live(h: str, t: int) -> Entry:
    return {"h": h, "t": t}


def is_tombstone(entry: Entry | None) -> bool:
    return _is_tombstone(entry)


# ── config 结构合并 ──────────────────────────────────────
#
# 集合型配置项显式登记，不做递归深度猜测。新增集合型配置时需要在这里加一行，
# 未登记的一律整块作为一个条目（最坏情况是一次整体覆盖，不会静默误删）。
COLLECTION_KEYS: Dict[str, str] = {
    "LLM.SavedProviders": "dict",
    "LLM.ModelOverrides": "dict",
    "Tools.Toggles": "dict",
    "Tools.PermissionPolicy": "dict",
    "MCP.Servers": "list",
    "Plugin.EnabledPlugins": "list",
    "Plugin.DisabledPlugins": "list",
    "Plugin.DisabledComponents": "list",
    "LLM.EnabledSkills": "list",
    "Plugin.ConfirmedPluginServers": "list",
    "Marketplace.AllowedGitHosts": "list",
    "Hooks.SafePythonModules": "list",
    "QFluentWidgets.FontFamilies": "list",
}

_ELEM_ID_FIELDS = ("id", "name", "key", "uuid", "url", "model", "provider")

_MISSING = object()


def element_key(value: object) -> str:
    """list 元素身份：优先语义字段，否则取整体序列化。"""
    if isinstance(value, dict):
        for name in _ELEM_ID_FIELDS:
            v = value.get(name)
            if isinstance(v, (str, int)) and str(v):
                return f"{name}:{v}"
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return repr(value)


def _collection_items(kind: str, value: object) -> Dict[str, object]:
    if kind == "dict":
        return dict(value) if isinstance(value, dict) else {}
    items: Dict[str, object] = {}
    for elem in value if isinstance(value, list) else []:
        items[element_key(elem)] = elem
    return items


def _pick_entry(
    path: str,
    local_entries: Dict[str, Entry],
    remote_entries: Dict[str, Entry],
) -> Tuple[Entry | None, str]:
    """按 t 取胜者，返回 (entry, side)。side ∈ {local, remote, conflict, none}。

    entry 为 None 表示两边都没有元数据（老版本抹掉 / 首次升级），调用方走
    union 降级：本地有取本地，否则取远端。
    """
    le = local_entries.get(path)
    re_ = remote_entries.get(path)
    if le is None and re_ is None:
        return None, "none"
    if le is None:
        return re_, "remote"
    if re_ is None:
        return le, "local"
    lt, rt = entry_ts(le), entry_ts(re_)
    if rt > lt:
        return re_, "remote"
    if lt > rt:
        return le, "local"
    if str(le.get("h") or "") != str(re_.get("h") or ""):
        return le, "conflict"
    return le, "local"


def _merge_collection(
    path: str,
    kind: str,
    local_value: object,
    remote_value: object,
    local_entries: Dict[str, Entry],
    remote_entries: Dict[str, Entry],
    merged_entries: Dict[str, Entry],
    conflicts: List[str],
) -> object:
    l_items = _collection_items(kind, local_value) if local_value is not _MISSING else {}
    r_items = _collection_items(kind, remote_value) if remote_value is not _MISSING else {}
    out_items: Dict[str, object] = {}
    order = list(l_items) + [k for k in r_items if k not in l_items]
    for ek in order:
        epath = f"{path}.{ek}"
        entry, side = _pick_entry(epath, local_entries, remote_entries)
        if entry is not None:
            merged_entries[epath] = entry
            if side == "conflict":
                conflicts.append(epath)
            if _is_tombstone(entry):
                continue
        if side == "remote":
            out_items[ek] = r_items.get(ek, l_items.get(ek))
        else:
            out_items[ek] = l_items.get(ek, r_items.get(ek))
    if kind == "dict":
        return dict(out_items)
    return [v for v in out_items.values()]


def merge_config_lww(
    local_cfg: Dict[str, object],
    local_entries: Dict[str, Entry],
    remote_cfg: Dict[str, object],
    remote_entries: Dict[str, Entry],
) -> Tuple[Dict[str, object], Dict[str, Entry], List[str]]:
    """按条目 LWW 合并两份 app.config，返回 (结果, 条目表, 冲突路径)。

    直接操作嵌套结构，不经过「扁平化再重建」往返，避免路径里含点号时的歧义。
    """
    merged_cfg: Dict[str, object] = {}
    merged_entries: Dict[str, Entry] = {}
    conflicts: List[str] = []

    for group in sorted(set(local_cfg) | set(remote_cfg)):
        lg = local_cfg.get(group, _MISSING)
        rg = remote_cfg.get(group, _MISSING)
        if not isinstance(lg, dict) or not isinstance(rg, dict):
            path = str(group)
            entry, side = _pick_entry(path, local_entries, remote_entries)
            if entry is not None:
                merged_entries[path] = entry
                if side == "conflict":
                    conflicts.append(path)
                if _is_tombstone(entry):
                    continue
            if side == "remote" or (side == "none" and lg is _MISSING):
                if rg is not _MISSING:
                    merged_cfg[group] = rg
            elif lg is not _MISSING:
                merged_cfg[group] = lg
            continue

        g_out: Dict[str, object] = {}
        order = list(lg) + [n for n in rg if n not in lg]
        for name in order:
            path = f"{group}.{name}"
            kind = COLLECTION_KEYS.get(path)
            if kind in ("dict", "list"):
                value = _merge_collection(
                    path,
                    kind,
                    lg.get(name, _MISSING),
                    rg.get(name, _MISSING),
                    local_entries,
                    remote_entries,
                    merged_entries,
                    conflicts,
                )
                g_out[name] = value
                continue
            entry, side = _pick_entry(path, local_entries, remote_entries)
            if entry is not None:
                merged_entries[path] = entry
                if side == "conflict":
                    conflicts.append(path)
                if _is_tombstone(entry):
                    continue
            if side == "remote":
                if name in rg:
                    g_out[name] = rg[name]
                elif name in lg:
                    g_out[name] = lg[name]
            else:
                if name in lg:
                    g_out[name] = lg[name]
                elif name in rg:
                    g_out[name] = rg[name]
        merged_cfg[group] = g_out

    return merged_cfg, merged_entries, conflicts


# ── 目录单元条目对齐 ────────────────────────────────────

DIR_META_NAME = ".__drifox_sync__.json"


def reconcile_dir_entries(
    manifest: Dict[str, str],
    entries: Dict[str, Entry],
    clock: LamportClock,
) -> Dict[str, Entry]:
    """把目录现状（{相对路径: 内容 sha256}）对齐到条目表，返回新表。

    - 文件新增 / 内容变化 → live，打新 t
    - 文件消失且条目仍是 live → 墓碑（保留原内容指纹，对方可据此判断删的是哪一版）
    - 已是墓碑且文件确实不存在 → 保持原 t，不刷新（否则墓碑会被本地空转推新）
    """
    out: Dict[str, Entry] = {str(k): dict(v) for k, v in entries.items() if isinstance(v, dict)}
    for rel, h in manifest.items():
        cur = out.get(rel)
        if cur is None or _is_tombstone(cur) or str(cur.get("h") or "") != str(h):
            out[rel] = live(h, clock.tick())
    for rel, cur in list(out.items()):
        if rel in manifest or _is_tombstone(cur):
            continue
        out[rel] = tombstone(str(cur.get("h") or ""), clock.tick())
    return out


def seed_entries_from_manifest(
    manifest: Dict[str, str],
    entries: Dict[str, Entry],
) -> Dict[str, Entry]:
    """给没有元数据的文件补 t=0 条目（兼容没有包内元数据的老版本 zip）。

    t=0 是最低优先级：对方有正经序号时永远输给对方，只在「独有文件」场景
    被保留，不会拿它去覆盖任何有元数据的内容。
    """
    out: Dict[str, Entry] = {str(k): dict(v) for k, v in entries.items() if isinstance(v, dict)}
    for rel, h in manifest.items():
        if rel not in out:
            out[rel] = {"h": h, "t": 0}
    return out


def dir_meta_payload(entries: Dict[str, Entry], clock: int) -> str:
    return json.dumps(
        {"version": 1, "clock": int(clock or 0), "entries": entries},
        ensure_ascii=False,
        sort_keys=True,
    )


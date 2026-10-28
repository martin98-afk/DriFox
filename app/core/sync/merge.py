# -*- coding: utf-8 -*-
"""云同步的三方合并。

背景：旧实现多设备同步是「整包后写者胜」，任何一方上传即覆盖对方全部内容，
本地未上传的改动在下次下载时被整目录替换并删除，属于真实数据丢失。改为以
「本机最后一次与远端一致的版本」（base）为基准做三方合并：

- 本地未动（local == base）→ 取远端
- 远端未动（remote == base）→ 取本地
- 两边都改且不同 → 冲突，**本地优先**，远端版本另存为冲突副本，双方都不丢
- 无 base（首次同步/缓存丢失）→ 按 union 合并，同样本地优先 + 冲突副本

app.config 是 JSON 配置，按 key 逐项三方合并，粒度比整文件细得多。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Gitee 凭证三字段永远取本机值，不参与合并（多设备共享 refresh_token 会互相踩踏）
LOCAL_ONLY_CONFIG_KEYS = frozenset({"UserToken", "UserRefreshToken", "TokenExpiresAt"})

_CONFLICT_SUFFIX_RE = re.compile(r"\.remote-.+?-\d{8}-\d{6}")


def conflict_name(rel: str, device_tag: str, ts: str) -> str:
    """冲突副本文件名：<原名>.remote-<设备>.remote-<时间>。"""
    p = Path(rel)
    suffix = "".join(p.suffixes)
    stem = rel[: len(rel) - len(suffix)] if suffix else rel
    return f"{stem}.remote-{device_tag}-{ts}{suffix}"


def is_conflict_copy(rel: str) -> bool:
    """判断是否为冲突副本（合并时不应把它当成正常内容参与三方比较）"""
    return bool(_CONFLICT_SUFFIX_RE.search(Path(rel).name))


@dataclass
class DirMergePlan:
    """目录三方合并的执行计划（只记录动作，不搬运内容）"""

    take_remote: List[str] = field(default_factory=list)
    """用远端内容覆盖本地（本地未动、远端改过 / 远端新增 / 本地删除但远端改过）"""

    conflicts: List[str] = field(default_factory=list)
    """冲突路径：保留本地内容，另存远端副本"""

    delete_local: List[str] = field(default_factory=list)
    """远端已删除且本地未改 → 跟随删除"""

    keep_deleted_local: List[str] = field(default_factory=list)
    """远端已删除但本地改过 → 保留本地（并把删除意图推翻，下次上传会恢复云端）"""

    local_added: List[str] = field(default_factory=list)
    """本地新增（远端无），随后续上传带到云端"""

    @property
    def needs_local_write(self) -> bool:
        return bool(self.take_remote or self.conflicts or self.delete_local)

    @property
    def has_divergence(self) -> bool:
        """本地与远端的差异是否需要一次上传来收敛"""
        return bool(self.conflicts or self.local_added or self.keep_deleted_local)

    def summary(self) -> str:
        return (
            f"取远端 {len(self.take_remote)}，冲突 {len(self.conflicts)}，"
            f"跟随删除 {len(self.delete_local)}，保留本地已删 {len(self.keep_deleted_local)}，"
            f"本地新增 {len(self.local_added)}"
        )


def merge_dir_manifests(
    base: Dict[str, str],
    local: Dict[str, str],
    remote: Dict[str, str],
) -> DirMergePlan:
    """按 base / local / remote 三份清单计算目录合并计划。

    三个参数都是 {相对路径: 内容 sha256}。base 为空表示无基准（首次同步），
    此时 local 与 remote 同路径不同内容一律视为冲突。
    """
    plan = DirMergePlan()
    for rel in sorted(set(base) | set(local) | set(remote)):
        if is_conflict_copy(rel):
            # 冲突副本是产物而非用户内容：任一端存在即保留，不参与三方比较
            if rel in remote and rel not in local:
                plan.take_remote.append(rel)
            continue

        in_base = rel in base
        in_local = rel in local
        in_remote = rel in remote
        b = base.get(rel)
        l = local.get(rel)
        r = remote.get(rel)

        if in_local and in_remote:
            if l == r:
                continue
            if in_base and l == b:
                plan.take_remote.append(rel)  # 只有远端改过
            elif in_base and r == b:
                continue  # 只有本地改过，保留本地等待上传
            else:
                plan.conflicts.append(rel)  # 双方都改 / 无基准 → 冲突
            continue

        if in_local and not in_remote:
            if in_base:
                if l == b:
                    plan.delete_local.append(rel)  # 远端删除，本地未改 → 跟随
                else:
                    plan.keep_deleted_local.append(rel)  # 本地改过 → 保留
            else:
                plan.local_added.append(rel)  # 本地新增
            continue

        if in_remote and not in_local:
            if in_base and r == b:
                continue  # 本地删除，远端未改 → 删除生效，不要补回
            plan.take_remote.append(rel)  # 远端新增 / 远端改过 → 补入
            continue

        # 两边都不存在 → 无动作

    return plan


def merge_config_dicts(
    base: Optional[dict],
    local: dict,
    remote: dict,
) -> Tuple[dict, Dict[str, object], List[str]]:
    """app.config 的三方合并（递归到集合元素级）。

    app.config 结构为 ``{分组: {键: {"value": ...}}}``（qfluentwidgets 格式）。
    返回 (合并结果, 冲突远端值快照, 冲突键列表)。冲突时本地值胜出，远端值放进
    第二个返回值，调用方可落盘成 ``app.config.remote-<ts>`` 供人工取舍。

    合并粒度：
    - 标量配置项 → 值级三方；
    - dict 型值（SavedProviders / ModelOverrides / Tools.Toggles / PermissionPolicy）
      → 逐 entry 三方，再逐字段递归，双机各自新增的 entry 都保留；
    - list 型值（MCP.Servers / EnabledPlugins / EnabledSkills / DisabledPlugins）
      → 按元素标识三方，保留本地顺序，远端新增追加。
    """
    if not isinstance(local, dict):
        return remote if isinstance(remote, dict) else {}, {}, []
    if not isinstance(remote, dict):
        return local, {}, []
    base_d = base if isinstance(base, dict) else {}

    merged, flat_conflicts = _merge_mapping(base_d, local, remote, depth=0)

    conflicts: Dict[str, object] = {}
    conflict_keys: List[str] = []
    for path in flat_conflicts:
        key = path.rsplit(".", 1)[-1]
        conflicts[path] = _lookup(remote, path)
        conflict_keys.append(path)
        if key == "API_KEY":
            conflicts[path] = "<本机保留，未覆盖>"
    return merged, conflicts, conflict_keys


def _lookup(tree: object, path: str) -> object:
    """按 a.b.c 路径从 dict 中取值（用于记录冲突的远端值）"""
    cur = tree
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<missing>"


_MISSING = _Missing()


def _same(a: object, b: object) -> bool:
    """配置值相等判断（dict/list/标量均可，异常时视为不等）"""
    if a is b:
        return True
    if a is _MISSING or b is _MISSING:
        return False
    try:
        return bool(a == b)
    except Exception:
        return False


def _ser(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return repr(value)


def _element_key(value: object) -> str:
    """list 元素的身份标识：优先语义字段，否则整体序列化"""
    if isinstance(value, dict):
        for name in ("id", "name", "key", "uuid", "url", "model", "provider"):
            v = value.get(name)
            if isinstance(v, (str, int)) and str(v):
                return f"{name}:{v}"
        return _ser(value)
    return _ser(value)


def _merge_mapping(
    base: dict,
    local: dict,
    remote: dict,
    depth: int = 0,
) -> Tuple[dict, List[str]]:
    """通用 dict 三方合并，返回 (结果, 冲突路径列表)。"""
    out: dict = {}
    conflicts: List[str] = []
    all_keys = set(base) | set(local) | set(remote)
    for key in sorted(all_keys, key=str):
        if key in LOCAL_ONLY_CONFIG_KEYS:
            # Gitee 凭证三字段：本机权威，不参与合并
            if key in local:
                out[key] = local[key]
            continue

        b_val = base.get(key, _MISSING)
        l_val = local.get(key, _MISSING)
        r_val = remote.get(key, _MISSING)

        if key == "API_KEY":
            # 密钥保护：本机已有值（明文或密文）永不覆盖；本地为空才接收云端
            # （新机器场景，允许用本机主密码尝试解开云端密文）。
            if l_val is not _MISSING and str(l_val or ""):
                out[key] = l_val
                if r_val is not _MISSING and not _same(l_val, r_val):
                    conflicts.append(str(key))
                continue
            if r_val is not _MISSING:
                out[key] = r_val
                continue
            if l_val is not _MISSING:
                out[key] = l_val
            continue

        if l_val is _MISSING and r_val is _MISSING:
            continue

        # 删除传播只在「值内部」开放（配置项集合里的元素、嵌套 dict 的字段）。
        # 分组（depth 0）/ 配置项（depth 1）/ ConfigItem 结构字段（depth 2）都禁用：
        # 老版本客户端不认识新版本加的 ConfigItem，cfg.save() 重写文件时会把它丢掉，
        # 若允许删除传播，这个缺失会被当成用户意图同步到所有设备，导致
        # 「我新加的配置项同步后就没了」。depth 3 起是 SavedProviders 的
        # provider 条目、MCP.Servers 的元素等真实用户数据，删除要传播。
        allow_delete = depth >= 3

        if l_val is _MISSING:
            if allow_delete and b_val is not _MISSING and _same(r_val, b_val):
                continue  # 本地删除，远端未改 → 跟随删除
            out[key] = r_val
            continue
        if r_val is _MISSING:
            if allow_delete and b_val is not _MISSING and _same(l_val, b_val):
                continue  # 远端删除，本地未改 → 跟随删除
            out[key] = l_val
            continue
        if _same(l_val, r_val):
            out[key] = l_val
            continue

        # 两边都是容器 → 始终逐项递归，不走「整组取一边」的短路。
        # 短路会把「远端少了一个键」当成整组替换，内部新增的配置项/条目会被
        # 整组抹掉（历史隐患：老版本客户端重写 app.config 丢掉新 ConfigItem，
        # 同步后新配置项在所有设备上一起消失）。
        if isinstance(l_val, dict) and isinstance(r_val, dict) and depth < 6:
            sub, sub_conflicts = _merge_mapping(
                b_val if isinstance(b_val, dict) else {},
                l_val,
                r_val,
                depth + 1,
            )
            out[key] = sub
            conflicts.extend(f"{key}.{c}" for c in sub_conflicts)
            continue
        if isinstance(l_val, list) and isinstance(r_val, list) and depth < 6:
            sub_list, sub_conflicts = _merge_list(
                b_val if isinstance(b_val, list) else [],
                l_val,
                r_val,
                depth + 1,
            )
            out[key] = sub_list
            conflicts.extend(f"{key}.{c}" for c in sub_conflicts)
            continue

        if b_val is not _MISSING and _same(l_val, b_val):
            out[key] = r_val  # 只有远端改
            continue
        if b_val is not _MISSING and _same(r_val, b_val):
            out[key] = l_val  # 只有本地改
            continue

        # 两边都改（或无基准）→ 标量，本地优先
        out[key] = l_val
        conflicts.append(str(key))

    return out, conflicts


def _merge_list(base: list, local: list, remote: list, depth: int = 0) -> Tuple[list, List[str]]:
    """list 值三方合并：按元素身份合并，保留本地顺序，远端新增追加到末尾。"""
    b_map = {_element_key(x): x for x in base}
    l_map = {_element_key(x): x for x in local}
    r_map = {_element_key(x): x for x in remote}
    merged_map, conflicts = _merge_mapping(b_map, l_map, r_map, depth=depth + 1)
    ordered = [merged_map[k] for k in l_map if k in merged_map]
    ordered += [merged_map[k] for k in r_map if k in merged_map and k not in l_map]
    return ordered, conflicts

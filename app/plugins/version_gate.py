# -*- coding: utf-8 -*-
"""插件版本契约门禁。

plugin.json 可选声明 ``min_host_version``（如 "0.5.8"），宿主加载前比对当前
版本，不满足则拒载该插件（P1 加固）。目标：坏插件/旧插件在加载前被拦住，
而不是进 registry 后在主链路里炸。

规则（宽容优先，避免误伤）：
- 未声明 min_host_version → 视为兼容（老插件零改动）
- 声明了但解析失败（作者写错格式）→ 拒载（契约3：声明即契约，格式非法
  = 契约不可信，防脏数据/注入串混入加载链）
- 解析成功则按 semver 数值比较：host >= min 才放行
- 宿主自身版本读不到 → 放行（宿主异常不迁怒插件）
"""
from __future__ import annotations

import re
from typing import Tuple

from loguru import logger

_SEMVER_RE = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?\s*$")


def parse_semver(text: str) -> Tuple[int, int, int] | None:
    """宽容解析版本号为 (major, minor, patch)。解析失败返回 None。

    接受 "0.5.8" / "v0.5.8" / "0.5" / "0.5.8-beta.1"；预发布后缀忽略。
    """
    if not text:
        return None
    m = _SEMVER_RE.match(str(text))
    if not m:
        return None
    major = int(m.group(1))
    minor = int(m.group(2) or 0)
    patch = int(m.group(3) or 0)
    return (major, minor, patch)


def host_version() -> str:
    """宿主当前版本（去 v 前缀）。读不到时返回 "0.0.0"（放行一切，宽容）。"""
    try:
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        v = str(getattr(cfg, "current_version", "") or "")
        return v.lstrip("vV") or "0.0.0"
    except Exception:
        return "0.0.0"


# 宿主插件 API 契约版本：manifest 可选声明 api_version，高于此值拒载
HOST_PLUGIN_API_VERSION = 1


def check_api_version(manifest: dict, plugin_name: str = "") -> Tuple[bool, str]:
    """契约2：manifest 可选 api_version 与宿主插件 API 版本比对。

    - 缺省/None → 兼容（老插件零改动，存量插件均未声明）
    - > HOST_PLUGIN_API_VERSION → 不兼容（load_blocked，reason 可见）
    - < 1（含 0/负数）→ warning 向下兼容放行

    Returns:
        (是否兼容, 不兼容原因)。兼容时 reason 为空串。
    """
    api = (manifest or {}).get("api_version")
    if api is None:
        return True, ""
    if not isinstance(api, int) or isinstance(api, bool):
        # 契约1 schema 层已把非 int 规范化为缺省 1；此处兜底
        api = 1
    if api < 1:
        logger.warning(
            f"[VersionGate] 插件 '{plugin_name}' api_version={api} < 1，按向下兼容放行"
        )
        return True, ""
    if api > HOST_PLUGIN_API_VERSION:
        reason = f"插件 api_version={api} 高于宿主支持的 {HOST_PLUGIN_API_VERSION}"
        logger.warning(f"[VersionGate] 插件 '{plugin_name}' {reason}，拒载")
        return False, reason
    return True, ""


def check_host_version(manifest: dict, plugin_name: str = "") -> Tuple[bool, str]:
    """按 manifest 的 min_host_version 校验当前宿主。

    Returns:
        (是否兼容, 不兼容原因)。兼容时 reason 为空串。
    """
    required = (manifest or {}).get("min_host_version")
    if not required:
        return True, ""

    cur = parse_semver(host_version())
    need = parse_semver(str(required))
    if need is None:
        # 契约3：声明即契约，格式非法=契约不可信，拒载（原为放行+warning）
        reason = f"min_host_version 格式非法: {required!r}"
        logger.warning(f"[VersionGate] 插件 '{plugin_name}' {reason}，拒载")
        return False, reason
    if cur is None:
        # 宿主版本未知（异常兜底），放行并记录
        logger.warning("[VersionGate] 宿主版本未知，跳过插件版本校验")
        return True, ""

    if cur >= need:
        return True, ""
    reason = f"需要宿主 >= v{need[0]}.{need[1]}.{need[2]}，当前 v{cur[0]}.{cur[1]}.{cur[2]}"
    logger.warning(f"[VersionGate] 插件 '{plugin_name}' 版本不满足: {reason}")
    return False, reason

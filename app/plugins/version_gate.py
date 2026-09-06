# -*- coding: utf-8 -*-
"""插件版本契约门禁。

plugin.json 可选声明 ``min_host_version``（如 "0.5.8"），宿主加载前比对当前
版本，不满足则拒载该插件（P1 加固）。目标：坏插件/旧插件在加载前被拦住，
而不是进 registry 后在主链路里炸。

规则（宽容优先，避免误伤）：
- 未声明 min_host_version → 视为兼容（老插件零改动）
- 声明了但解析失败（作者写错格式）→ 视为兼容 + warning（不因拼写错误拒载）
- 解析成功则按 semver 数值比较：host >= min 才放行
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
        from app.utils.config import Config

        v = str(getattr(Config, "current_version", "") or "")
        return v.lstrip("vV") or "0.0.0"
    except Exception:
        return "0.0.0"


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
        logger.warning(
            f"[VersionGate] 插件 '{plugin_name}' 的 min_host_version 格式非法: {required!r}，忽略校验"
        )
        return True, ""
    if cur is None:
        # 宿主版本未知（异常兜底），放行并记录
        logger.warning("[VersionGate] 宿主版本未知，跳过插件版本校验")
        return True, ""

    if cur >= need:
        return True, ""
    reason = f"需要宿主 >= v{need[0]}.{need[1]}.{need[2]}，当前 v{cur[0]}.{cur[1]}.{cur[2]}"
    logger.warning(f"[VersionGate] 插件 '{plugin_name}' 版本不满足: {reason}")
    return False, reason

# -*- coding: utf-8 -*-
"""插件运行诊断 — 汇总宿主环境、插件状态、兼容性、依赖与日志错误

产物是一段可直接粘贴给 AI 的纯文本：用户在插件市场点「诊断」复制后
发给任意对话，AI 据此定位插件加载/运行问题。

设计约束：
- 纯读操作，不触发网络（市场数据走缓存，拉取失败不影响其余段落）
- 可在后台线程调用：只用 installer / deps_loader / Path IO，不碰 Qt
- 日志只读尾部片段（all.log 可达 10MB，全量读会拖慢且无必要）
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger

_MAX_DIRECTORY_ITEMS = 80  # 已安装清单最多列多少个插件
_MAX_LOG_ERRORS = 40  # 日志错误最多取多少条

# 日志里与本问题域相关的模块标记（命中即视为插件相关）
_PLUGIN_LOG_MARKS = (
    "[PluginManager]",
    "[UIPluginRegistry]",
    "[PluginToolLoader]",
    "[deps_loader]",
    "[VersionGate]",
    "[Installer]",
    "[plugin-marketplace]",
    "[PluginHostService]",
)
_LEVEL_RE = re.compile(r"\b(ERROR|CRITICAL|WARNING|SUCCESS)\b")


def _log_dir() -> Path:
    """日志目录（与 main.py 传给 setup_logging 的路径一致）"""
    try:
        from app.utils.utils import get_app_data_dir

        return get_app_data_dir() / "logs"
    except Exception:
        return Path.home() / ".drifox" / "logs"


def _host_info() -> Tuple[str, str]:
    """(宿主版本, 平台) —— 读不到时给兜底文案"""
    try:
        from app.plugins.deps_loader import host_version_string

        version = host_version_string()
    except Exception:
        version = "未知"
    return version, sys.platform


def _plugin_dir_map(inst) -> Dict[str, Path]:
    """插件名 → 目录（启用 / 禁用 / 系统三处），仅收录有 manifest 的目录"""
    result: Dict[str, Path] = {}
    for attr in ("_plugins_dir", "_disabled_dir", "_system_dir"):
        base = getattr(inst, attr, None)
        if base is None or not Path(base).is_dir():
            continue
        try:
            for entry in Path(base).iterdir():
                if not entry.is_dir():
                    continue
                if (
                    (entry / ".drifox-plugin" / "plugin.json").is_file()
                    or (entry / ".claude-plugin" / "plugin.json").is_file()
                ):
                    result.setdefault(entry.name, entry)
        except OSError:
            continue
    return result


def _missing_deps(plugin_dir: Path, manifest: dict) -> List[str]:
    """缺失的 pip 依赖（复用市场的依赖安装器判定）

    宿主 missing_pip_deps 按「包名 = 顶层目录名」匹配，会误报安装了
    dist-info 但导入名不同的包（python-docx 实装为 docx + python_docx-*.dist-info）。
    这里用 dist-info 目录二次校验，命中则视为已就绪，避免诊断报告误导 AI。
    """
    try:
        from .deps_installer import DepsInstaller

        missing = list(DepsInstaller()._missing(plugin_dir, manifest) or [])
    except Exception:
        return []
    if not missing:
        return []

    installed: set = set()
    try:
        from app.plugins.deps_loader import current_platform_key

        dep_dirs = [plugin_dir / "deps" / current_platform_key(), plugin_dir / "deps"]
    except Exception:
        dep_dirs = [plugin_dir / "deps"]
    for d in dep_dirs:
        if not d.is_dir():
            continue
        try:
            for entry in d.iterdir():
                if entry.is_dir() and entry.name.endswith(".dist-info"):
                    installed.add(entry.name.split("-")[0].replace("-", "_").lower())
        except OSError:
            continue

    kept: List[str] = []
    for spec in missing:
        pkg = spec.split("==")[0].split(">=")[0].split("<=")[0].split("~=")[0].split("!=")[0]
        pkg = pkg.split("[")[0].strip().replace("-", "_").lower()
        if pkg and pkg in installed:
            continue
        kept.append(spec)
    return kept


def _compat_reasons(manifest: dict) -> List[str]:
    """平台 / 宿主版本不满足的原因（空列表 = 兼容）"""
    reasons: List[str] = []
    try:
        from app.plugins.deps_loader import check_host_compat, check_platform

        for check in (check_platform, check_host_compat):
            ok, reason = check(manifest)
            if not ok and reason:
                reasons.append(reason)
    except Exception:
        pass
    return reasons


def _collect_log_errors(plugin_names: Optional[set] = None, limit: int = _MAX_LOG_ERRORS) -> List[str]:
    """从 all.log 尾部提取插件相关的错误/警告行（去重保序，最多 limit 条）

    插件相关的判定：命中已知插件模块标记，或行内出现已安装插件名
    （``[bili-scroll]``）。不用宽松的「任意小写括号词」正则——那会把
    ``[dock-blank]`` 这类渲染日志误当成插件错误。
    """
    log_file = _log_dir() / "all.log"
    if not log_file.is_file():
        return []
    try:
        # 只读尾部 512KB：10MB 全量读会明显拖慢诊断
        with log_file.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 512 * 1024))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError as e:
        logger.debug(f"[diagnostics] 读取日志失败: {e}")
        return []

    name_marks = tuple(f"[{n}]" for n in (plugin_names or ()))
    seen = set()
    picked: List[str] = []
    for raw in reversed(chunk.splitlines()):
        line = raw.strip()
        if not line or not _LEVEL_RE.search(line):
            continue
        if not any(mark in line for mark in _PLUGIN_LOG_MARKS) and not (
            name_marks and any(mark in line for mark in name_marks)
        ):
            continue
        if line in seen:
            continue
        seen.add(line)
        picked.append(line)
        if len(picked) >= limit:
            break
    picked.reverse()
    return picked


def collect_diagnostics() -> str:
    """采集插件运行诊断，返回可直接粘贴给 AI 的文本（不抛异常）"""
    lines: List[str] = []
    host_ver, platform = _host_info()
    lines.append("# DriFox 插件运行诊断报告")
    lines.append(
        f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')} | 宿主: v{host_ver} | 平台: {platform}"
    )

    try:
        from .installer import get_installer

        inst = get_installer()
        status_map = inst.get_status_map()
        inst_map = inst.get_installed_map()
        manifests = inst.get_manifest_map()
        dir_map = _plugin_dir_map(inst)
    except Exception as e:
        lines.append(f"\n## 采集失败\n无法读取插件状态: {e}")
        return "\n".join(lines)

    # ── 市场可更新数（只读缓存；拉不到不影响报告） ──
    market: Dict[str, dict] = {}
    try:
        from .data import get_marketplace

        market = {str(p.get("name", "")): p for p in get_marketplace().list_plugins()}
    except Exception as e:
        logger.debug(f"[diagnostics] 市场数据不可用: {e}")

    # ── 一、已安装插件总览 ──
    names = sorted(status_map)
    counts: Dict[str, int] = {}
    for st in status_map.values():
        counts[st] = counts.get(st, 0) + 1
    summary = " / ".join(f"{k} {v}" for k, v in sorted(counts.items()) if v)
    lines.append(f"\n## 一、已安装插件（{len(names)} 个：{summary or '无'}）")

    # ── 二、异常项汇总（兼容性 / 依赖） ──
    anomalies: List[str] = []
    for name in names:
        manifest = manifests.get(name) or {}
        reasons = _compat_reasons(manifest)
        plugin_dir = dir_map.get(name)
        missing = _missing_deps(plugin_dir, manifest) if plugin_dir and manifest else []
        if missing:
            reasons.append("依赖缺失: " + ", ".join(missing[:8]))
        if reasons:
            anomalies.append(f"- {name}（{status_map.get(name)}）: " + "；".join(reasons))

    for name, meta in market.items():
        if name in status_map:
            continue
        reasons = []
        try:
            from app.plugins.deps_loader import check_host_compat, check_platform

            for check in (check_platform, check_host_compat):
                ok, reason = check(meta)
                if not ok and reason:
                    reasons.append(f"市场插件不兼容当前环境: {reason}")
        except Exception:
            pass
        if reasons:
            anomalies.append(f"- {name}（未安装）: " + "；".join(reasons))

    lines.append(f"\n## 二、异常项（{len(anomalies)} 个）")
    if anomalies:
        lines.extend(anomalies[: _MAX_DIRECTORY_ITEMS])
        if len(anomalies) > _MAX_DIRECTORY_ITEMS:
            lines.append(f"... 还有 {len(anomalies) - _MAX_DIRECTORY_ITEMS} 项未列出")
    else:
        lines.append("无：所有已装插件的平台/宿主版本/依赖均正常")

    # ── 三、清单（含可更新标记） ──
    lines.append("\n## 三、插件清单")
    for name in names[:_MAX_DIRECTORY_ITEMS]:
        ver = inst_map.get(name) or "?"
        bits = [f"{name} v{ver}", status_map.get(name, "?")]
        meta_ver = str((market.get(name) or {}).get("version") or "")
        if meta_ver:
            try:
                from .data import compare_versions

                if compare_versions(str(ver), meta_ver) < 0:
                    bits.append(f"可更新 v{meta_ver}")
            except Exception:
                pass
        lines.append("- " + " | ".join(bits))
    if len(names) > _MAX_DIRECTORY_ITEMS:
        lines.append(f"... 还有 {len(names) - _MAX_DIRECTORY_ITEMS} 个未列出")

    # ── 四、日志错误 ──
    log_errors = _collect_log_errors(set(names))
    lines.append(f"\n## 四、最近日志中的插件错误（{len(log_errors)} 条）")
    if log_errors:
        lines.extend(log_errors)
    else:
        lines.append("无：日志尾部未发现插件相关的 ERROR/WARNING")

    # ── 五、修复要求 ──
    lines.append("\n## 五、修复要求")
    lines.append(
        "请根据以上信息定位并修复 DriFox 插件加载/运行问题。约束：\n"
        "1. 先逐条解释异常项的可能根因（平台/版本/依赖/禁用各是什么后果），再动手；\n"
        "2. 只改有问题的插件或必要的宿主代码，不顺手重构无关文件；\n"
        "3. 修复后说明验证方式（重载插件或重启宿主，确认工具/命令重新注册）。"
    )
    return "\n".join(lines)

# -*- coding: utf-8 -*-
"""插件市场工具 — 让模型自己搜索 / 安装 / 卸载 / 更新 / 启停插件

设计要点：
- 复用市场插件自身链路：工具模块由主程序以 ``_plugin_tool_*`` 独立模块名加载，
  与 ``ui/`` 包不属于同一命名空间，不能相对导入。这里按模块名取
  ``ui_plugin_plugin_marketplace``（UIPluginRegistry 已加载）的 installer / data
  子模块，保证与市场卡片共用同一份安装器与缓存单例。
- 线程约定：安装/更新/卸载/启停的目录操作在工具线程（后台线程）执行，符合
  ``PluginInstaller`` 的调用约定；「删除/替换目录前卸载插件 UI」与「启用后
  重载插件组件」必须回主线程，统一走 ``run_on_main_thread``。
- 危险动作（装/卸/更新/启停）声明 ``danger="dangerous"``，调用前由权限系统向
  用户确认；工具内不再弹 Qt 对话框（工具线程不能操作控件）。
"""

from __future__ import annotations

import importlib
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from app.plugins.sdk import ToolResult, make_summarize_from_preview

_PLUGIN_NAME = "plugin-marketplace"
_UI_DIR = Path(__file__).resolve().parent.parent / "ui"
# 兜底包名：UI 组件未加载时（CLI / UI 加载失败）用私有包名自持一份，避免与
# 其它插件的通用 ``ui`` 包抢占 sys.modules。
_FALLBACK_PKG = "_plugin_marketplace_ui"
_GROUP = "插件市场"
_ICON = "插件市场"

# 市场值缺失的兜底文案
_STATE_LABEL = {
    "enabled": "已安装·启用",
    "disabled": "已安装·禁用",
    "builtin_enabled": "内置·启用",
    "builtin_disabled": "内置·禁用",
    "system": "系统插件",
}
# 可以卸载 / 移动目录的状态（用户插件目录内）
_USER_STATES = ("enabled", "disabled")
# 已安装（含内置/系统）状态
_INSTALLED_STATES = ("enabled", "disabled", "builtin_enabled", "builtin_disabled", "system")

# ── 插件内模块解析 ──────────────────────────────────────

def _ui_module(sub: str):
    """取插件 ui/ 下的子模块（installer / data / marketplace_manager）

    优先复用 UIPluginRegistry 已加载的 ``ui_plugin_plugin_marketplace`` 包：
    与市场卡片共享 installer 单例、安装缓存与市场源配置。热重载会摘除该包，
    因此每次调用重新解析，不缓存模块对象。
    """
    safe = _PLUGIN_NAME.replace("-", "_")
    pkg = f"ui_plugin_{safe}"
    if pkg in sys.modules:
        return importlib.import_module(f"{pkg}.{sub}")
    if _FALLBACK_PKG not in sys.modules:
        _load_fallback_package()
    return importlib.import_module(f"{_FALLBACK_PKG}.{sub}")


def _load_fallback_package() -> None:
    """按包结构加载插件 ui/__init__.py（只执行模块级 import，不调 register_ui）"""
    init = _UI_DIR / "__init__.py"
    if not init.is_file():
        raise RuntimeError(f"插件市场 UI 目录缺失: {_UI_DIR}")
    spec = importlib.util.spec_from_file_location(
        _FALLBACK_PKG, init, submodule_search_locations=[str(_UI_DIR)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法构建插件市场模块说明（spec）")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_FALLBACK_PKG] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_FALLBACK_PKG, None)
        raise


def _installer():
    return _ui_module("installer").get_installer()


def _display_name(meta: dict) -> str:
    """展示名：转调主程序公共契约层（与设置页、市场卡片同口径）。"""
    from app.plugins.contracts.plugin_display import plugin_display_name

    return plugin_display_name(meta)

def _market_data():
    return _ui_module("data")


def _market_plugins(force: bool = False) -> List[Dict[str, Any]]:
    return list(_market_data().get_marketplace().list_plugins(force=force) or [])


def _find_market_plugin(name: str, force: bool = False) -> Optional[Dict[str, Any]]:
    """按标识名或展示名查市场元数据（大小写不敏感）

    标识名（meta.name，安装主键）精确匹配优先；未命中回退展示名
    （display_name 解析结果）精确匹配，多个同名时取下载量最高，
    与 marketplace_search 排序口径一致。
    """
    target = name.strip().lower()
    if not target:
        return None
    plugins = _market_plugins(force=force)
    for meta in plugins:
        if str(meta.get("name", "")).strip().lower() == target:
            return meta
    display_hits = [m for m in plugins if _display_name(m).strip().lower() == target]
    if display_hits:
        return max(display_hits, key=lambda m: _to_int(m.get("downloads")))
    return None

def _resolve_local_name(name: str, inst) -> str:
    """把输入名解析成本地已装插件的标识名（slug）

    标识名直接命中；否则按已装插件的展示名反查。未装返回原输入，由调用方报错。
    """
    if inst.get_status_map().get(name):
        return name
    target = name.strip().lower()
    if target:
        try:
            manifests = inst.get_manifest_map()
        except Exception:
            manifests = {}
        for n in inst.get_status_map():
            manifest = manifests.get(n)
            if manifest and _display_name(manifest).strip().lower() == target:
                return n
    return name


# ── 主线程投递（UI 卸载 / 组件重载） ─────────────────────

def _run_on_main(fn, timeout: float = 15.0) -> bool:
    try:
        from app.core.services.plugin_host_service import run_on_main_thread

        return bool(run_on_main_thread(fn, wait=True, timeout=timeout))
    except Exception as e:  # 无 GUI（CLI）或主线程忙：记录后继续，删除仍有 relocate 兜底
        logger.debug(f"[MarketplaceTool] 主线程投递失败: {e}")
        return False


def _unload_ui_on_main(name: str) -> bool:
    """删除/移动/替换插件目录前，主线程卸载其 UI 组件（显示中卡片自动关闭）"""

    def _do():
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        UIPluginRegistry.get_instance().unload_plugin(name)

    return _run_on_main(_do)


def _reload_plugin_on_main(name: str, action: str) -> bool:
    """启用后精准重载插件全部组件（UI/工具/命令），不触发全量重载"""

    def _do():
        from app.core.services.plugin_host_service import PluginHostService

        PluginHostService.get_instance().reload_plugin_targeted(name, action=action)

    return _run_on_main(_do, timeout=25.0)


# ── 状态与格式化 ────────────────────────────────────────

def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _protected_names() -> set:
    """不可禁用/卸载的核心插件名单（与 PluginManager 单一数据源）"""
    try:
        return set(_installer()._non_disableable_names())
    except Exception:
        return {"plugin-marketplace"}


def _self_guard(name: str) -> Optional[str]:
    """插件市场自身不能通过工具改动（删除/替换正在运行的安装链路）"""
    if name and name.lower() == _PLUGIN_NAME:
        return "不能通过工具操作插件市场自身（会中断当前安装链路），请让用户在设置中处理"
    return None


def _install_state(name: str, status_map: dict, inst_map: dict, market_ver: str = "") -> str:
    status = status_map.get(name)
    if status is None:
        return "未安装"
    local = inst_map.get(name)
    label = _STATE_LABEL.get(status, status)
    text = f"{label} v{local}" if local else label
    if market_ver and local:
        try:
            if _market_data().compare_versions(local, market_ver) < 0:
                text += f" → 可更新 v{market_ver}"
        except Exception:
            pass
    return text


def _shorten(text: Any, limit: int = 120) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s[:limit] + "…" if len(s) > limit else s


def _compat_notes(meta: dict) -> List[str]:
    """兼容性提示（空列表 = 兼容）：平台 / 宿主版本不满足时给出原因。

    与插件市场 UI 的置灰判定同源（``deps_loader.check_platform`` /
    ``check_host_compat``），保证 AI 看到的结论与用户界面一致；宿主老版本
    无该 API 时静默跳过，不阻断搜索。
    """
    notes: List[str] = []
    try:
        from app.plugins.deps_loader import check_host_compat, check_platform

        ok, reason = check_platform(meta)
        if not ok and reason:
            notes.append(f"⚠ {reason}")
        ok, reason = check_host_compat(meta)
        if not ok and reason:
            notes.append(f"⚠ {reason}")
    except Exception as e:
        logger.debug(f"[MarketplaceTool] 兼容性判定不可用: {e}")
    return notes


def _installed_dir(name: str) -> str:
    """已安装插件的实际目录（启用 / 禁用 / 系统三处查找），未找到返回空串"""
    try:
        inst = _installer()
        for attr in ("_plugins_dir", "_disabled_dir", "_system_dir"):
            base = getattr(inst, attr, None)
            if base is None:
                continue
            p = Path(base) / name
            if (p / ".drifox-plugin" / "plugin.json").is_file() or (
                p / ".claude-plugin" / "plugin.json"
            ).is_file():
                return str(p.resolve())
    except Exception:
        pass
    return ""


def _format_plugin_line(idx: int, meta: dict, status_map: dict, inst_map: dict) -> str:
    name = str(meta.get("name", ""))
    ver = str(meta.get("version", "") or "?")
    state = _install_state(name, status_map, inst_map, ver)
    tags = list(meta.get("categories") or []) + list(meta.get("keywords") or [])
    bits = [state]
    if tags:
        bits.append("分类 " + "/".join(str(t) for t in tags[:4]))
    if meta.get("_marketplace"):
        bits.append(f"来源 {meta['_marketplace']}")
    # 不兼容插件显式标注：AI 推荐/安装前即可看到「装不了」的原因
    bits.extend(_compat_notes(meta))
    display = _display_name(meta)
    label = display if display == name else f"{display} ({name})"
    head = f"{idx}. {label} v{ver} | " + " | ".join(bits)
    desc = _shorten(meta.get("description"))
    return f"{head}\n    {desc}" if desc else head


def _match_score(meta: dict, keyword: str) -> int:
    """关键词相关度：名字命中优先于分类/标签，再其次是描述"""
    name = str(meta.get("name", "")).lower()
    if name == keyword:
        return 100
    if name.startswith(keyword):
        return 80
    if keyword in name:
        return 60
    display = _display_name(meta).lower()
    if keyword in display:
        return 55
    tag_text = " ".join(
        str(t) for t in (list(meta.get("categories") or []) + list(meta.get("tags") or []))
    ).lower()
    if keyword in tag_text:
        return 40
    if keyword in str(meta.get("description", "")).lower():
        return 20
    return 0


# ── 工具实现 ───────────────────────────────────────────

def _search_impl(tool_ctx, **kwargs):
    keyword = str(kwargs.get("keyword") or "").strip()
    category = str(kwargs.get("category") or "").strip()
    force = bool(kwargs.get("force"))
    limit = max(1, min(_to_int(kwargs.get("limit")) or 20, 50))
    try:
        data = _market_data().get_marketplace().fetch(force=force)
    except Exception as e:
        logger.warning(f"[MarketplaceTool] 拉取市场失败: {e}")
        return ToolResult(False, error=f"拉取插件市场失败（网络/源配置问题）: {e}")
    plugins = list(data.get("plugins") or [])
    source_errors = list(data.get("_errors") or [])
    if category:
        plugins = [p for p in plugins if category in (p.get("categories") or [])]

    if keyword:
        kw = keyword.lower()
        scored = [(s, p) for p in plugins if (s := _match_score(p, kw)) > 0]
        scored.sort(key=lambda it: (-it[0], -_to_int(it[1].get("downloads"))))
        plugins = [p for _, p in scored]
    else:
        plugins.sort(key=lambda p: (-_to_int(p.get("downloads")), str(p.get("name", ""))))

    inst = _installer()
    status_map = inst.get_status_map()
    inst_map = inst.get_installed_map()
    shown = plugins[:limit]

    if not plugins:
        hint = "换个关键词，或省略 keyword 列出市场全部插件"
        return ToolResult(True, content=f"市场中没有匹配「{keyword}」的插件。{hint}。")

    lines = [f"市场匹配 {len(plugins)} 个插件（按相关度/下载量排序，显示 {len(shown)} 个）："]
    for i, meta in enumerate(shown, 1):
        lines.append(_format_plugin_line(i, meta, status_map, inst_map))
    if len(plugins) > len(shown):
        lines.append(f"... 还有 {len(plugins) - len(shown)} 个未显示，可加 keyword 缩小范围或提高 limit")
    if source_errors:
        lines.append("⚠ 部分市场源拉取失败（结果可能不全）：" + "；".join(source_errors[:3]))
    lines.append(
        "安装/详情请用 marketplace_install / marketplace_detail，"
        "参数 plugin 传上面括号内的标识名（展示名也支持）。"
    )
    return ToolResult(True, content="\n".join(lines))


def _installed_impl(tool_ctx, **kwargs):
    keyword = str(kwargs.get("keyword") or "").strip().lower()
    check_update = kwargs.get("check_update", True) is not False
    try:
        inst = _installer()
        status_map = inst.get_status_map()
        inst_map = inst.get_installed_map()
    except Exception as e:
        return ToolResult(False, error=f"扫描已安装插件失败: {e}")

    market: Dict[str, dict] = {}
    market_error = ""
    if check_update:
        try:
            market = {str(p.get("name", "")): p for p in _market_plugins(force=False)}
        except Exception as e:
            market_error = str(e)
    # 本地 manifest（get_installed_map 扫描时已缓存）：兼容性判定用本地声明，
    # 市场条目可能过期或缺失（本地/已下架插件）
    try:
        manifests = inst.get_manifest_map()
    except Exception:
        manifests = {}

    names = [n for n in sorted(status_map) if status_map.get(n) in _INSTALLED_STATES]
    if keyword:
        def _kw_hit(n: str) -> bool:
            if keyword in n.lower():
                return True
            manifest = manifests.get(n) or market.get(n) or {}
            return keyword in _display_name(manifest).lower()
        names = [n for n in names if _kw_hit(n)]
    if not names:
        return ToolResult(True, content="没有匹配的已安装插件。")

    # 默认只详列可操作的用户插件；内置/系统插件（随主程序分发、不可卸载）只给数量与
    # 样例，避免 80+ 行清单白占上下文。带 keyword 查询时全部详列。
    if keyword:
        detail, folded = names, []
    else:
        detail = [n for n in names if status_map.get(n) in _USER_STATES]
        folded = [n for n in names if status_map.get(n) not in _USER_STATES]

    counts = {state: 0 for state in _STATE_LABEL}
    for n in detail:
        st = status_map.get(n, "")
        counts[st] = counts.get(st, 0) + 1
    summary = "，".join(f"{_STATE_LABEL.get(s, s)} {c}" for s, c in counts.items() if c)
    lines = [f"可操作用户插件 {len(detail)} 个（{summary}）："] if detail else ["没有可操作的用户插件。"]
    for i, name in enumerate(detail, 1):
        ver = str((market.get(name) or {}).get("version", "") or "")
        local_meta = manifests.get(name) or market.get(name) or {}
        compat = _compat_notes(local_meta)
        suffix = (" | " + " | ".join(compat)) if compat else ""
        display = _display_name(local_meta)
        label = name if not display or display == name else f"{name}（{display}）"
        lines.append(f"{i}. {label} | {_install_state(name, status_map, inst_map, ver)}{suffix}")
    if folded:
        sample = "、".join(folded[:10]) + ("…" if len(folded) > 10 else "")
        lines.append(f"另有 {len(folded)} 个内置/系统插件（不可卸载，可用 keyword 查询）：{sample}")
    if market_error:
        lines.append(f"（更新状态不可用：{market_error}）")
    lines.append("更新用 marketplace_update；卸载用 marketplace_uninstall；启停用 marketplace_set_enabled。")
    return ToolResult(True, content="\n".join(lines))


def _install_impl(tool_ctx, **kwargs):
    name = str(kwargs.get("plugin") or "").strip()
    if not name:
        return ToolResult(False, error="缺少 plugin 参数：请传市场返回的精确插件名")
    blocked = _self_guard(name)
    if blocked:
        return ToolResult(False, error=blocked)
    try:
        inst = _installer()
        meta = _find_market_plugin(name, force=False) or _find_market_plugin(name, force=True)
        # 展示名输入在这里规范化成 slug，后续本地状态/安装记录全部按 slug 走
        name = str((meta or {}).get("name") or name)
        status = inst.get_status_map().get(name)
        if status:
            local = inst.get_installed_version(name)
            return ToolResult(
                True,
                content=(
                    f"{name} 已安装（v{local or '?'}，状态：{_STATE_LABEL.get(status, status)}），"
                    "未重复安装。升级用 marketplace_update；启用/禁用用 marketplace_set_enabled。"
                ),
            )
        if meta is None:
            return ToolResult(
                False,
                error=f"市场中没有名为 {name} 的插件。先 marketplace_search 确认精确插件名",
            )
        started = time.time()
        ok = inst.install(meta)
    except Exception as e:
        logger.warning(f"[MarketplaceTool] 安装 {name} 失败: {e}")
        return ToolResult(False, error=f"安装 {name} 失败: {e}")

    cost = f"{time.time() - started:.1f}s"
    if not ok:
        return ToolResult(False, error=f"安装 {name} 失败（{cost}）：{inst.last_error or '未知错误'}")
    ver = meta.get("version", "?")
    return ToolResult(
        True,
        content=(
            f"已安装 {name} v{ver}（耗时 {cost}）。\n"
            "插件的 UI 卡片/命令立即生效，新增的工具/技能/智能体从下一轮请求起可用"
            "（极端情况下需重启 DriFox）。依赖缺失时市场会自动尝试安装，失败可让用户在插件市场详情重试。"
        ),
    )


def _uninstall_impl(tool_ctx, **kwargs):
    name = str(kwargs.get("plugin") or "").strip()
    if not name:
        return ToolResult(False, error="缺少 plugin 参数")
    blocked = _self_guard(name)
    if blocked:
        return ToolResult(False, error=blocked)
    try:
        inst = _installer()
        name = _resolve_local_name(name, inst)
        status = inst.get_status_map().get(name)
        if status is None:
            return ToolResult(False, error=f"{name} 未安装，无需卸载")
        if status not in _USER_STATES:
            what = "系统插件" if status == "system" else "内置插件"
            return ToolResult(
                False,
                error=(
                    f"{name} 是{what}（随主程序分发，不能卸载）。"
                    "需要停用请用 marketplace_set_enabled（enabled=false）"
                ),
            )
        ui_ok = _unload_ui_on_main(name)
        ok = inst.uninstall(name)
    except Exception as e:
        logger.warning(f"[MarketplaceTool] 卸载 {name} 失败: {e}")
        return ToolResult(False, error=f"卸载 {name} 失败: {e}")

    if not ok:
        return ToolResult(False, error=f"卸载 {name} 失败：目录删除不完整，请重启后重试或让用户在插件市场处理")
    extra = "" if ui_ok else "\n（UI 未能在主线程卸载，重启后完全生效）"
    return ToolResult(
        True,
        content=(
            f"已卸载 {name}。插件数据目录（plugin_data/{name}）保留未删（重装可复用）。"
            "该插件的工具/命令从下一轮请求起不再出现。"
            f"{extra}"
        ),
    )


def _update_impl(tool_ctx, **kwargs):
    name = str(kwargs.get("plugin") or "").strip()
    if not name:
        return ToolResult(False, error="缺少 plugin 参数")
    blocked = _self_guard(name)
    if blocked:
        return ToolResult(False, error=blocked)
    try:
        inst = _installer()
        name = _resolve_local_name(name, inst)
        status = inst.get_status_map().get(name)
        if status is None:
            return ToolResult(False, error=f"{name} 未安装；安装请用 marketplace_install")
        meta = _find_market_plugin(name, force=False) or _find_market_plugin(name, force=True)
        if meta is None:
            return ToolResult(False, error=f"市场中没有名为 {name} 的插件，无法更新（可能是本地/已下架插件）")
        has_update, local_ver, remote_ver = inst.check_update(meta)
        if not has_update:
            return ToolResult(
                True,
                content=f"{name} 已是最新（本地 v{local_ver or '?'}，市场 v{remote_ver or '?'}）",
            )
        # 启用中先卸载 UI；禁用中的插件目录在 plugins-disabled，UI 未加载
        if status in ("enabled", "builtin_enabled", "system"):
            _unload_ui_on_main(name)
        started = time.time()
        ok = inst.update(meta)
    except Exception as e:
        logger.warning(f"[MarketplaceTool] 更新 {name} 失败: {e}")
        return ToolResult(False, error=f"更新 {name} 失败: {e}")

    cost = f"{time.time() - started:.1f}s"
    if not ok:
        return ToolResult(False, error=f"更新 {name} 失败（旧版保留可用）：{inst.last_error or '未知错误'}")
    new_ver = inst.get_installed_version(name) or remote_ver
    return ToolResult(True, content=f"已更新 {name}：v{local_ver or '?'} → v{new_ver}（耗时 {cost}）")


def _set_enabled_impl(tool_ctx, **kwargs):
    name = str(kwargs.get("plugin") or "").strip()
    if not name:
        return ToolResult(False, error="缺少 plugin 参数")
    enabled = kwargs.get("enabled")
    if not isinstance(enabled, bool):
        return ToolResult(False, error="缺少 enabled 参数（true=启用，false=禁用）")
    blocked = _self_guard(name)
    if blocked:
        return ToolResult(False, error=blocked)
    if not enabled and name in _protected_names():
        return ToolResult(False, error=f"{name} 是核心插件（禁用会断主链路），不能禁用")
    try:
        inst = _installer()
        name = _resolve_local_name(name, inst)
        if not enabled and name in _protected_names():
            return ToolResult(False, error=f"{name} 是核心插件（禁用会断主链路），不能禁用")
        status = inst.get_status_map().get(name)
        if status is None:
            return ToolResult(False, error=f"{name} 未安装；先 marketplace_install")
        if enabled:
            if status in ("enabled", "builtin_enabled", "system"):
                return ToolResult(True, content=f"{name} 已是启用状态（{_STATE_LABEL.get(status, status)}），未做改动")
            was_builtin = status == "builtin_disabled"
            ok = inst.enable(name)
        else:
            if status in ("disabled", "builtin_disabled"):
                return ToolResult(True, content=f"{name} 已是禁用状态，未做改动")
            was_builtin = False
            _unload_ui_on_main(name)
            ok = inst.disable(name)
    except Exception as e:
        logger.warning(f"[MarketplaceTool] 启停 {name} 失败: {e}")
        return ToolResult(False, error=f"{'启用' if enabled else '禁用'} {name} 失败: {e}")

    if not ok:
        return ToolResult(False, error=f"{'启用' if enabled else '禁用'} {name} 失败（详见日志）")
    if enabled:
        # 用户插件 enable 内部已精准重载；内置插件走配置启用，不触发重载，这里补一次
        if was_builtin:
            _reload_plugin_on_main(name, "enabled")
        return ToolResult(True, content=f"已启用 {name}。")
    return ToolResult(
        True,
        content=f"已禁用 {name}。其工具/命令从下一轮请求起不再出现。",
    )


def _detail_impl(tool_ctx, **kwargs):
    name = str(kwargs.get("plugin") or "").strip()
    if not name:
        return ToolResult(False, error="缺少 plugin 参数：请传市场返回的精确插件名")
    meta = _find_market_plugin(name, force=False) or _find_market_plugin(name, force=True)
    if meta:
        name = str(meta.get("name") or name)
    try:
        inst = _installer()
        status = inst.get_status_map().get(name)
        local_ver = inst.get_installed_version(name)
        manifests = inst.get_manifest_map()
    except Exception as e:
        logger.warning(f"[MarketplaceTool] 查询 {name} 状态失败: {e}")
        status, local_ver, manifests = None, None, {}
    if meta is None and not status:
        return ToolResult(
            False,
            error=f"市场与本地都没有名为 {name} 的插件。先 marketplace_search 确认精确插件名",
        )

    # 市场元数据优先（含 min_host_version / platforms / 下载量），本地 manifest 补缺
    manifest = manifests.get(name) or {}
    info = dict(manifest)
    if meta:
        info.update({k: v for k, v in meta.items() if v not in (None, "", [], {})})

    ver = str(info.get("version") or local_ver or "?")
    state = _install_state(
        name,
        {name: status} if status else {},
        {name: local_ver} if local_ver else {},
        ver if meta else "",
    )
    lines = [f"插件 {_display_name(info)} v{ver} | {state}"]
    desc = _shorten(info.get("description"), limit=2000)
    if desc:
        lines.append(f"描述：{desc}")

    facts: List[str] = []
    author = info.get("author")
    if isinstance(author, dict):
        author = author.get("name") or ""
    if author:
        facts.append(f"作者 {author}")
    if info.get("license"):
        facts.append(f"许可 {info['license']}")
    if info.get("categories"):
        facts.append("分类 " + "/".join(str(c) for c in info["categories"]))
    if info.get("keywords"):
        facts.append("关键词 " + ", ".join(str(k) for k in list(info["keywords"])[:8]))
    if info.get("downloads") is not None:
        facts.append(f"下载 {info['downloads']}")
    if facts:
        lines.append(" | ".join(facts))

    compat = _compat_notes(info)
    lines.append("兼容：" + ("；".join(compat) if compat else "平台与宿主版本均满足"))

    comps = info.get("components")
    if isinstance(comps, dict):
        active = [k for k, v in comps.items() if v]
        if active:
            lines.append("组件：" + ", ".join(active))

    deps = info.get("dependencies")
    if isinstance(deps, dict):
        pip = deps.get("pip")
        dep_bits: List[str] = []
        if isinstance(pip, dict):
            try:
                from app.plugins.deps_loader import current_platform_key

                key = current_platform_key()
            except Exception:
                key = ""
            for label, vals in (("通用", pip.get("default") or []), (key, pip.get(key) or [] if key else [])):
                if vals:
                    dep_bits.append(f"{label}: " + ", ".join(str(v) for v in vals[:12]))
        if dep_bits:
            lines.append("依赖：" + "；".join(dep_bits))

    src = info.get("source")
    if isinstance(src, dict):
        loc = str(src.get("url") or src.get("repo") or "")
        extra = []
        if src.get("path"):
            extra.append(f"path={src['path']}")
        if src.get("ref"):
            extra.append(f"ref={src['ref']}")
        lines.append(("来源：" + " ".join([str(src.get("type") or ""), loc] + extra)).strip())
    if info.get("homepage"):
        lines.append(f"官网：{info['homepage']}")

    local_dir = _installed_dir(name)
    if local_dir:
        lines.append(f"本地目录：{local_dir}")
        lines.append("需要 README/源码细节可直接读该目录下的文件。")
    else:
        lines.append("未安装。安装用 marketplace_install（参数传上面的插件名）。")
    return ToolResult(True, content="\n".join(lines))

# ── schema ────────────────────────────────────────────

_COMMON_PLUGIN_PARAM = {
    "type": "string",
    "description": "插件名，与搜索/列出的名字完全一致",
}

_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "marketplace_search",
        "description": (
            "按关键词（匹配名/描述/分类/标签）搜索插件市场，返回版本、安装状态、可更新标记。安装前先搜索拿精确插件名。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "关键词（名/功能/作者/标签），省略按下载量列全部"},
                "category": {"type": "string", "description": "分类过滤"},
                "limit": {"type": "integer", "description": "条数上限，默认 20，最大 50"},
                "force": {"type": "boolean", "description": "true=跳过 1 小时缓存，重新拉取市场"},
            },
        },
    },
}

_INSTALLED_SCHEMA = {
    "type": "function",
    "function": {
        "name": "marketplace_installed",
        "description": (
            "列出已安装插件的名、版本、启停状态、市场新版本。卸载/更新/启停前先查精确名。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "按插件名过滤"},
                "check_update": {"type": "boolean", "description": "检查市场新版本，默认 true；false 省一次请求"},
            },
        },
    },
}

_DETAIL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "marketplace_detail",
        "description": (
            "查看单个插件详情：完整描述、作者/许可、分类关键词、组件清单、依赖、"
            "平台与宿主版本兼容性、安装状态、本地目录。安装前判断插件是否符合需求时先看详情。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"plugin": _COMMON_PLUGIN_PARAM},
            "required": ["plugin"],
        },
    },
}

_INSTALL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "marketplace_install",
        "description": (
            "从市场装插件到用户插件目录，自动装 dependencies.pip 依赖。耗时 5~60 秒。已装则直接返回状态。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"plugin": _COMMON_PLUGIN_PARAM},
            "required": ["plugin"],
        },
    },
}

_UNINSTALL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "marketplace_uninstall",
        "description": (
            "卸载用户插件（删目录，plugin_data 保留）。系统/内置插件只能禁用。破坏性操作，需用户确认。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"plugin": _COMMON_PLUGIN_PARAM},
            "required": ["plugin"],
        },
    },
}

_UPDATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "marketplace_update",
        "description": (
            "更新到市场最新版，先下载后替换，失败留旧版。内置/系统插件更新装用户目录影子副本，删副本回退内置版。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"plugin": _COMMON_PLUGIN_PARAM},
            "required": ["plugin"],
        },
    },
}

_SET_ENABLED_SCHEMA = {
    "type": "function",
    "function": {
        "name": "marketplace_set_enabled",
        "description": (
            "启用/禁用已安装插件（用户插件移目录，内置插件写配置）。核心插件拒绝禁用。破坏性操作，需用户确认。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "plugin": _COMMON_PLUGIN_PARAM,
                "enabled": {"type": "boolean", "description": "true=启用，false=禁用"},
            },
            "required": ["plugin", "enabled"],
        },
    },
}


# ── 预览/摘要 ──────────────────────────────────────────

def _preview_search(tool_args: dict) -> str:
    kw = str(tool_args.get("keyword") or "").strip()
    return f'搜索插件 "{kw}"' if kw else "列出插件市场"


def _preview_installed(tool_args: dict) -> str:
    kw = str(tool_args.get("keyword") or "").strip()
    return f'已安装插件（筛 "{kw}"）' if kw else "列出已安装插件"


def _preview_plugin(tool_args: dict, verb: str) -> str:
    return f"{verb}插件 {tool_args.get('plugin', '')}"


def _preview_set_enabled(tool_args: dict) -> str:
    verb = "启用" if tool_args.get("enabled") else "禁用"
    return f"{verb}插件 {tool_args.get('plugin', '')}"


def _preview_detail(tool_args: dict) -> str:
    return f"插件详情 {tool_args.get('plugin', '')}"


def register(registry):
    registry.register(
        "marketplace_search",
        _SEARCH_SCHEMA,
        impl=_search_impl,
        danger="safe",
        icon=_ICON,
        cn_name="市场搜索",
        group=_GROUP,
        description="搜索插件市场",
        render_mode="inline",
        preview=_preview_search,
        summarize=make_summarize_from_preview(_preview_search),
    )
    registry.register(
        "marketplace_installed",
        _INSTALLED_SCHEMA,
        impl=_installed_impl,
        danger="safe",
        icon=_ICON,
        cn_name="已装插件",
        group=_GROUP,
        description="列出已安装插件",
        render_mode="inline",
        preview=_preview_installed,
        summarize=make_summarize_from_preview(_preview_installed),
    )
    registry.register(
        "marketplace_detail",
        _DETAIL_SCHEMA,
        impl=_detail_impl,
        danger="safe",
        icon=_ICON,
        cn_name="插件详情",
        group=_GROUP,
        description="查看单个插件完整信息",
        render_mode="inline",
        preview=_preview_detail,
        summarize=make_summarize_from_preview(_preview_detail),
    )
    registry.register(
        "marketplace_install",
        _INSTALL_SCHEMA,
        impl=_install_impl,
        danger="dangerous",
        icon=_ICON,
        cn_name="安装插件",
        group=_GROUP,
        description="从市场安装插件",
        preview=lambda a: _preview_plugin(a, "安装"),
        summarize=make_summarize_from_preview(lambda a: _preview_plugin(a, "安装")),
    )
    registry.register(
        "marketplace_uninstall",
        _UNINSTALL_SCHEMA,
        impl=_uninstall_impl,
        danger="dangerous",
        icon=_ICON,
        cn_name="卸载插件",
        group=_GROUP,
        description="卸载已安装插件",
        preview=lambda a: _preview_plugin(a, "卸载"),
        summarize=make_summarize_from_preview(lambda a: _preview_plugin(a, "卸载")),
    )
    registry.register(
        "marketplace_update",
        _UPDATE_SCHEMA,
        impl=_update_impl,
        danger="dangerous",
        icon=_ICON,
        cn_name="更新插件",
        group=_GROUP,
        description="更新插件到最新版",
        preview=lambda a: _preview_plugin(a, "更新"),
        summarize=make_summarize_from_preview(lambda a: _preview_plugin(a, "更新")),
    )
    registry.register(
        "marketplace_set_enabled",
        _SET_ENABLED_SCHEMA,
        impl=_set_enabled_impl,
        danger="dangerous",
        icon=_ICON,
        cn_name="启停插件",
        group=_GROUP,
        description="启用/禁用插件",
        preview=_preview_set_enabled,
        summarize=make_summarize_from_preview(_preview_set_enabled),
    )

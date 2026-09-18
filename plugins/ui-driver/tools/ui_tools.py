# -*- coding: utf-8 -*-
"""UI 驱动插件工具层（S3c）：AI 闭环观测与操作 DriFox 界面。

消费 tools/ui_driver 驱动库（S3a），首批 5 工具：
- ui_inspect   safe     控件查询（find / tree）
- ui_state     safe     状态快照（会话/批次/卡/池/配额/懒队列）
- ui_memory    safe     内存采样（Private 口径 + WebEngine 子进程 RSS + 容器计数）
- ui_screenshot safe    grab 截图 → 临时文件路径 + 尺寸（不塞 base64）
- ui_click     dangerous 点击控件（QAbstractButton 语义）

三重安全闸（全部通过才注册工具）：
1. 插件开关 enabled（config_schema 默认 false，false 时 register 直接 return）
2. 环境变量 DRIFOX_UI_DRIVER=1
3. ARM 总闸（tools.ui_driver.setArmed；仅 auto_confirm+auto_arm 双开时启动自动置位，
   否则需外部测试启动器/显式命令置位——后续批次将含作用域校验）

打包态守卫：打包产物无 tools/ 目录，``import tools.ui_driver`` 失败时
logger.warning 并禁用全部工具注册，不影响插件系统。
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Optional

from loguru import logger


# ── 打包态守卫：import 失败 = 打包产物（无 tools/），禁用全部工具注册 ──
def _driver_unavailable(*_args, **_kwargs):  # pragma: no cover — 闸 0 保证不可达
    raise RuntimeError(f"tools.ui_driver 不可用：{_DRIVER_IMPORT_ERROR}")


try:  # noqa: SIM105 — 需要区分「import 成功与否」而非吞掉一切
    from tools.ui_driver import find as _driver_find
    from tools.ui_driver import setArmed as _driver_setArmed
    from tools.ui_driver import tree as _driver_tree

    _DRIVER_IMPORT_ERROR: Optional[str] = None
except Exception as _exc:  # noqa: BLE001 — ImportError 及其环境变体
    _driver_find = _driver_unavailable
    _driver_tree = _driver_unavailable
    _driver_setArmed = _driver_unavailable
    _DRIVER_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

_PLUGIN_NAME = "ui-driver"
_GROUP = "UI测试"
_TMP_SCREENSHOT_DIR = os.path.join(tempfile.gettempdir(), "ui_driver_shots")


def _config_get(key: str) -> Any:
    """读插件配置（环境变量 → plugin_data/<name>/config.json → schema 默认）。"""
    try:
        from app.plugins.managers.plugin_config_store import PluginConfigStore

        return PluginConfigStore().get(_PLUGIN_NAME, key)
    except Exception as exc:  # noqa: BLE001 — 配置读不到按 False 兜底
        logger.warning(f"[ui-driver] 配置读取失败，{key} 按 False 兜底: {exc}")
        return False


def _dump(ok: bool, data: Any, hint: str) -> str:
    """统一紧凑 JSON 返回：{ok, data, hint}（S1 闭环协议）。"""
    return json.dumps({"ok": ok, "data": data, "hint": hint}, ensure_ascii=False, default=str)


# ── 工具实现 ──


def _ui_inspect_impl(tool_ctx, **kwargs):
    mode = kwargs.get("mode", "tree")
    selector = kwargs.get("selector") or {}
    root_object_name = kwargs.get("root_object_name")
    depth = int(kwargs.get("depth", 2) or 2)
    try:
        root = _driver_find({"objectName": root_object_name}) if root_object_name else None
        if root_object_name and root is None:
            return _dump(False, None, f"未找到 root_object_name={root_object_name}")
        if mode == "find":
            hit = _driver_find(selector, root=root)
            if hit is None:
                return _dump(True, {"found": False}, "未命中；可用 ui_inspect mode=tree 查看结构")
            data = {"found": True, "cls": type(hit).__name__, "objectName": hit.objectName()}
            return _dump(True, data, "命中；可用 ui_click 提供该控件 objectName/text 点击")
        snap = _driver_tree(root=root, depth=max(1, min(depth, 4)))
        return _dump(True, snap, "树已按深度截断；children_count 大的节点可用 ui_inspect mode=find 精查")
    except Exception as exc:  # noqa: BLE001
        return _dump(False, None, f"{type(exc).__name__}: {exc}")


def _ui_state_impl(tool_ctx, **kwargs):
    try:
        return _dump(True, _driver_observe_state(), "可用 ui_memory 看内存；ui_inspect 看控件")
    except Exception as exc:  # noqa: BLE001
        return _dump(False, None, f"{type(exc).__name__}: {exc}")


def _driver_observe_state():
    from tools.ui_driver import state

    return state()


def _driver_observe_memory():
    from tools.ui_driver import memory

    return memory()


def _ui_memory_impl(tool_ctx, **kwargs):
    try:
        return _dump(True, _driver_observe_memory(), "Private 口径（=T1b/T2 排查口径）；可间隔采样对比增量")
    except Exception as exc:  # noqa: BLE001
        return _dump(False, None, f"{type(exc).__name__}: {exc}")


def _ui_screenshot_impl(tool_ctx, **kwargs):
    from tools.ui_driver import screenshot

    target_object_name = kwargs.get("target_object_name")
    max_width = int(kwargs.get("max_width", 1280) or 1280)
    try:
        target = _driver_find({"objectName": target_object_name}) if target_object_name else None
        data = screenshot(target, max_width=max_width)
        if not data:
            return _dump(False, None, "grab 失败或无可截图窗口")
        os.makedirs(_TMP_SCREENSHOT_DIR, exist_ok=True)
        path = os.path.join(_TMP_SCREENSHOT_DIR, f"ui_shot_{int(__import__('time').time() * 1000)}.png")
        with open(path, "wb") as f:
            f.write(data)
        from struct import unpack

        w, h = unpack(">II", data[16:24])  # PNG IHDR 尺寸
        return _dump(True, {"path": path, "width": w, "height": h, "bytes": len(data)}, "可用 read 工具查看该 PNG")
    except Exception as exc:  # noqa: BLE001
        return _dump(False, None, f"{type(exc).__name__}: {exc}")


def _ui_click_impl(tool_ctx, **kwargs):
    from tools.ui_driver import click

    selector = {k: kwargs[k] for k in ("objectName", "text", "role") if kwargs.get(k)}
    if not selector:
        return _dump(False, None, "至少提供 objectName/text/role 之一")
    try:
        hit = _driver_find(selector)
        if hit is None:
            return _dump(True, {"clicked": False}, "未找到控件；先 ui_inspect mode=tree 定位")
        if not click(hit):
            return _dump(False, {"clicked": False}, "该控件不支持 click()（非 QAbstractButton）")
        return _dump(True, {"clicked": True, "cls": type(hit).__name__}, "已点击；可用 ui_state 观察变化")
    except Exception as exc:  # noqa: BLE001
        return _dump(False, None, f"{type(exc).__name__}: {exc}")


# ── schemas ──


def _schema(name: str, description: str, properties: dict, required: Optional[list] = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        },
    }


_SCHEMAS = {
    "ui_inspect": _schema(
        "ui_inspect",
        "查询/浏览 DriFox 界面控件。mode=find 按 selector 精查单个控件；mode=tree 输出浅层控件树（token 友好，children_count 可判断子树规模）",
        {
            "mode": {"type": "string", "enum": ["find", "tree"], "description": "find=精查 / tree=浅树"},
            "selector": {
                "type": "object",
                "description": "find 的匹配条件（可组合）：objectName/cls/text/role",
                "properties": {
                    "objectName": {"type": "string"},
                    "cls": {"type": "string", "description": "类名"},
                    "text": {"type": "string", "description": "包含匹配"},
                    "role": {"type": "string"},
                },
            },
            "root_object_name": {"type": "string", "description": "限定查找根控件（可选）"},
            "depth": {"type": "integer", "description": "tree 深度，默认 2，上限 4"},
        },
    ),
    "ui_state": _schema(
        "ui_state",
        "DriFox 界面状态快照：当前会话/批次/已渲染卡/可见窗口/WebView 池/配额/懒队列/_unloaded_pids",
        {},
    ),
    "ui_memory": _schema(
        "ui_memory",
        "内存采样：主进程 Private/WS（Private 口径）+ WebEngine 子进程 RSS + 容器计数；间隔多次采样对比增量",
        {},
    ),
    "ui_screenshot": _schema(
        "ui_screenshot",
        "截取 DriFox 界面为 PNG 临时文件，返回路径与尺寸（不塞 base64）",
        {
            "target_object_name": {"type": "string", "description": "目标控件 objectName（缺省截首个可见窗口）"},
            "max_width": {"type": "integer", "description": "最大宽度（超宽下采样），默认 1280"},
        },
    ),
    "ui_click": _schema(
        "ui_click",
        "点击 DriFox 界面控件（QAbstractButton 语义）。先用 ui_inspect 定位再点击",
        {
            "objectName": {"type": "string"},
            "text": {"type": "string", "description": "控件文本包含匹配"},
            "role": {"type": "string"},
        },
    ),
}


def register(registry) -> None:
    """注册入口。三重安全闸任一未通过 → 零工具注册。"""
    # 闸 0（打包态守卫）：打包产物无 tools/，import 失败只告警不炸插件系统
    if _DRIVER_IMPORT_ERROR is not None:
        logger.warning(f"[ui-driver] tools.ui_driver 不可用（打包产物无 tools/），全部工具禁用：{_DRIVER_IMPORT_ERROR}")
        return
    # 闸 1：插件开关（默认 false）
    if not _config_get("enabled"):
        logger.debug("[ui-driver] enabled=false，跳过工具注册")
        return
    # 闸 2：环境变量
    if os.environ.get("DRIFOX_UI_DRIVER") != "1":
        logger.debug("[ui-driver] DRIFOX_UI_DRIVER != 1，跳过工具注册")
        return
    # 闸 3：ARM——默认不自动 ARM；仅 auto_confirm+auto_arm 双开时启动置位
    if _config_get("auto_confirm") and _config_get("auto_arm"):
        _driver_setArmed(True)
        logger.info("[ui-driver] auto_confirm+auto_arm 双开 → 已自动 ARM")
    else:
        logger.info("[ui-driver] 未自动 ARM：操作前需外部测试启动器/显式命令 setArmed(True)")

    registry.register(
        "ui_inspect",
        _SCHEMAS["ui_inspect"],
        impl=_ui_inspect_impl,
        danger="safe",
        icon="工具",
        cn_name="UI检查",
        group=_GROUP,
        description="查询/浏览界面控件（find/tree）",
        aliases=["UiInspect", "ui_inspect"],
    )
    registry.register(
        "ui_state",
        _SCHEMAS["ui_state"],
        impl=_ui_state_impl,
        danger="safe",
        icon="工具",
        cn_name="UI状态",
        group=_GROUP,
        description="界面状态快照（会话/批次/卡/池/配额）",
        aliases=["UiState", "ui_state"],
    )
    registry.register(
        "ui_memory",
        _SCHEMAS["ui_memory"],
        impl=_ui_memory_impl,
        danger="safe",
        icon="工具",
        cn_name="UI内存",
        group=_GROUP,
        description="内存采样（Private + WebEngine 子进程 RSS）",
        aliases=["UiMemory", "ui_memory"],
    )
    registry.register(
        "ui_screenshot",
        _SCHEMAS["ui_screenshot"],
        impl=_ui_screenshot_impl,
        danger="safe",
        icon="工具",
        cn_name="UI截图",
        group=_GROUP,
        description="界面截图（PNG 临时文件）",
        aliases=["UiScreenshot", "ui_screenshot"],
    )
    registry.register(
        "ui_click",
        _SCHEMAS["ui_click"],
        impl=_ui_click_impl,
        danger="dangerous",
        icon="工具",
        cn_name="UI点击",
        group=_GROUP,
        description="点击界面控件",
        aliases=["UiClick", "ui_click"],
    )

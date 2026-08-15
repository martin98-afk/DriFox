# -*- coding: utf-8 -*-
"""
系统工具插件 — 桌面自动化（自包含实现）

mouse / keyboard / screenshot 完全自包含：pynput + mss 直接实现。
运行环境从 tool_ctx 获取（app_data_dir 截图目录、desktop_automation 开关）。
"""
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from loguru import logger
from pynput import keyboard as _pynput_kb
from pynput.mouse import Button
from pynput.mouse import Controller as MouseController
import mss
import mss.tools

from app.tools.result import ToolResult
from app.tools.registry import make_summarize_from_preview

GROUP_DESKTOP = "桌面控制"

_MOUSE_ACTIONS = {"move", "click", "double_click", "right_click", "scroll", "drag", "position"}
# position 不需要坐标；其它 action 必须校验 x/y
_MOUSE_ACTIONS_NO_COORD = frozenset({"position"})
_MOUSE_BUTTONS = {"left": Button.left, "right": Button.right, "middle": Button.middle}


def _app_data_dir(tool_ctx) -> Path:
    """截图默认目录（<app_data>/.drifox/screenshots）"""
    app_data = tool_ctx.get("env", {}).get("app_data_dir")
    if app_data:
        return Path(app_data) / ".drifox" / "screenshots"
    return Path.home() / ".drifox" / "screenshots"


def _automation_enabled(tool_ctx) -> bool:
    """桌面自动化开关：配置缺失时默认启用（设置 → 桌面自动化）"""
    return tool_ctx.get("env", {}).get("desktop_automation_enabled", True)


def _check_enabled(tool_ctx) -> Optional[ToolResult]:
    if not _automation_enabled(tool_ctx):
        return ToolResult(False, error="桌面自动化未开启（设置 → 桌面自动化）")
    return None


# ========== screenshot ==========

_SCREENSHOT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "screenshot",
        "description": "截屏并保存PNG。支持全屏或区域截图。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "输出PNG路径(可选，空则自动生成到.drifox/screenshots/)"},
                "region": {
                    "type": "array",
                    "description": "区域(left,top,width,height)如[100,200,800,600]；空=全屏",
                    "items": {"type": "integer"},
                    "minItems": 4,
                    "maxItems": 4,
                },
            },
        },
    },
}


def _screenshot_impl(tool_ctx, **kwargs):
    err = _check_enabled(tool_ctx)
    if err:
        return err
    path = kwargs.get("path") or ""
    region = kwargs.get("region") or None
    try:
        with mss.mss() as sct:
            if region is not None:
                if len(region) != 4:
                    return ToolResult(False, error="region must be (left, top, width, height)")
                left, top, width, height = region
                monitor = {"left": int(left), "top": int(top), "width": int(width), "height": int(height)}
            else:
                monitor = sct.monitors[1]  # 主显示器
            raw = sct.grab(monitor)
            # 输出路径
            if path:
                out_path = Path(path).expanduser()
                if not out_path.is_absolute():
                    out_path = Path.cwd() / out_path
                out_path.parent.mkdir(parents=True, exist_ok=True)
            else:
                save_dir = _app_data_dir(tool_ctx)
                save_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                out_path = save_dir / f"desktop_{stamp}.png"
            mss.tools.to_png(raw.rgb, raw.size, output=str(out_path))
            size_bytes = out_path.stat().st_size
            width, height = raw.size
            return ToolResult(
                True,
                content={
                    "path": str(out_path),
                    "absolute_path": str(out_path.resolve()),
                    "width": width,
                    "height": height,
                    "size_bytes": size_bytes,
                    "markdown": f"![截图]({out_path.as_posix()})",
                },
            )
    except Exception as e:
        return ToolResult(False, error=f"Screenshot error: {str(e)}")


# ========== mouse ==========

_MOUSE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "mouse",
        "description": "桌面鼠标操作。支持移动/单击/双击/右键/滚动/拖拽/查位置。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["move", "click", "double_click", "right_click", "scroll", "drag", "position"],
                    "description": "move=移动,click=单击,double_click=双击,right_click=右键,scroll=滚动,drag=拖到(x,y),position=查坐标+屏幕尺寸",
                },
                "x": {"type": "integer", "description": "目标屏幕 X 坐标（像素）"},
                "y": {"type": "integer", "description": "目标屏幕 Y 坐标（像素）"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "鼠标按钮(默认left)"},
                "clicks": {"type": "integer", "description": "点击次数(默认1),double_click固定2次"},
                "dx": {"type": "integer", "description": "scroll水平滚动"},
                "dy": {"type": "integer", "description": "scroll垂直滚动(负上正下)"},
                "duration": {"type": "number", "description": "move/drag过渡秒数；move默认0瞬移，drag默认0.3"},
            },
            "required": ["action"],
        },
    },
}


def _mouse_impl(tool_ctx, **kwargs):
    err = _check_enabled(tool_ctx)
    if err:
        return err
    action = kwargs.get("action", "")
    x = int(kwargs.get("x") or 0)
    y = int(kwargs.get("y") or 0)
    button = kwargs.get("button", "left")
    clicks = int(kwargs.get("clicks") or 1)
    dx = int(kwargs.get("dx") or 0)
    dy = int(kwargs.get("dy") if kwargs.get("dy") is not None else -1)
    duration = float(kwargs.get("duration") or 0)
    try:
        if action not in _MOUSE_ACTIONS:
            return ToolResult(False, error=f"Unknown action: {action!r}. Valid: {sorted(_MOUSE_ACTIONS)}")
        # position 不需要坐标；其它 action 必须提供 x/y
        if action not in _MOUSE_ACTIONS_NO_COORD:
            if not x and not y:
                return ToolResult(False, error="move/click/drag 需要 x/y 坐标")
        mouse = MouseController()
        if action == "position":
            pos = mouse.position
            result = {"x": pos[0], "y": pos[1]}
            try:
                with mss.mss() as sct:
                    result["screen_width"] = sct.monitors[1]["width"]
                    result["screen_height"] = sct.monitors[1]["height"]
                    result["monitors"] = [
                        {"left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]}
                        for m in sct.monitors[1:]
                    ]
            except Exception:
                pass
            return ToolResult(True, content=result)
        if action == "move":
            mouse.position = (x, y)
            if duration > 0:
                time.sleep(duration)
            return ToolResult(True, content=f"鼠标移动到 ({x}, {y})")
        if action == "click":
            mouse.position = (x, y)
            btn = _MOUSE_BUTTONS.get(button, Button.left)
            for _ in range(clicks):
                mouse.click(btn)
                time.sleep(0.05)
            return ToolResult(True, content=f"点击 ({x}, {y}) {clicks} 次 [{button}]")
        if action == "double_click":
            mouse.position = (x, y)
            mouse.click(_MOUSE_BUTTONS.get(button, Button.left), 2)
            return ToolResult(True, content=f"双击 ({x}, {y})")
        if action == "right_click":
            mouse.position = (x, y)
            mouse.click(Button.right)
            return ToolResult(True, content=f"右键点击 ({x}, {y})")
        if action == "scroll":
            mouse.scroll(dx, dy)
            return ToolResult(True, content=f"滚动 dx={dx} dy={dy}")
        if action == "drag":
            mouse.position = (x, y)
            btn = _MOUSE_BUTTONS.get(button, Button.left)
            mouse.press(btn)
            time.sleep(duration or 0.3)
            mouse.position = (x + dx, y + dy)
            time.sleep(0.1)
            mouse.release(btn)
            return ToolResult(True, content=f"拖拽 ({x}, {y}) → ({x + dx}, {y + dy})")
        return ToolResult(False, error=f"Unhandled action: {action}")
    except Exception as e:
        return ToolResult(False, error=f"Mouse error: {str(e)}")


# ========== keyboard ==========

_KEYBOARD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "keyboard",
        "description": "桌面键盘操作。支持打字/按单键/组合热键。需先开启桌面自动化。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["type", "press", "hotkey"], "description": "type=输入文本,press=按单键,hotkey=组合热键"},
                "text": {"type": "string", "description": "type 操作要输入的文本（支持 Unicode）"},
                "key": {"type": "string", "description": "单键名: enter/f5/ctrl_l/esc/tab"},
                "keys": {"type": "string", "description": "组合键用+连接: ctrl+c,ctrl+shift+n"},
            },
            "required": ["action"],
        },
    },
}

# pynput 键名映射（常用键）
_KEY_ALIASES = {
    "enter": _pynput_kb.Key.enter, "return": _pynput_kb.Key.enter,
    "esc": _pynput_kb.Key.esc, "escape": _pynput_kb.Key.esc,
    "tab": _pynput_kb.Key.tab,
    "space": _pynput_kb.Key.space, "backspace": _pynput_kb.Key.backspace,
    "delete": _pynput_kb.Key.delete, "del": _pynput_kb.Key.delete,
    "insert": _pynput_kb.Key.insert,
    "home": _pynput_kb.Key.home, "end": _pynput_kb.Key.end,
    "page_up": _pynput_kb.Key.page_up, "page_down": _pynput_kb.Key.page_down,
    "up": _pynput_kb.Key.up, "down": _pynput_kb.Key.down,
    "left": _pynput_kb.Key.left, "right": _pynput_kb.Key.right,
    "f1": _pynput_kb.Key.f1, "f2": _pynput_kb.Key.f2, "f3": _pynput_kb.Key.f3,
    "f4": _pynput_kb.Key.f4, "f5": _pynput_kb.Key.f5, "f6": _pynput_kb.Key.f6,
    "f7": _pynput_kb.Key.f7, "f8": _pynput_kb.Key.f8, "f9": _pynput_kb.Key.f9,
    "f10": _pynput_kb.Key.f10, "f11": _pynput_kb.Key.f11, "f12": _pynput_kb.Key.f12,
    "ctrl": _pynput_kb.Key.ctrl, "ctrl_l": _pynput_kb.Key.ctrl_l, "ctrl_r": _pynput_kb.Key.ctrl_r,
    "shift": _pynput_kb.Key.shift, "shift_l": _pynput_kb.Key.shift_l, "shift_r": _pynput_kb.Key.shift_r,
    "alt": _pynput_kb.Key.alt, "alt_l": _pynput_kb.Key.alt_l, "alt_r": _pynput_kb.Key.alt_r,
    "cmd": _pynput_kb.Key.cmd, "win": _pynput_kb.Key.cmd,
    "caps_lock": _pynput_kb.Key.caps_lock,
}


def _resolve_key(name: str):
    """解析键名（别名 → pynput Key；否则按字符键）"""
    name_lower = name.lower()
    if name_lower in _KEY_ALIASES:
        return _KEY_ALIASES[name_lower]
    if len(name) == 1:
        return name
    return name  # pynput 可处理部分特殊字符


def _keyboard_impl(tool_ctx, **kwargs):
    err = _check_enabled(tool_ctx)
    if err:
        return err
    action = kwargs.get("action", "")
    text = kwargs.get("text") or ""
    key = kwargs.get("key") or ""
    keys = kwargs.get("keys") or ""
    try:
        kb = _pynput_kb.Controller()
        if action == "type":
            if not text:
                return ToolResult(False, error="type 操作需要 text 参数")
            kb.type(text)
            return ToolResult(True, content=f"已输入 {len(text)} 字符")
        if action == "press":
            if not key:
                return ToolResult(False, error="press 操作需要 key 参数")
            kb.press(_resolve_key(key))
            time.sleep(0.05)
            kb.release(_resolve_key(key))
            return ToolResult(True, content=f"已按下 {key}")
        if action == "hotkey":
            if not keys:
                return ToolResult(False, error="hotkey 操作需要 keys 参数（如 ctrl+c）")
            parts = [_resolve_key(p.strip()) for p in keys.split("+") if p.strip()]
            if not parts:
                return ToolResult(False, error="无效组合键")
            for p in parts:
                kb.press(p)
            time.sleep(0.05)
            for p in reversed(parts):
                kb.release(p)
            return ToolResult(True, content=f"已执行组合键 {keys}")
        return ToolResult(False, error=f"Unknown action: {action!r}. Valid: type/press/hotkey")
    except Exception as e:
        return ToolResult(False, error=f"Keyboard error: {str(e)}")


def _render_screenshot_body(result, tool_name, tool_args, success):
    """screenshot 完成框渲染闭包：直接展示截图图片

    从主程序 render_helpers 的截图分支迁出；图片路径从结果提取
    （兼容 dict 字符串 / 正则），无图片时回退默认渲染。
    """
    from app.widgets.render_helpers import _extract_screenshot_image_path, escape

    raw = getattr(result, "content", "") or ""
    if isinstance(raw, dict):
        img_path = raw.get("absolute_path") or raw.get("path") or ""
    else:
        img_path = _extract_screenshot_image_path(str(raw))
    if not img_path:
        return None
    return f'''
    <div class="screenshot-preview" style="margin: 0; padding: 0;">
        <img src="{escape(img_path)}" style="width: 100%; height: auto; display: block; border-radius: 8px;" alt="Screenshot" />
    </div>'''


def _preview_screenshot(tool_args: dict) -> str:
    region = tool_args.get("region")
    if region and isinstance(region, (list, tuple)) and len(region) == 4:
        return f"截取屏幕 ({region[2]}×{region[3]})"
    return "截取屏幕"


def _preview_mouse(tool_args: dict) -> str:
    action = tool_args.get("action", "")
    x = tool_args.get("x", "")
    y = tool_args.get("y", "")
    action_labels = {
        "move": "移动",
        "click": "点击",
        "double_click": "双击",
        "right_click": "右键",
        "scroll": "滚动",
        "drag": "拖拽",
        "position": "查询位置",
    }
    action_label = action_labels.get(action, action or "操作")
    if action == "position":
        return "查询鼠标位置"
    if x != "" and y != "":
        return f"鼠标{action_label} ({x}, {y})"
    return f"鼠标{action_label}"


def _preview_keyboard(tool_args: dict) -> str:
    action = tool_args.get("action", "")
    if action == "type":
        text = tool_args.get("text", "")
        preview = text[:30] + ("…" if len(text) > 30 else "")
        return f'键盘输入 "{preview}"' if preview else "键盘输入"
    if action == "press":
        key = tool_args.get("key", "")
        return f"按键 {key}" if key else "按键"
    if action == "hotkey":
        keys = tool_args.get("keys", "")
        return f"热键 {keys}" if keys else "热键"
    return "键盘操作"


def register(registry):
    registry.register(
        "screenshot", _SCREENSHOT_SCHEMA, impl=_screenshot_impl,
        danger="safe", icon="裁剪", cn_name="截图",
        group=GROUP_DESKTOP, description="截取屏幕截图",
        render_mode="expand",  # 图片直接展示，禁用折叠框
        render=_render_screenshot_body,
        preview=_preview_screenshot,
        summarize=make_summarize_from_preview(_preview_screenshot),
        # 视觉注入声明：结果可解析出本地图片路径（协议 A），供 chat_worker 视觉模型注入
        metadata={"provides_image": True},
    )
    registry.register(
        "mouse", _MOUSE_SCHEMA, impl=_mouse_impl,
        danger="dangerous", icon="鼠标", cn_name="鼠标",
        group=GROUP_DESKTOP, description="鼠标操作",
        aliases=["Mouse"],
        preview=_preview_mouse,
        summarize=make_summarize_from_preview(_preview_mouse),
    )
    registry.register(
        "keyboard", _KEYBOARD_SCHEMA, impl=_keyboard_impl,
        danger="dangerous", icon="233键盘-线性", cn_name="键盘",
        group=GROUP_DESKTOP, description="键盘操作",
        aliases=["Keyboard"],
        preview=_preview_keyboard,
        summarize=make_summarize_from_preview(_preview_keyboard),
    )

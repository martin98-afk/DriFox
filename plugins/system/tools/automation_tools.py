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


def register(registry):
    registry.register(
        "screenshot", _SCREENSHOT_SCHEMA, impl=_screenshot_impl,
        danger="safe", icon="裁剪", cn_name="截图",
        group=GROUP_DESKTOP, description="截取屏幕截图",
    )
    registry.register(
        "mouse", _MOUSE_SCHEMA, impl=_mouse_impl,
        danger="dangerous", icon="鼠标", cn_name="鼠标",
        group=GROUP_DESKTOP, description="鼠标操作",
        aliases=["Mouse"],
    )
    registry.register(
        "keyboard", _KEYBOARD_SCHEMA, impl=_keyboard_impl,
        danger="dangerous", icon="233键盘-线性", cn_name="键盘",
        group=GROUP_DESKTOP, description="键盘操作",
        aliases=["Keyboard"],
    )

# -*- coding: utf-8 -*-
"""
桌面自动化工具集 - 轻量级键鼠操控 (pynput + mss)

3 个核心工具, 统一管理:
  1. mouse       - 鼠标操作 (move/click/double_click/right_click/scroll/drag/position)
  2. keyboard    - 键盘操作 (type/press/hotkey)
  3. screenshot  - 截屏 (全屏 / 指定区域 / 默认输出到 .drifox/screenshots/)

设计原则:
  - 总开关: Settings.llm_desktop_automation_enabled 默认 False
  - 紧急停止: Ctrl+Alt+Esc 全局热键 (pynput.GlobalHotKeys)
  - 操作日志: 所有动作写入 llm_chatter.log 便于审计
  - 体积: 依赖 pynput (~50KB) + mss (~30KB), 比 pyautogui 小 98%
  - 平台: Windows / macOS / Linux (macOS 首次需授权 Accessibility)
"""
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from loguru import logger

from app.tools.result import ToolResult

try:
    from pynput import keyboard as _kb_module
    from pynput.mouse import Button
    from pynput.mouse import Controller as MouseController
    _HAS_PYNPUT = True
except ImportError:  # 依赖未装时不崩溃, 仅工具不可用
    _HAS_PYNPUT = False
    MouseController = None  # type: ignore
    Button = None  # type: ignore
    _kb_module = None  # type: ignore

try:
    import mss
    import mss.tools
    _HAS_MSS = True
except ImportError:
    _HAS_MSS = False


# ============ 工具枚举与映射 ============
_MOUSE_ACTIONS = frozenset({
    "move", "click", "double_click", "right_click", "scroll", "drag",
    "position",
})
# 不需要 (x, y) 入参的 action: 跳过 _validate_xy 检查
_MOUSE_ACTIONS_NO_COORD = frozenset({"position"})
_KEYBOARD_ACTIONS = frozenset({"type", "press", "hotkey"})

# 单键字符串 -> pynput Key 枚举的映射 (常用键, 其它走 Key[key] 动态查)
_SPECIAL_KEY_MAP = {
    "enter": "enter", "return": "enter",
    "esc": "esc", "escape": "esc",
    "tab": "tab", "space": "space", " ": "space",
    "backspace": "backspace", "delete": "delete", "del": "delete",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end", "page_up": "page_up", "page_down": "page_down",
    "ctrl": "ctrl_l", "control": "ctrl_l",
    "alt": "alt_l", "shift": "shift_l", "cmd": "cmd", "win": "cmd",
    "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4", "f5": "f5",
    "f6": "f6", "f7": "f7", "f8": "f8", "f9": "f9",
    "f10": "f10", "f11": "f11", "f12": "f12",
}

# 坐标安全阈值: 拒绝 10000 之外的坐标
_MAX_COORD = 10_000


def _resolve_key(key_str: str):
    """字符串 -> pynput Key 枚举; 普通字符直接返回 str"""
    if _kb_module is None:
        return key_str
    k = _SPECIAL_KEY_MAP.get(key_str.lower(), key_str)
    if hasattr(_kb_module.Key, k) if isinstance(k, str) else False:
        try:
            return getattr(_kb_module.Key, k)
        except AttributeError:
            pass
    return k  # 普通字符, pynput 会按字面处理

class AutomationTools:
    """桌面自动化工具集 (mouse / keyboard / screenshot)

    必须在 Settings.llm_desktop_automation_enabled = True 时才生效
    """

    # 类级共享资源：只有一个全局热键监听器，跨窗口共用
    _global_stop_listener = None  # 共享的 GlobalHotKeys 实例
    _global_stop_callback = None  # 共享回调包装器
    _stop_listener_instances: set = set()  # 所有注册的 AutomationTools 实例

    def __init__(self, owner=None):
        self._owner = owner
        self._mouse = MouseController() if _HAS_PYNPUT else None
        self._kb = _kb_module.Controller() if (_HAS_PYNPUT and _kb_module) else None
        self._emergency_stopped = False
        self._stop_listener = None  # 不再直接持有监听器
        # 显示器信息懒加载缓存; 分辨率运行期几乎不变, 不必每次重新探测
        self._screen_info_cache: Optional[dict] = None
        # 注册紧急停止热键 (Ctrl+Alt+Esc) - 全局共享
        self._register_global_stop()

    def _get_screen_info(self) -> dict:
        """获取主显示器尺寸 + 全部显示器列表 (mss 未装时返回空 dict)

        Returns:
            {
                "primary": {"width": int, "height": int},
                "monitors": [{"left", "top", "width", "height"}, ...],
            }
            或 {} (mss 不可用 / 探测失败)
        """
        if self._screen_info_cache is not None:
            return self._screen_info_cache
        if not _HAS_MSS:
            self._screen_info_cache = {}
            return self._screen_info_cache
        try:
            with mss.mss() as sct:
                # mss.monitors[0] = 全屏拼接, [1:] = 各物理显示器
                primary = sct.monitors[1]
                self._screen_info_cache = {
                    "primary": {
                        "width": int(primary["width"]),
                        "height": int(primary["height"]),
                    },
                    "monitors": [
                        {
                            "left": int(m["left"]),
                            "top": int(m["top"]),
                            "width": int(m["width"]),
                            "height": int(m["height"]),
                        }
                        for m in sct.monitors[1:]
                    ],
                }
        except Exception as e:
            logger.warning(f"[Automation] 获取屏幕信息失败: {e}")
            self._screen_info_cache = {}
        return self._screen_info_cache

    # ============== 安全闸门 ==============

    def _check_enabled(self) -> Optional[ToolResult]:
        """统一闸门: 总开关 + 紧急停止状态"""
        if not _HAS_PYNPUT:
            return ToolResult(
                False,
                error=(
                    "pynput is not installed. "
                    "Run: pip install pynput>=1.7.6"
                ),
            )
        try:
            from app.utils.config import Settings
            if not Settings.get_instance().llm_desktop_automation_enabled.value:
                return ToolResult(
                    False,
                    error=(
                        "Desktop automation is disabled. "
                        "Enable it in Settings first."
                    ),
                )
        except Exception as e:
            logger.warning(f"[Automation] 读取总开关失败, 默认拒绝: {e}")
            return ToolResult(False, error=f"Settings unavailable: {e}")

        if self._emergency_stopped:
            return ToolResult(
                False,
                error=(
                    "Emergency stop activated (Ctrl+Alt+Esc). "
                    "Re-instantiate the worker to resume."
                ),
            )
        return None

    def _register_global_stop(self) -> None:
        """注册全局共享的 Ctrl+Alt+Esc 热键监听器

        macOS 上不允许同一个进程注册多个 CGEventTapCreate 全局事件监听器，
        否则第二个 GlobalHotKeys 会触发 SIGTRAP 崩溃。
        这里使用类变量确保进程内只创建一个监听器，所有实例共享。
        """
        if not _HAS_PYNPUT or _kb_module is None:
            return

        # 注册当前实例到共享集合
        AutomationTools._stop_listener_instances.add(self)

        # 如果全局监听器已存在，不再重复创建
        if AutomationTools._global_stop_listener is not None:
            return

        try:
            # 定义一个共享回调，通知所有已注册实例
            def _shared_emergency_stop():
                logger.warning("[Automation] ⚠️ 紧急停止触发 (Ctrl+Alt+Esc)")
                for inst in AutomationTools._stop_listener_instances:
                    try:
                        inst._emergency_stopped = True
                    except Exception:
                        pass

            AutomationTools._global_stop_callback = _shared_emergency_stop
            listener = _kb_module.GlobalHotKeys({
                "<ctrl>+<alt>+<esc>": _shared_emergency_stop,
            })
            listener.daemon = True
            listener.start()
            AutomationTools._global_stop_listener = listener
            logger.info("[Automation] 紧急停止热键已注册: Ctrl+Alt+Esc")
        except Exception as e:
            logger.warning(f"[Automation] 紧急热键注册失败 (可能缺权限): {e}")

    def cleanup(self):
        """窗口关闭时从全局实例集合中注销自身（泄漏修复 P1）。

        _register_global_stop 会把实例 add 进类级集合
        AutomationTools._stop_listener_instances（供 Ctrl+Alt+Esc 紧急停止
        广播遍历），但历史上只 add 不 remove → 窗口关闭后实例仍被类级集合
        强引用，整棵窗口对象树无法回收。cleanup 在此移除自身引用，
        紧急停止广播自然跳过已关闭的实例。
        """
        AutomationTools._stop_listener_instances.discard(self)


# ============================================================
# 废弃说明：mouse/keyboard/screenshot 工具方法已删除（工具插件化 v2）
# 桌面自动化工具实现已迁移至 plugins/system/tools/automation_tools.py
# （自包含：pynput + mss 直接实现）。本类保留为紧急停止服务：
# Ctrl+Alt+Esc 全局热键（pynput.GlobalHotKeys）标记紧急停止状态，
# 供 UI/backend 查询；_get_screen_info 供 UI 坐标显示等场景使用。
# 删除历史：_validate_xy/mouse/_do_move/keyboard/screenshot（2026-08-15 移除）。
# ============================================================

# ============================================================
# 紧急停止服务（从主程序 app/tools/automation.py 迁移）
# Ctrl+Alt+Esc 全局热键：标记紧急停止状态，供 UI 查询
# 模块级单例（进程级，加载时注册热键；热重载时共享检测避免重复注册）
# ============================================================
_emergency_stop_service = AutomationTools()


def is_emergency_stopped() -> bool:
    """查询紧急停止状态（供 UI/其他服务使用）"""
    return _emergency_stop_service._emergency_stopped


def reset_emergency_stop() -> None:
    """复位紧急停止状态"""
    _emergency_stop_service._emergency_stopped = False


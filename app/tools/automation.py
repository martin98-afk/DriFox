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

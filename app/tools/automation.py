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
    from pynput.mouse import Controller as MouseController, Button
    from pynput import keyboard as _kb_module
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

    def _validate_xy(self, x, y) -> Optional[ToolResult]:
        if not (isinstance(x, int) and isinstance(y, int)):
            return ToolResult(False, error=f"x, y must be int, got ({type(x).__name__}, {type(y).__name__})")
        if not (0 <= x <= _MAX_COORD and 0 <= y <= _MAX_COORD):
            return ToolResult(False, error=f"Coordinates out of range: ({x}, {y}), max={_MAX_COORD}")
        return None

    # ============== 1) 鼠标 ==============

    def mouse(
        self,
        action: str,
        x: int = 0,
        y: int = 0,
        button: str = "left",
        clicks: int = 1,
        dx: int = 0,
        dy: int = -1,
        duration: float = 0.0,
    ) -> ToolResult:
        """统一的鼠标操作入口

        Args:
            action:  move | click | double_click | right_click | scroll | drag | position
            x, y:    目标屏幕坐标 (左上角原点, 单位像素); position 时忽略
            button:  left | right | middle  (默认 left)
            clicks:  click 操作的次数 (默认 1)
            dx, dy:  scroll 时滚动量 (dx 水平, dy 垂直, 默认 -1 向下)
            duration: move/drag 过渡时长 (秒); move 默认 0 瞬移, drag 默认 0.3

        Returns:
            ToolResult; position 操作的 content 为
            {"x": int, "y": int, "screen_width": int, "screen_height": int}
            (多显示器时附加 "monitors": [{left,top,width,height}, ...])
        """
        if (err := self._check_enabled()):
            return err
        if action not in _MOUSE_ACTIONS:
            return ToolResult(
                False,
                error=f"Unknown action: {action!r}. Valid: {sorted(_MOUSE_ACTIONS)}",
            )
        # position 不需要坐标; 其它 action 仍须校验
        if action not in _MOUSE_ACTIONS_NO_COORD:
            if (err := self._validate_xy(x, y)):
                return err

        log_extra = f"@({x},{y}) button={button}"
        try:
            if action == "position":
                cur_x, cur_y = self._mouse.position
                cur_x, cur_y = int(cur_x), int(cur_y)
                result = {"x": cur_x, "y": cur_y}
                # 附带屏幕信息: 让 LLM 能判断鼠标的相对位置
                screen = self._get_screen_info()
                if screen:
                    result["screen_width"] = screen["primary"]["width"]
                    result["screen_height"] = screen["primary"]["height"]
                    # 多显示器场景才暴露 monitors 列表, 避免单屏冗余
                    if len(screen.get("monitors", [])) > 1:
                        result["monitors"] = screen["monitors"]
                logger.info(
                    f"[Automation] mouse position -> ({cur_x}, {cur_y}) "
                    f"screen={result.get('screen_width')}x{result.get('screen_height')}"
                )
                return ToolResult(True, content=result)

            if action == "move":
                self._do_move(x, y, duration)
                return ToolResult(True, content=f"Mouse moved to ({x}, {y})")

            if action in ("click", "double_click", "right_click"):
                self._do_move(x, y, duration)
                actual_clicks = 2 if action == "double_click" else clicks
                # right_click 强制用 right 按钮, 忽略 button 参数
                effective_button = "right" if action == "right_click" else button
                actual_button = {
                    "left": Button.left, "right": Button.right, "middle": Button.middle,
                }.get(effective_button, Button.left)
                for _ in range(actual_clicks):
                    self._mouse.click(actual_button)
                verb = "double-clicked" if action == "double_click" else "clicked"
                target = (
                    f"{effective_button} x{actual_clicks}"
                    if action != "right_click" else "right"
                )
                logger.info(f"[Automation] mouse {action} {log_extra} -> {target}")
                return ToolResult(True, content=f"{verb} {target} at ({x}, {y})")

            if action == "scroll":
                self._do_move(x, y, duration)
                self._mouse.scroll(dx, dy)
                logger.info(f"[Automation] mouse scroll {log_extra} dx={dx} dy={dy}")
                return ToolResult(True, content=f"Scrolled at ({x}, {y}) dx={dx} dy={dy}")

            if action == "drag":
                # drag 语义: 从当前位置按住左键, 平滑移动到 (x, y), 然后释放
                # 必须有过渡时长, 瞬移 + press/release 会被多数 UI 识别为单击而非拖拽
                start = self._mouse.position
                target = (x, y)
                effective_duration = duration if duration > 0 else 0.3
                self._mouse.press(Button.left)
                try:
                    self._do_move(x, y, effective_duration)
                finally:
                    # 无论移动过程是否异常, 都必须释放, 避免鼠标卡在按下状态
                    self._mouse.release(Button.left)
                logger.info(
                    f"[Automation] mouse drag {start} -> {target} "
                    f"duration={effective_duration}s"
                )
                return ToolResult(True, content=f"Dragged from {start} to {target}")

            return ToolResult(False, error=f"Unhandled action: {action}")
        except Exception as e:
            logger.exception(f"[Automation] mouse {action} failed: {e}")
            return ToolResult(False, error=f"mouse {action} failed: {e}")

    def _do_move(self, x: int, y: int, duration: float) -> None:
        """带可选平滑过渡的移动"""
        if duration <= 0:
            self._mouse.position = (x, y)
            return
        start = self._mouse.position
        steps = max(1, int(duration * 60))  # 60 fps
        for i in range(1, steps + 1):
            t = i / steps
            self._mouse.position = (
                int(start[0] + (x - start[0]) * t),
                int(start[1] + (y - start[1]) * t),
            )
            time.sleep(duration / steps)

    # ============== 2) 键盘 ==============

    def keyboard(
        self,
        action: str,
        text: str = "",
        key: str = "",
        keys: str = "",
    ) -> ToolResult:
        """统一的键盘操作入口

        Args:
            action: type | press | hotkey
            text:   type 操作要输入的文本 (支持 unicode)
            key:    press 操作的单键名 (e.g. "enter", "f5", "ctrl_l")
            keys:   hotkey 操作的组合键, 用 "+" 连接 (e.g. "ctrl+c", "ctrl+shift+n")

        Returns:
            ToolResult
        """
        if (err := self._check_enabled()):
            return err
        if action not in _KEYBOARD_ACTIONS:
            return ToolResult(
                False,
                error=f"Unknown action: {action!r}. Valid: {sorted(_KEYBOARD_ACTIONS)}",
            )

        try:
            if action == "type":
                if not text:
                    return ToolResult(False, error="'text' is required for type")
                self._kb.type(text)
                logger.info(
                    f"[Automation] keyboard type {len(text)} chars: {text[:30]!r}{'...' if len(text) > 30 else ''}"
                )
                return ToolResult(True, content=f"Typed {len(text)} chars")

            if action == "press":
                if not key:
                    return ToolResult(False, error="'key' is required for press")
                k = _resolve_key(key)
                self._kb.press(k)
                self._kb.release(k)
                logger.info(f"[Automation] keyboard press {key!r}")
                return ToolResult(True, content=f"Pressed {key}")

            if action == "hotkey":
                if not keys:
                    return ToolResult(False, error="'keys' is required for hotkey (e.g. 'ctrl+c')")
                parts = [p.strip() for p in keys.split("+") if p.strip()]
                if not parts:
                    return ToolResult(False, error=f"Invalid keys: {keys!r}")
                resolved = [_resolve_key(p) for p in parts]
                for k in resolved:
                    self._kb.press(k)
                for k in reversed(resolved):
                    self._kb.release(k)
                logger.info(f"[Automation] keyboard hotkey {keys!r}")
                return ToolResult(True, content=f"Hotkey {'+'.join(parts)}")

            return ToolResult(False, error=f"Unhandled action: {action}")
        except Exception as e:
            logger.exception(f"[Automation] keyboard {action} failed: {e}")
            return ToolResult(False, error=f"keyboard {action} failed: {e}")

    # ============== 3) 截屏 ==============

    def screenshot(
        self,
        path: str = "",
        region: Optional[Tuple[int, int, int, int]] = None,
    ) -> ToolResult:
        """截屏 (默认保存到 .drifox/screenshots/desktop_*.png)

        Args:
            path:   输出 PNG 路径; 为空时使用默认目录
            region: (left, top, width, height) 截取区域; 为空时截主显示器全屏

        Returns:
            ToolResult, content 为 dict (含 path/width/height/size_bytes/markdown)
        """
        if not _HAS_MSS:
            return ToolResult(
                False,
                error="mss is not installed. Run: pip install mss>=9.0.1",
            )

        try:
            with mss.mss() as sct:
                if region is not None:
                    if len(region) != 4:
                        return ToolResult(False, error="region must be (left, top, width, height)")
                    left, top, width, height = region
                    monitor = {
                        "left": int(left), "top": int(top),
                        "width": int(width), "height": int(height),
                    }
                else:
                    monitor = sct.monitors[1]  # 主显示器

                img = sct.grab(monitor)
                if not path:
                    from app.utils.utils import get_app_data_dir
                    out_dir = get_app_data_dir() / "screenshots"
                    out_dir.mkdir(parents=True, exist_ok=True)
                    path = str(
                        out_dir / f"desktop_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
                    )
                else:
                    Path(path).parent.mkdir(parents=True, exist_ok=True)

                mss.tools.to_png(img.rgb, img.size, output=path)
                size_bytes = Path(path).stat().st_size
                abs_path = str(Path(path).resolve())

            logger.info(f"[Automation] screenshot saved: {abs_path} ({size_bytes} bytes)")
            return ToolResult(
                True,
                content={
                    "path": path,
                    "absolute_path": abs_path,
                    "width": img.size[0],
                    "height": img.size[1],
                    "size_bytes": size_bytes,
                    "markdown": f"![screenshot]({abs_path})",
                },
            )
        except Exception as e:
            logger.exception(f"[Automation] screenshot failed: {e}")
            return ToolResult(False, error=f"screenshot failed: {e}")

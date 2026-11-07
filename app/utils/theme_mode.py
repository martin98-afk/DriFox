# -*- coding: utf-8 -*-
"""外观模式：自动 / 深色 / 浅色

配置分两层，避免改动扩散：
- ``ui_theme_mode``（新增）：用户选择，auto / light / dark
- ``ui_light_mode``（旧）  ：实际生效深浅，沿用既有语义 —— 主题过滤
  （collect_theme_entries）、主题卡、快捷菜单等读取点一律不动

auto 模式由 QTimer 轮询系统偏好（app/utils/system_theme.py），变化后写生效
值并切换默认深浅主题，再广播一次主题刷新（与设置卡 configChanged 同一条
窗口刷新链路）。

换主题规则：只在「当前主题的深浅与目标不一致」时才切默认主题
（lumia / true-black）。旧开关是无条件切默认主题，会把用户在手选主题
（例如浅色下选 crema）打回默认，这里收窄为按需切换。
"""

from __future__ import annotations

from typing import Optional

from loguru import logger
from PyQt5.QtCore import QObject, QTimer

from app.utils import system_theme

MODE_AUTO = "auto"
MODE_LIGHT = "light"
MODE_DARK = "dark"
MODES = (MODE_AUTO, MODE_LIGHT, MODE_DARK)

DEFAULT_THEME_LIGHT = "lumia"
DEFAULT_THEME_DARK = "true-black"

_watcher: Optional["_SystemThemeWatcher"] = None


def _settings():
    from app.utils.config import Settings

    return Settings.get_instance()


def default_theme_for(light: bool) -> str:
    return DEFAULT_THEME_LIGHT if light else DEFAULT_THEME_DARK


def current_mode() -> str:
    """当前外观模式（非法存量值按 auto 处理）"""
    mode = _settings().ui_theme_mode.value
    return mode if mode in MODES else MODE_AUTO


# 主题记忆存在 app_state 缓存（cache/app_state.json），不进 app.config：
# 它只描述「这台机器上次选了什么」，不是用户偏好项，也就不该进设置页、
# 不该参与云端配置同步。
_STATE_KEY = "theme_style_by_mode"


def _memory_key(light: bool) -> str:
    return "light" if light else "dark"


def _load_theme_memory() -> dict:
    from app.utils import app_state

    value = app_state.get(_STATE_KEY, {})
    return dict(value) if isinstance(value, dict) else {}


def remembered_theme(light: bool) -> Optional[str]:
    """取某深浅模式下用户上次选的主题；没记录 / 主题已消失 / 深浅不符都返回 None"""
    theme_id = _load_theme_memory().get(_memory_key(light))
    if not theme_id:
        return None
    try:
        from app.utils.theme_manager import theme_manager

        if theme_id not in theme_manager.list_themes():
            return None
        if bool(theme_manager.is_light_theme(theme_id)) != bool(light):
            return None
    except Exception:
        return None
    return str(theme_id)


def remember_theme(theme_id: str) -> None:
    """记住用户在某深浅模式下选的主题（切走再切回时恢复，而不是落回默认）"""
    if not theme_id:
        return
    try:
        from app.utils.theme_manager import theme_manager

        light = bool(theme_manager.is_light_theme(theme_id))
    except Exception:
        return
    from app.utils import app_state

    memory = _load_theme_memory()
    key = _memory_key(light)
    if memory.get(key) == theme_id:
        return
    memory[key] = str(theme_id)
    app_state.set(_STATE_KEY, memory)


def resolve_light(mode: str, probe: Optional[bool], current: bool) -> bool:
    """纯函数：模式 + 系统探测结果 + 当前生效值 → 应生效的深浅

    探测失败（probe is None）回退 current，不折腾用户；auto 之外的模式
    忽略探测结果。
    """
    if mode == MODE_LIGHT:
        return True
    if mode == MODE_DARK:
        return False
    return current if probe is None else bool(probe)


def effective_light(mode: Optional[str] = None) -> bool:
    """按当前配置解析生效深浅（auto 会现场探测一次系统）"""
    cfg = _settings()
    if mode is None:
        mode = cfg.ui_theme_mode.value
    return resolve_light(mode, system_theme.system_prefers_light(), bool(cfg.ui_light_mode.value))


def apply_light(light: bool, *, save: bool = True) -> bool:
    """把生效深浅落到配置：写 ui_light_mode，必要时切默认主题。

    返回是否改动了配置。不广播 UI 刷新（启动期还没有窗口，由调用方决定）。
    """
    cfg = _settings()
    changed = False
    if bool(cfg.ui_light_mode.value) != bool(light):
        cfg.set(cfg.ui_light_mode, bool(light), save=save)
        changed = True
    # 主题深浅必须与生效值一致：主题卡按深浅过滤，不一致时用户会看到一个
    # 不在当前候选集里的主题（表现为切换后主题卡空白或闪回）
    try:
        from app.utils.config import update_theme_options
        from app.utils.theme_manager import theme_manager

        current = cfg.ui_theme_style.value
        if bool(theme_manager.is_light_theme(current)) != bool(light):
            target = remembered_theme(light) or default_theme_for(light)
            if current != target:
                # 写之前刷新 validator 选项集：目标主题尚未注册时会被
                # OptionsValidator.correct 静默回退到 options[0]
                update_theme_options()
                cfg.set(cfg.ui_theme_style, target, save=save)
                changed = True
    except Exception:
        logger.debug("外观模式：主题深浅对齐失败", exc_info=True)
    return changed


def set_mode(mode: str, *, save: bool = True, broadcast: bool = True) -> None:
    """切换外观模式（分段控件 / 快捷菜单 / 外部同步共用入口）"""
    if mode not in MODES:
        return
    cfg = _settings()
    # 先把当前主题记到它所属的深浅模式下，再切模式，否则切回来只能落回默认
    remember_theme(cfg.ui_theme_style.value)
    if cfg.ui_theme_mode.value != mode:
        cfg.set(cfg.ui_theme_mode, mode, save=save)
    apply_light(effective_light(mode), save=save)
    sync_watcher()
    if broadcast:
        broadcast_theme_refresh()


def broadcast_theme_refresh() -> None:
    """通知所有窗口按「主题变更」刷新（复用设置卡的刷新链路）"""
    try:
        from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

        LLMSettingsCard._last_change_type = "theme"
    except Exception:
        pass
    try:
        from app.core.infra import window_registry

        windows = list(window_registry.alive_window_instances())
    except Exception:
        return
    for win in windows:
        if getattr(win, "_is_destroyed", False):
            continue
        try:
            win._on_settings_config_changed()
        except Exception:
            logger.debug("外观模式：窗口刷新失败", exc_info=True)


class _SystemThemeWatcher(QObject):
    """系统深浅轮询：只在 auto 模式转表"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(system_theme.poll_interval_ms())
        self._timer.timeout.connect(self._poll)
        self._last = system_theme.system_prefers_light()

    def set_active(self, active: bool) -> None:
        if active:
            # 转表瞬间重置基线，避免停机期间的系统切换在恢复时被当成「无变化」
            self._last = system_theme.system_prefers_light()
            if not self._timer.isActive():
                self._timer.start()
        elif self._timer.isActive():
            self._timer.stop()

    def _poll(self) -> None:
        value = system_theme.system_prefers_light()
        if value is None or value == self._last:
            return
        self._last = value
        # 探测与生效之间用户可能已切到手动模式
        if _settings().ui_theme_mode.value != MODE_AUTO:
            return
        logger.info(f"[外观模式] 系统深浅切换 → {'浅色' if value else '深色'}")
        apply_light(value)
        broadcast_theme_refresh()


def _ensure_watcher() -> Optional[_SystemThemeWatcher]:
    global _watcher
    if _watcher is not None:
        return _watcher
    try:
        from PyQt5.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return None
        _watcher = _SystemThemeWatcher(app)
    except Exception:
        logger.debug("外观模式：系统深浅监听初始化失败", exc_info=True)
        return None
    return _watcher


def sync_watcher() -> None:
    """auto 才轮询；手动模式停表（非 Windows 每次探测都要起子进程）"""
    watcher = _ensure_watcher()
    if watcher is None:
        return
    watcher.set_active(_settings().ui_theme_mode.value == MODE_AUTO)


def reconcile_on_startup() -> None:
    """启动期对齐：auto 解析系统深浅并让主题归位。

    必须在首次 ``setTheme`` 之前调用 —— qfluentwidgets 首帧深浅由当前主题
    决定，晚了会先按旧主题渲染一帧再翻色。
    """
    cfg = _settings()
    remember_theme(cfg.ui_theme_style.value)
    apply_light(effective_light(cfg.ui_theme_mode.value), save=True)
    sync_watcher()
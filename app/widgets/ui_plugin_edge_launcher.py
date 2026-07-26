# -*- coding: utf-8 -*-
"""UI 插件左侧边缘入口（Launcher）

设计目标：在主窗口左侧提供「细线 → 胶囊 → 菜单」三态切换，
让用户在不记忆斜杠命令的情况下发现并打开 UI 插件浮动卡片。

组件职责（严格限制）：
  1. 绘制细线 / 胶囊两态；
  2. 管理鼠标进入 / 离开 / 点击 / 收起等交互状态；
  3. 创建和显示插件菜单；
  4. 把菜单点击转发给 UIPluginRegistry（不直接创建卡片）。

不负责：
  - 插件卡片实例化（交给 UIPluginRegistry.toggle_floating_card）；
  - 插件启用 / 禁用 / 安装 / 卸载；
  - 替换斜杠命令系统；
  - 修改主布局（浮层不参与 sizeHint 计算）。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from loguru import logger
from PyQt5.QtCore import QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QPainter, QPainterPath
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QMenu,
    QWidget,
)
from qfluentwidgets import FluentIcon, isDarkTheme

from app.utils.design_tokens import Colors, font_size_css, scale_font_size
from app.utils.utils import _is_current_theme_light, get_font_family_css

# ── 尺寸常量（来自设计文档 §2）───────────────────────────────
LINE_WIDTH = 4  # 默认细线宽（px）
LINE_HEIGHT = 80  # 默认细线高（px）
CAPSULE_WIDTH = 22  # 胶囊展开后宽（比设计文档的 18 略宽，便于图标识别）
CAPSULE_HEIGHT = 64  # 胶囊高（设计文档 60，留 4px 给阴影/边距）
# 触发区与胶囊等宽（紧贴左边缘时，胶囊整体都应可点击命中）
TRIGGER_ZONE_WIDTH = CAPSULE_WIDTH
COLLAPSE_DELAY_MS = 220  # 鼠标离开后延迟收起（避免路过边缘闪烁）
MENU_MIN_WIDTH = 220  # 弹出菜单最小宽度
MENU_MAX_HEIGHT = 360  # 弹出菜单最大高度（超过则上下调整）
ANIM_DURATION_MS = 160  # 展开/收起动画时长
ANIM_TICK_MS = 16  # 动画帧间隔（~60fps）


class _LauncherVisual(QWidget):
    """Launcher 内部可见的"细线 / 胶囊"渲染层

    作为 :class:`UIPluginEdgeLauncher` 的子控件，承载动画和绘制。
    外层（Launcher）负责触发区命中检测与状态机，本控件仅负责视觉。
    鼠标事件透传给父控件（WA_TransparentForMouseEvents）。
    """

    def __init__(self, parent: "UIPluginEdgeLauncher"):
        super().__init__(parent)
        self._expansion: float = 0.0
        self._anim_timer: Optional[QTimer] = None
        self._anim_start: float = 0.0
        self._anim_target: float = 0.0
        self._anim_elapsed: int = 0
        self._anim_duration: int = ANIM_DURATION_MS

        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)

    def set_expansion(self, value: float) -> None:
        """设置展开量（0.0 ~ 1.0），会同步重绘。"""
        self._expansion = max(0.0, min(1.0, value))
        self.update()

    def animate_to(self, target: float, duration_ms: int = ANIM_DURATION_MS) -> None:
        """动画到目标展开量。"""
        target = max(0.0, min(1.0, target))
        if self._anim_timer is None:
            self._anim_timer = QTimer(self)
            self._anim_timer.setInterval(ANIM_TICK_MS)
            self._anim_timer.timeout.connect(self._on_anim_tick)
        self._anim_timer.stop()
        self._anim_start = self._expansion
        self._anim_target = target
        self._anim_elapsed = 0
        self._anim_duration = max(1, duration_ms)
        self._anim_timer.start()

    def _on_anim_tick(self) -> None:
        self._anim_elapsed += ANIM_TICK_MS
        t = min(1.0, self._anim_elapsed / self._anim_duration)
        # smoothstep (ease-in-out)
        eased = t * t * (3 - 2 * t)
        value = self._anim_start + (self._anim_target - self._anim_start) * eased
        self.set_expansion(value)
        if t >= 1.0:
            self._anim_timer.stop()

    def paintEvent(self, event) -> None:  # noqa: D401
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        rect = QRect(0, 0, self.width(), self.height())
        if self._expansion <= 0.001:
            # 纯细线紧贴左边缘
            line_rect = QRect(0, 0, LINE_WIDTH, rect.height())
            self._paint_line(painter, line_rect)
        else:
            # 从左边缘向右展开的半胶囊
            current_w = LINE_WIDTH + (CAPSULE_WIDTH - LINE_WIDTH) * self._expansion
            cap_rect = QRect(0, 0, int(current_w), rect.height())
            self._paint_half_capsule(painter, cap_rect, self._expansion)

        painter.end()

    def _paint_line(self, painter: QPainter, rect: QRect) -> None:
        """绘制默认细线（紧贴左边缘，发光向右）"""
        accent = self._accent_color()
        # 发光底层（向右扩散，alpha 较低）
        glow_color = QColor(accent)
        glow_color.setAlpha(60)
        painter.setPen(Qt.NoPen)
        painter.setBrush(glow_color)
        painter.drawRoundedRect(rect.adjusted(0, 6, 2, -6), 2, 2)
        # 主线条
        solid = QColor(accent)
        solid.setAlpha(220)
        painter.setBrush(solid)
        painter.drawRoundedRect(rect, 2, 2)

    def _paint_half_capsule(self, painter: QPainter, rect: QRect, expansion: float) -> None:
        """绘制半胶囊：左边缘平直 + 右上右下圆角（向右展开）"""
        accent = self._accent_color()
        x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        r = w / 2.0  # 圆角半径 = 宽度的一半（胶囊感）

        # ── 半胶囊路径：左平右圆 ──
        path = QPainterPath()
        path.moveTo(x, y)
        path.lineTo(x + w - r, y)
        path.quadTo(x + w, y, x + w, y + r)
        path.lineTo(x + w, y + h - r)
        path.quadTo(x + w, y + h, x + w - r, y + h)
        path.lineTo(x, y + h)
        path.closeSubpath()

        # 背景填充
        bg = QColor(accent)
        bg.setAlpha(int(50 + 150 * expansion))
        painter.setPen(Qt.NoPen)
        painter.setBrush(bg)
        painter.drawPath(path)

        # 边框
        border = QColor(accent)
        border.setAlpha(int(160 + 95 * expansion))
        from PyQt5.QtGui import QPen

        pen = QPen(border)
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)

        # 中心 FluentIcon.MENU 图标
        icon_size = max(10, int(w * 0.6))
        menu_icon = FluentIcon.MENU.icon()
        icon_rect = QRect(
            int(x + w / 2 - icon_size / 2),
            int(y + h / 2 - icon_size / 2),
            icon_size,
            icon_size,
        )
        menu_icon.paint(painter, icon_rect, Qt.AlignCenter)

    def _accent_color(self) -> str:
        """从当前主题读取强调色，失败时回退到设计文档约定的暖色。"""
        try:
            accent = getattr(Colors, "TEXT_ACCENT", "") or getattr(Colors, "INPUT_FOCUS_BORDER", "")
            if accent:
                return accent
        except Exception:
            pass
        # 安全回退：暖色调（与设计文档描述一致，但不硬编码具体主题）
        return "#f59e9b"


class UIPluginEdgeLauncher(QWidget):
    """UI 插件左侧边缘入口 — 主窗口内部浮层（overlay）模式

    作为 MainWidget 的内部子控件（绝对定位，不参与主布局）浮在左边缘中部。
    共享主窗口的 z-order，随主窗口自动移动 / 裁剪 / 销毁，因此**不存在与
    "强行置顶主窗口"争抢层级的问题**——这正是之前独立置顶窗口闪烁/消失的根因。

    生命周期：
      1. MainWidget 中创建（以 self 为 Qt 父对象）并 hide()；
      2. 插件热重载或首次加载完成时 ``refresh_plugins()`` 刷新列表并 show()；
      3. 首次 ``_sync_position()`` 时若顶层窗口已就绪，则重定父到顶层窗口，
         之后随窗口一起移动 / 缩放，无需事件过滤器；
      4. 窗口销毁时作为子控件随父对象一起释放。

    状态机：
      COLLAPSED → 鼠标进入触发区 → EXPANDED
      EXPANDED → 鼠标离开 + 延迟 → COLLAPSED
      EXPANDED → 点击胶囊 → MENU_OPEN
      MENU_OPEN → 点击菜单项 / Esc / 空白 → EXPANDED（保持展开直到收起计时）

    信号：
      menu_visibility_changed(bool) — 菜单打开 / 关闭，供父窗口观察
    """

    menu_visibility_changed = pyqtSignal(bool)

    def __init__(self, parent: QWidget = None, *, main_widget=None):
        # 注入为主窗口内部子控件（overlay）：随主窗口一起移动/裁剪/销毁，
        # 共享主窗口 z-order → 永不与"置顶主窗口"争抢层级，从根本上消除闪烁。
        # 不参与主布局（绝对定位），仅浮在左边缘中部。
        super().__init__(main_widget)
        # 保留 main_widget 强引用（用于取窗口高度做垂直居中）
        self._main_widget = main_widget
        # 共享 Launcher 模式下，卡片操作的目标窗口（与 _main_widget 分离）
        # 独立模式下 = _main_widget；Tab 模式下 = 当前活跃标签页窗口
        self._card_target_widget: Optional[QWidget] = None
        # 是否已重定父到顶层窗口（保证"窗口左边缘"而非"内容区左边缘"）
        self._reparented_to_top: bool = False
        # 缓存的插件列表 [(card_id, title, plugin_name), ...]
        self._card_infos: List[Tuple[str, str, str]] = []
        # 性能优化缓存
        self._cached_stylesheet: Optional[str] = None
        self._cached_stylesheet_is_light: Optional[bool] = None
        self._icon_cache: Dict[str, QIcon] = {}
        # 状态机
        self._state: str = "COLLAPSED"  # COLLAPSED / EXPANDED / MENU_OPEN
        self._menu: Optional[QMenu] = None
        # 收起延迟定时器
        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.setInterval(COLLAPSE_DELAY_MS)
        self._collapse_timer.timeout.connect(self._on_collapse_timeout)

        # 点击防重开：菜单刚因点击胶囊而关闭时置 True，阻止 mousePressEvent 重开
        self._click_just_closed_menu: bool = False
        self._clear_flag_timer = QTimer(self)
        self._clear_flag_timer.setSingleShot(True)
        self._clear_flag_timer.setInterval(0)
        self._clear_flag_timer.timeout.connect(self._clear_click_just_closed)

        # 触发区自身就是 Launcher（this），负责命中检测
        self.setFixedWidth(TRIGGER_ZONE_WIDTH)
        # 视觉层紧贴触发区
        self._visual = _LauncherVisual(self)
        self._visual.show()

        # Qt 自身属性
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # 不抢焦点（避免输入框失焦）
        self.setFocusPolicy(Qt.NoFocus)
        # 鼠标跟踪：enterEvent / leaveEvent
        self.setMouseTracking(True)

    # ── 公开 API ────────────────────────────────────────────
    def set_card_target(self, widget: QWidget) -> None:
        """设置卡片操作的目标窗口（共享 Launcher 使用）

        Args:
            widget: 目标 OpenAIChatToolWindow 实例，菜单点击时传递给
                    UIPluginRegistry.toggle_floating_card 以定位到正确窗口。
        """
        self._card_target_widget = widget

    def update_geometry(self, chat_rect: QRect = QRect()) -> None:
        """兼容接口 —— 被 MainWidget.resizeEvent 调用，重定向到位置同步。

        Args:
            chat_rect: 不再使用（作为主窗口内部子控件，直接从父窗口读取高度），
                       保留参数签名避免破坏调用方。
        """
        self._sync_position()

    # ── 显示 / 位置同步（作为主窗口内部子控件，父坐标定位）────────
    def showEvent(self, event) -> None:  # noqa: N802
        """显示时同步一次位置（首次显示或窗口恢复时触发）"""
        super().showEvent(event)
        self._sync_position()
        self.raise_()

    def _ensure_parent(self) -> QWidget:
        """确保本浮层挂在真正的顶层窗口上，从而定位到「窗口左边缘」。

        仅在尚未挂接时执行一次：main_widget 注入时可能还未加入顶层窗口，
        window() 会返回自身；待窗口就绪后重定父到顶层窗口，之后随窗口一起
        移动/缩放/销毁，无需再处理事件过滤器与置顶竞争。
        """
        if self._reparented_to_top:
            return self.parent() or self._main_widget
        mw = self._main_widget
        if mw is None:
            return self.parent() or mw
        top = mw.window()
        # 尚未加入顶层窗口 → window() 返回自身，暂时挂 mw 上，下次再试
        if top is None or top is mw:
            return mw
        if self.parent() is not top:
            was_visible = self.isVisible()
            self.setParent(top)
            if was_visible:
                self.show()
        self._reparented_to_top = True
        return top

    def _sync_position(self) -> None:
        """同步自身到主窗口左边缘中部（父控件坐标，绝对定位）

        作为主窗口内部子控件，本浮层共享主窗口 z-order，随主窗口自动移动/
        裁剪/销毁——从根本上消除独立置顶窗口与"置顶主窗口"争抢层级导致的闪烁。
        仅需在父窗口尺寸变化时重新垂直居中。
        """
        mw = self._main_widget
        if mw is None or not mw.isVisible():
            return

        parent = self._ensure_parent()
        if parent is None or not parent.isVisible():
            return

        h = max(LINE_HEIGHT, CAPSULE_HEIGHT) + 12
        # 紧贴父窗口（顶层窗口）左边缘
        x = 0
        # 垂直居中于父窗口
        y = parent.height() // 2 - h // 2

        new_geo = QRect(int(x), int(y), TRIGGER_ZONE_WIDTH, int(h))
        if self.geometry() != new_geo:
            self.setGeometry(new_geo)
            self._visual.setGeometry(0, 0, CAPSULE_WIDTH, int(h))
        # 置于同层兄弟控件之上（避免被内容区遮挡）
        self.raise_()

    # ── 位置同步（独立窗口全局坐标定位）──────────────────
    def refresh_plugins(self) -> None:
        """从 UIPluginRegistry 重新读取插件列表，决定是否显示入口

        异常：单个 FloatingCardInfo 数据损坏时跳过该项，其余继续显示。
        """
        try:
            from app.core.ui_plugin_registry import UIPluginRegistry

            cards = UIPluginRegistry.get_instance().get_floating_cards()
        except Exception as e:
            logger.warning(f"[EdgeLauncher] 读取 UIPluginRegistry 失败：{e}")
            self._card_infos = []
            self.hide()
            return

        infos: list = []
        for card_id, info in cards.items():
            try:
                title = (info.title or "").strip() or card_id
                infos.append((card_id, title, info.plugin_name))
            except Exception as e:
                logger.warning(f"[EdgeLauncher] 跳过异常插件项 card_id={card_id!r}: {e}")
        # 排序：按 title 字母序，保持稳定可预期
        infos.sort(key=lambda x: x[1].lower())
        self._card_infos = infos

        logger.info(f"[EdgeLauncher] refresh_plugins loaded {len(infos)} cards: {[cid for cid, _, _ in infos]}")

        # 预加载插件图标缓存（避免菜单首次打开时同步文件 I/O 阻塞 UI 线程）
        self._preload_icons()

        if self._card_infos:
            self._sync_position()
            self.show()
            self.raise_()
        else:
            self.hide()
            # 没有插件时也收起菜单
            self._close_menu()

    def _preload_icons(self) -> None:
        """预加载所有插件的图标到缓存，避免菜单打开时同步文件 I/O 阻塞 UI 线程。"""
        from app.core.plugin_manager import PluginManager

        pm = PluginManager.get_instance()
        theme = "dark" if isDarkTheme() else "light"
        cache: Dict[str, QIcon] = {}
        for _, _, plugin_name in self._card_infos:
            try:
                pi = pm.get_plugin(plugin_name)
                if pi and pi.icon_config:
                    icon_p = pi.icon_config.get(theme)
                    if icon_p:
                        cache[plugin_name] = QIcon(str(icon_p))
            except Exception:
                pass  # non-critical, skip icon
        self._icon_cache = cache

    def apply_theme(self) -> None:
        """主题切换后调用：刷新视觉颜色与菜单样式。"""
        # 使样式表缓存和图标缓存失效（主题色/深色/浅色图标路径可能不同）
        self._cached_stylesheet = None
        self._cached_stylesheet_is_light = None
        # 重新加载图标缓存：深浅主题切换后图标路径可能不同（如 icon_dark.svg / icon_light.svg）
        self._preload_icons()
        self._visual.update()

    # ── 鼠标事件（命中检测）────────────────────────────────
    def enterEvent(self, event) -> None:  # noqa: N802
        if not self._card_infos:
            return
        self.raise_()  # 确保浮在内容区之上
        self._collapse_timer.stop()
        if self._state == "COLLAPSED":
            self._set_state("EXPANDED")
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._state == "MENU_OPEN":
            # 菜单打开期间，鼠标离开不应立即收起
            super().leaveEvent(event)
            return
        if self._state == "EXPANDED":
            self._collapse_timer.start()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self._card_infos:
            super().mousePressEvent(event)
            return
        if event.button() == Qt.LeftButton:
            # ── 防重开：若菜单刚因点击胶囊而关闭，此点击即为「关」动作 ──
            if self._click_just_closed_menu:
                self._click_just_closed_menu = False
                self._clear_flag_timer.stop()
                event.accept()
                return
            if self._state in ("EXPANDED", "COLLAPSED"):
                self._open_menu()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape and self._state == "MENU_OPEN":
            self._close_menu()
        super().keyPressEvent(event)

    # ── 状态机 ──────────────────────────────────────────────
    def _set_state(self, new_state: str) -> None:
        if new_state == self._state:
            return
        self._state = new_state
        if new_state == "COLLAPSED":
            self._visual.animate_to(0.0, duration_ms=ANIM_DURATION_MS)
        elif new_state == "EXPANDED":
            self._visual.animate_to(1.0, duration_ms=ANIM_DURATION_MS - 20)
        # MENU_OPEN 不改变视觉展开量（保持胶囊显示）

    def _on_collapse_timeout(self) -> None:
        # 鼠标仍未回到本控件才真正收起
        if self.underMouse():
            return
        self._set_state("COLLAPSED")

    # ── 菜单 ────────────────────────────────────────────────
    def _open_menu(self) -> None:
        # 插件列表在初始化/热重载时已通过 refresh_plugins() 刷新，此处不再重复加载。
        # 注意：若将来需支持"运行中动态增减插件且无热重载"，请在此处加版本号检查。
        if not self._card_infos:
            return
        self._collapse_timer.stop()
        self._set_state("MENU_OPEN")

        # 关闭旧菜单
        self._close_menu()
        menu = QMenu(self)
        menu.setAttribute(Qt.WA_TranslucentBackground, True)
        menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        menu.setStyleSheet(self._menu_stylesheet())
        menu.setMinimumWidth(MENU_MIN_WIDTH)

        for card_id, title, plugin_name in self._card_infos:
            label = title
            if plugin_name and plugin_name != "system" and not title.startswith(plugin_name):
                # 用户插件且 title 未含前缀时，附加 plugin_name 提示
                label = f"{title}  ·  {plugin_name}"
            action = QAction(label, menu)
            action.setData(card_id)

            # 使用预加载的图标缓存（refresh_plugins 时已加载），避免同步文件 I/O
            icon = self._icon_cache.get(plugin_name)
            if icon is not None:
                action.setIcon(icon)

            action.triggered.connect(lambda checked=False, cid=card_id: self._on_menu_action(cid))
            menu.addAction(action)

        # 定位：菜单在胶囊右侧，垂直居中于 Launcher
        # 计算菜单理想高度（无硬上限，让屏幕修正来决定最终高度）
        item_count = len(self._card_infos)
        est_menu_h = max(80, item_count * 32 + 24)
        # 水平：胶囊右边缘 + 4px 间距
        x = self.mapToGlobal(QPoint(CAPSULE_WIDTH + 4, 0)).x()
        # 垂直：Launcher 中心对齐（向上下均等展开）
        launcher_center_y = self.mapToGlobal(QPoint(0, self.height() // 2)).y()
        y = launcher_center_y - est_menu_h // 2

        # 屏幕边界修正 + 动态最大高度
        screen = QApplication.screenAt(QPoint(x, y)) or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            # 水平：不超出右边缘
            if x + MENU_MIN_WIDTH > avail.right():
                x = avail.right() - MENU_MIN_WIDTH - 8
            # 垂直：先确保不超出顶部
            if y < avail.top() + 4:
                y = avail.top() + 4
            # 再确保不超出底部（若空间不够，回缩 y 让菜单底部对齐）
            if y + est_menu_h > avail.bottom() - 4:
                y = avail.bottom() - est_menu_h - 4
            # 若顶部又超出，说明屏幕实在不够高，限制最大高度 + 滚轮
            if y < avail.top() + 4:
                y = avail.top() + 4
                max_h = avail.height() - 80
            else:
                max_h = max(MENU_MAX_HEIGHT, est_menu_h)
        else:
            max_h = MENU_MAX_HEIGHT

        # 设置最大高度（超出时 QMenu 自动出现滚轮）
        menu.setMaximumHeight(max_h)

        global_pos = QPoint(int(x), int(y))

        # 关闭联动：菜单关闭（点击外部 / Esc / 选中项）后回到 EXPANDED/COLLAPSED
        menu.aboutToHide.connect(self._on_menu_about_to_hide)

        self._menu = menu
        self.menu_visibility_changed.emit(True)
        menu.popup(global_pos)
        menu.raise_()

    def _close_menu(self) -> None:
        if self._menu is not None:
            try:
                self._menu.close()
            except Exception:
                pass
            self._menu = None

    def _on_menu_about_to_hide(self) -> None:
        if self._menu is not None:
            try:
                self._menu.aboutToHide.disconnect(self._on_menu_about_to_hide)
            except Exception:
                pass
        self._menu = None
        self.menu_visibility_changed.emit(False)

        # 标记「菜单刚被关闭」，用于 mousePressEvent 防重开。
        # zero-timer 延迟清除：菜单关闭 → mousePressEvent（同一事件周期内）
        # → 清除标记 → 下次点击能正常打开。
        # 用 self._clear_flag_timer（持引用，防 GC）而非 QTimer.singleShot。
        self._click_just_closed_menu = True
        self._clear_flag_timer.start()

        if self.underMouse():
            self._set_state("EXPANDED")
        else:
            self._set_state("COLLAPSED")
            self._collapse_timer.start()

    def _clear_click_just_closed(self) -> None:
        """清除防重开标记（zero-timer 回调，在 mousePressEvent 之后执行）。"""
        if self._click_just_closed_menu:
            self._click_just_closed_menu = False

    def _on_menu_action(self, card_id: str) -> None:
        """菜单项点击：调用当前窗口的 UIPluginRegistry.toggle_floating_card

        使用 _card_target_widget（共享 Launcher 可动态设置）作为目标窗口，
        回退到 _main_widget。使用 getattr 安全访问（测试中可能绕过 __init__）。
        """
        try:
            from app.core.ui_plugin_registry import UIPluginRegistry

            registry = UIPluginRegistry.get_instance()
            target = getattr(self, '_card_target_widget', None) or self._main_widget
            registry.toggle_floating_card(card_id, main_widget=target)
        except Exception as e:
            logger.warning(f"[EdgeLauncher] 打开卡片 {card_id!r} 失败：{e}")

    # ── 样式 ────────────────────────────────────────────────
    def _menu_stylesheet(self) -> str:
        is_light = _is_current_theme_light()
        # 主题未变 → 返回缓存（避免重复计算字体/颜色 CSS 字符串）
        if self._cached_stylesheet is not None and self._cached_stylesheet_is_light == is_light:
            return self._cached_stylesheet

        try:
            text_color = Colors.TEXT_PRIMARY
            hover_bg = Colors.HOVER_BG_STRONG
            border = Colors.BORDER
            bg = Colors.CARD_BG_SOLID
        except Exception:
            text_color = "#ffffff"
            hover_bg = "rgba(255,255,255,0.08)"
            border = "#3d3d3d"
            bg = "rgba(33,33,38,250)"
        if is_light:
            # 浅色主题下做轻量反转
            text_color = "#1f1f1f"
            hover_bg = "rgba(0,0,0,0.06)"
            border = "rgba(0,0,0,0.12)"
            bg = "rgba(255,255,255,245)"
        # 应用系统字体 + 缩放字号（与项目其它 UI 保持一致）
        font_family_css = get_font_family_css()
        # 菜单项：13px（中等强度可读性）
        item_font_size = scale_font_size(13)
        # 菜单背景容器：稍小的字号
        container_font_size = scale_font_size(12)
        stylesheet = f"""
            QMenu {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 8px;
                padding: 4px;
                {font_family_css}
                {font_size_css(12)}
                color: {text_color};
            }}
            QMenu::item {{
                padding: 6px 20px 6px 20px;
                margin: 2px 2px;
                color: {text_color};
                border-radius: 5px;
                {font_family_css}
                {font_size_css(13)}
            }}
            QMenu::item:selected {{
                background-color: {hover_bg};
            }}
            QMenu::item:disabled {{
                color: rgba(128, 128, 128, 0.6);
            }}
            QMenu::separator {{
                height: 1px;
                background-color: {border};
                margin: 3px 8px;
            }}
            QMenu::icon {{
                margin-right: 6px;
            }}
        """
        self._cached_stylesheet = stylesheet
        self._cached_stylesheet_is_light = is_light
        return stylesheet

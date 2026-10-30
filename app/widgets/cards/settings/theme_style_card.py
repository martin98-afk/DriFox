# -*- coding: utf-8 -*-
"""主题风格卡：网格预览式主题选择器。

qfluentwidgets 的 OptionsSettingCard 把选项铺成一列单选框，主题之间除了名字
没有任何视觉差异，等同于盲选。这里换成网格预览：每格画出该主题的迷你外观
—— window 渐变作底，主题背景图按放大后的不透明度叠上去，底部压一条 accent
主色条，格子里写主题名。点格子即切换，选中格描 accent 边并打角标勾。

主题数据由 collect_theme_entries() 现场从 ThemeManager 采集；卡片本身不扫描
主题，也不持有跨主题状态。
"""

from __future__ import annotations

import os
import weakref
from typing import Optional

from PyQt5 import sip
from PyQt5.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt5.QtWidgets import QGridLayout, QLabel, QSizePolicy, QWidget
from qfluentwidgets import ExpandSettingCard, FluentIcon

from app.utils.design_tokens import Colors, _get_global_font, scale_font_size
from app.utils.theme_manager import theme_manager

# ── 网格几何 ──────────────────────────────────────────
_TILE_MIN_W = 156  # 单格最小宽度（决定一行放几格）
_PREVIEW_MIN_H = 72  # 预览区最小高（不含底部名称条）
_GRID_SPACING = 10
_ACCENT_BAR_H = 3  # 主色条高度
_NAME_FONT_SIZE = 14  # 名称条字号基准（最终像素 = scale_font_size 缩放后）
_NAME_FONT_MIN = 11  # 最小档位下也保底可读
_NAME_PAD_V = 5  # 名称条上下留白
_RADIUS = 8
_MARGIN_L = 48  # 与 ExpandSettingCard 的展开区左缩进（对齐单选项历史缩进）
_MARGIN_R = 24

# 名称字体与其撑出的条高按 (字族, 像素) 缓存：paint 每次都要用，
# 字族/字号变化时 key 自然失效重建（design_tokens 侧缓存同步失效）
_name_font_key: tuple = ()
_name_font_pack: Optional[tuple] = None


def _name_pack() -> tuple:
    """返回名称条绘制用的 (font, bar_height)。

    原先字体从 widget font 派生（-2px）：widget font 是 apply_font_size_to_widget
    统一铺的 14px 底噪，既不吃用户字族，又被压到 12px 以下，配上 22px 的名称条
    后主题名又小又糊。名称是格子里唯一的语义信息，这里改按 UI 字号档位独立
    定尺寸并加粗，字族跟随全局配置；条高按实际字高算，字号变大不会被裁。
    """
    global _name_font_key, _name_font_pack
    key = (_get_global_font(), max(_NAME_FONT_MIN, scale_font_size(_NAME_FONT_SIZE)))
    if _name_font_pack is not None and _name_font_key == key:
        return _name_font_pack
    font = QFont(key[0])
    font.setPixelSize(key[1])
    font.setBold(True)
    bar_height = _ACCENT_BAR_H + max(24, QFontMetrics(font).height() + 2 * _NAME_PAD_V)
    _name_font_pack = (font, bar_height)
    _name_font_key = key
    return _name_font_pack


def _tile_height() -> int:
    """单格总高：预览区 + 名称条（随字号变化）"""
    return _PREVIEW_MIN_H + _name_pack()[1]


def _parse_color(value, fallback: str = "#2a2a2e") -> QColor:
    """解析主题色串为 QColor。

    QColor 只认 #rrggbb / #aarrggbb，主题 yaml 里大量是 CSS 的 rgba(r,g,b,a)
    （有时还带引号），直接丢给 QColor 会退化成黑色。
    """
    if isinstance(value, QColor):
        return value
    s = str(value or "").strip().strip("'\"")
    try:
        if s.startswith("#"):
            color = QColor(s)
            if color.isValid():
                return color
        elif s.lower().startswith(("rgba(", "rgb(")):
            inner = s[s.index("(") + 1 : s.rindex(")")]
            parts = [p.strip() for p in inner.split(",")]
            if len(parts) >= 3:
                r, g, b = (int(round(float(parts[i]))) for i in range(3))
                a = 255
                if len(parts) >= 4:
                    av = float(parts[3])
                    a = int(round(av * 255)) if av <= 1 else int(round(av))
                return QColor(
                    max(0, min(255, r)),
                    max(0, min(255, g)),
                    max(0, min(255, b)),
                    max(0, min(255, a)),
                )
        else:
            color = QColor(s)
            if color.isValid():
                return color
    except Exception:
        pass
    fallback_color = QColor(fallback)
    return fallback_color if fallback_color.isValid() else QColor(42, 42, 46)


def _luminance(color: QColor) -> float:
    """感知亮度，用来决定名称条压在浅底还是深底上"""
    return 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()


# 预览图缓存：键为解析后的图像源。多套主题共用同一张 fox_bg，按源缓存能
# 让 13 个格子只持一份 QImage；用 QImage 而非 QPixmap，避免模块级 QPixmap
# 在 QApplication 析构后销毁造成退出崩。
_IMAGE_CACHE: dict = {}


# 异步加载队列：背景图解码挪出卡片构建关键路径。首次打开设置弹窗时 13 个
# tile 同步冷解码（最大 1.7MB PNG），实测热缓存 109ms、冷盘首读叠加后整段
# 阻塞主线程 >500ms（watchdog 2026-10-30 实证）。改为 tile 构造只查缓存，
# miss 入队由 singleShot 逐张加载（每张 10~30ms 分帧摊开），完成后回调刷新。
_pending_cbs: dict = {}  # source -> [callback, ...]
_load_queue: list = []


def _resolve_theme_image_source(entry: dict) -> Optional[str]:
    """解析主题背景图源：Qt 资源路径直接用，相对路径按主题目录解析"""
    spec = entry.get("image")
    if not spec:
        return None
    spec_str = str(spec)
    if spec_str.startswith(":/"):
        return spec_str
    path = theme_manager.get_theme_resource(entry.get("id", ""), os.path.basename(spec_str))
    return None if path is None else str(path)


def _request_theme_image(entry: dict, on_ready) -> None:
    """取主题背景图：缓存命中立即同步回调；miss 入队异步加载。

    缺图（加载结果为 None）也写缓存：缺图主题不该每次重绘都探一次文件。
    """
    source = _resolve_theme_image_source(entry)
    if not source:
        return
    if source in _IMAGE_CACHE:
        on_ready(_IMAGE_CACHE[source])
        return
    _pending_cbs.setdefault(source, []).append(on_ready)
    was_empty = not _load_queue
    if source not in _load_queue:
        _load_queue.append(source)
    if was_empty:
        QTimer.singleShot(30, _load_next_theme_image)


def _load_next_theme_image() -> None:
    """每帧加载一张：解码留在主线程但单张耗时不致触发阻塞阈值"""
    if not _load_queue:
        return
    source = _load_queue.pop(0)
    callbacks = _pending_cbs.pop(source, [])
    image = QImage(source)
    result = None if image.isNull() else image
    _IMAGE_CACHE[source] = result
    for callback in callbacks:
        callback(result)
    if _load_queue:
        QTimer.singleShot(30, _load_next_theme_image)


def _first_background(backgrounds: dict) -> dict:
    """取第一个「启用且有图」的背景区域（window → scene → chat_area → sidebar）"""
    for key in ("window", "scene", "chat_area", "sidebar"):
        area = backgrounds.get(key) or {}
        if area.get("image") and area.get("enabled", True):
            return area
    return {}


def collect_theme_entries(is_light: bool) -> list[dict]:
    """采集当前深浅模式下的主题预览条目。

    每项：{id, name, grad_start, grad_end, accent, image, image_opacity}。
    深浅过滤口径与 ThemeManager.is_light_theme 一致：浅色模式只列浅色主题。
    """
    entries: list[dict] = []
    for theme_id, name in theme_manager.list_themes().items():
        if bool(theme_manager.is_light_theme(theme_id)) != bool(is_light):
            continue
        theme = theme_manager.get_theme(theme_id) or {}
        window = theme.get("window") or {}
        colors = theme.get("colors") or {}
        background = _first_background(theme_manager.get_theme_backgrounds(theme_id))
        entries.append(
            {
                "id": theme_id,
                "name": name,
                "grad_start": window.get("gradient_start"),
                "grad_end": window.get("gradient_end"),
                "accent": colors.get("accent") or colors.get("border_accent"),
                "image": background.get("image"),
                "image_opacity": background.get("opacity") or 0.0,
            }
        )
    return entries


def prepare_theme_entries(config_item, settings, is_light: bool) -> list[dict]:
    """采集预览条目，并把 validator 对齐到这批判过滤后的主题集。

    当前值越界（主题被删 / 与深浅模式不匹配）时回退首项并落盘，与旧实现一致；
    无任何主题时返回空列表，调用方保持旧卡片不动。
    """
    from app.utils.config import update_theme_options

    update_theme_options()
    entries = collect_theme_entries(is_light)
    keys = [entry["id"] for entry in entries]
    if not keys:
        return []
    config_item.validator.__init__(keys)
    if config_item.value not in keys:
        settings.set(config_item, keys[0], save=True)
    return entries


# 主题源变化后自动重建网格：ThemeManager.reload() 在装插件 / 改主题文件时触发。
# 卡片列表用弱引用，回调只留存活项；重建投递到主线程，避开 reload 可能来自的
# 后台线程（UI 只在主线程动）。
_live_cards: list = []
_reload_hook_installed = False


def _install_reload_hook() -> None:
    global _reload_hook_installed
    if _reload_hook_installed:
        return
    _reload_hook_installed = True
    theme_manager.on_reload(_on_themes_reloaded)


def _on_themes_reloaded() -> None:
    QTimer.singleShot(0, _rebuild_live_cards)


def _rebuild_live_cards() -> None:
    alive = []
    for ref in _live_cards:
        card = ref()
        # 弱引用只保证 Python wrapper 活着：已 deleteLater 到位的卡片 wrapper
        # 仍在（访问属性抛 RuntimeError），必须 sip.isdeleted 硬判
        if card is None or sip.isdeleted(card):
            continue
        alive.append(ref)
        try:
            card.rebuild_entries()
        except Exception:
            pass
    _live_cards[:] = alive


class _ThemeTile(QWidget):
    """单个主题预览格：渐变底 + 背景图 + 主色条 + 名称，点击切换"""

    clicked = pyqtSignal(str)

    def __init__(self, entry: dict, parent=None):
        super().__init__(parent)
        self._entry = entry
        self._image: Optional[QImage] = None
        _request_theme_image(entry, self._on_image_ready)
        self._scaled_image: Optional[QImage] = None
        self._scaled_key: tuple = ()
        self._selected = False
        self._hover = False
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumSize(_TILE_MIN_W, _tile_height())
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def _on_image_ready(self, image: Optional[QImage]) -> None:
        """异步加载完成回调：tile 可能已被 deleteLater，活性硬判"""
        if sip.isdeleted(self):
            return
        self._image = image
        self.update()

    def refresh_metrics(self) -> None:
        """字族/字号变化后重算所占高度并重绘"""
        self.setMinimumSize(_TILE_MIN_W, _tile_height())
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(_TILE_MIN_W, _tile_height())

    def setSelected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self.update()

    # ── 绘制 ──────────────────────────────────────────

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)

        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, _RADIUS, _RADIUS)
        painter.setClipPath(path)

        start = _parse_color(self._entry.get("grad_start"), "#242428")
        end = _parse_color(self._entry.get("grad_end"), "#16161a")
        accent = _parse_color(self._entry.get("accent"), "#66c6ff")

        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, start)
        gradient.setColorAt(1.0, end)
        painter.fillPath(path, gradient)

        # 背景图按主题自身 opacity 放大后叠加：yaml 里多是 0.02~0.15，
        # 原值直接画几乎看不见，放大 3 倍并钳到 [0.15, 0.6] 才既可见又不糊住底色
        opacity = float(self._entry.get("image_opacity") or 0.0)
        if self._image is not None and opacity > 0:
            self._draw_cover(painter, rect, min(0.6, max(0.15, opacity * 3)))

        # 名称条：按渐变平均亮度选深/浅字，并压一层反色薄纱保证可读
        average = QColor(
            (start.red() + end.red()) // 2,
            (start.green() + end.green()) // 2,
            (start.blue() + end.blue()) // 2,
        )
        on_light = _luminance(average) > 140
        text_color = QColor(26, 26, 30) if on_light else QColor(244, 244, 248)
        name_font, name_bar_h = _name_pack()
        painter.fillRect(
            QRectF(rect.left(), rect.bottom() - name_bar_h, rect.width(), name_bar_h),
            QColor(255, 255, 255, 96) if on_light else QColor(0, 0, 0, 88),
        )
        painter.fillRect(
            QRectF(rect.left(), rect.bottom() - _ACCENT_BAR_H, rect.width(), _ACCENT_BAR_H),
            accent,
        )

        painter.setFont(name_font)
        painter.setPen(QPen(text_color))
        name_rect = QRectF(rect.left(), rect.bottom() - name_bar_h, rect.width(), name_bar_h - _ACCENT_BAR_H)
        text = QFontMetrics(name_font).elidedText(
            str(self._entry.get("name", "")), Qt.ElideRight, int(name_rect.width()) - 12
        )
        painter.drawText(name_rect, Qt.AlignCenter, text)

        # 边框：常态用当前主题的描边色，hover 提亮，选中用该主题 accent
        painter.setClipping(False)
        if self._selected:
            painter.setPen(QPen(accent, 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), _RADIUS - 1, _RADIUS - 1)
        else:
            border = (
                _parse_color(Colors.BORDER_ACCENT, "#f59e0b") if self._hover else _parse_color(Colors.BORDER, "#3d3d3d")
            )
            painter.setPen(QPen(border, 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect, _RADIUS, _RADIUS)

        if self._selected:
            self._draw_badge(painter, rect, accent)

    def _draw_cover(self, painter: QPainter, rect: QRectF, opacity: float) -> None:
        """背景图按 cover 方式缩放后居中裁切，兼顾高 DPI 清晰度"""
        dpr = max(1.0, self.devicePixelRatioF())
        target = QSize(int(rect.width() * dpr), int(rect.height() * dpr))
        if target.isEmpty():
            return
        cache_key = (target.width(), target.height())
        if self._scaled_image is None or self._scaled_key != cache_key:
            self._scaled_image = self._image.scaled(target, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            self._scaled_key = cache_key
        scaled = self._scaled_image
        if scaled is None or scaled.isNull():
            return
        sx = (scaled.width() - target.width()) / 2
        sy = (scaled.height() - target.height()) / 2
        painter.save()
        painter.setOpacity(opacity)
        painter.drawImage(rect, scaled, QRectF(sx, sy, target.width(), target.height()))
        painter.restore()

    def _draw_badge(self, painter: QPainter, rect: QRectF, accent: QColor) -> None:
        """选中角标：右上角实心圆 + 白勾"""
        center = QPointF(rect.right() - 14, rect.top() + 14)
        painter.setPen(Qt.NoPen)
        painter.setBrush(accent)
        painter.drawEllipse(center, 8, 8)
        pen = QPen(QColor(255, 255, 255), 2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawLine(QPointF(center.x() - 3.6, center.y()), QPointF(center.x() - 1.0, center.y() + 2.8))
        painter.drawLine(QPointF(center.x() - 1.0, center.y() + 2.8), QPointF(center.x() + 3.8, center.y() - 2.8))

    # ── 交互 ──────────────────────────────────────────

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit(str(self._entry.get("id", "")))
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)


class ThemeStyleCard(ExpandSettingCard):
    """主题风格卡：展开后是主题预览网格，点格子即切主题"""

    def __init__(self, configItem, entries: list[dict], settings, is_light: bool = False, icon=None, parent=None):
        # icon 不能传 None：SettingIconWidget 的重载解析会直接抛 TypeError
        super().__init__(icon or FluentIcon.PALETTE, "主题风格", "选择界面卡片配色方案", parent)
        self.configItem = configItem
        self.configName = configItem.name
        self._settings = settings
        self._is_light = bool(is_light)
        self._entries: dict = {}
        self._tiles: dict = {}
        self._grid_cols = 0

        self.choiceLabel = QLabel(self)
        self.choiceLabel.setObjectName("titleLabel")
        self.addWidget(self.choiceLabel)

        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(_GRID_SPACING)
        self._grid.setVerticalSpacing(_GRID_SPACING)
        self.viewLayout.setContentsMargins(_MARGIN_L, 18, _MARGIN_R, 18)
        self.viewLayout.setSpacing(0)
        self.viewLayout.addLayout(self._grid)

        configItem.valueChanged.connect(self.setValue)
        self._apply_entries(entries)
        # 主题源变化（装插件 / 放新主题）时自动重建，不必重启
        _live_cards.append(weakref.ref(self))
        _install_reload_hook()

    # ── 条目重建 ──────────────────────────────────────

    def rebuild_entries(self) -> None:
        """主题源变化后重新采集并重建网格"""
        entries = prepare_theme_entries(self.configItem, self._settings, self._is_light)
        if entries:
            self._apply_entries(entries)

    def _apply_entries(self, entries: list[dict]) -> None:
        """用给定条目重建整片网格（旧格子销毁）"""
        for tile in self._tiles.values():
            self._grid.removeWidget(tile)
            tile.hide()
            tile.deleteLater()
        self._tiles.clear()
        self._entries = {entry["id"]: entry for entry in entries}
        for theme_id, entry in self._entries.items():
            tile = _ThemeTile(entry, self.view)
            tile.clicked.connect(self._on_tile_clicked)
            self._tiles[theme_id] = tile
        self._grid_cols = 0
        self._relayout(force=True)
        self.setValue(self.configItem.value)

    # ── 选中态 ────────────────────────────────────────

    def setValue(self, value):
        """同步选中格与头部当前值文案"""
        for theme_id, tile in self._tiles.items():
            tile.setSelected(theme_id == value)
        entry = self._entries.get(value)
        if entry is not None:
            self.choiceLabel.setText(str(entry.get("name", value)))
            self.choiceLabel.adjustSize()

    def _on_tile_clicked(self, theme_id: str) -> None:
        if not theme_id or theme_id == self.configItem.value:
            return
        # 必须走 Settings.set：直接赋 item.value 只发信号不落盘
        self._settings.set(self.configItem, theme_id, save=True)

    # ── 布局 ──────────────────────────────────────────

    def _relayout(self, force: bool = False) -> None:
        """按可用宽度重排：一行 2~4 格，列宽均分"""
        available = self.width() - _MARGIN_L - _MARGIN_R
        if available <= 0:
            cols = 3
        else:
            cols = (available + _GRID_SPACING) // (_TILE_MIN_W + _GRID_SPACING)
            cols = max(2, min(4, int(cols)))
        if not force and cols == self._grid_cols:
            return
        self._grid_cols = cols
        for tile in self._tiles.values():
            self._grid.removeWidget(tile)
        for index, tile in enumerate(self._tiles.values()):
            self._grid.addWidget(tile, index // cols, index % cols)
        for col in range(4):
            self._grid.setColumnStretch(col, 1 if col < cols else 0)
        # 重排后两级布局的 sizeHint 还是旧值：必须先失效并立即重算，
        # 否则 _adjustViewSize 拿旧高度定格（变宽留白 / 变窄把网格裁在可视区外）
        self._grid.invalidate()
        self.viewLayout.invalidate()
        self.viewLayout.activate()
        self._adjustViewSize()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()

    # ── 主题刷新 ──────────────────────────────────────

    def refresh_style(self):
        """主题切换后重绘网格：边框/描边取自当前主题色，重绘即生效"""
        for tile in self._tiles.values():
            tile.update()

    def changeEvent(self, event):
        # 字族/字号切换由 apply_font_size_to_widget 对本卡 setFont 触发；
        # 名称条高度随字号变化，必须重算几何，否则大字号被固定条高裁掉。
        # getattr 兜底：super().__init__ 期间也可能派发 FontChange，那时 _tiles 还没建
        if event.type() == QEvent.FontChange:
            tiles = getattr(self, "_tiles", None)
            if tiles:
                for tile in tiles.values():
                    tile.refresh_metrics()
                self._relayout(force=True)
        super().changeEvent(event)

# -*- coding: utf-8 -*-
"""
Gitee 账号绑定设置卡片

继承 SettingCard：图标 + 标题 + 说明文字自动布局，
右侧追加头像 + 绑定/解绑按钮。
"""

import hashlib
import sip
import threading
import time
import webbrowser
from loguru import logger
from PyQt5.QtCore import Qt, QSize, pyqtSignal, QPoint, QPointF, QRectF, QTimer
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon, InfoBar, InfoBarPosition, MaskDialogBase, SettingCard, SwitchButton

from app.core.infra import window_registry
from app.gateway.auth.base import CANCEL_MESSAGE
from app.utils.config import Settings
from app.utils.design_tokens import Colors, font_size_css, scale_font_size
from app.utils.utils import get_font_family_css, get_icon, get_unified_font
from app.widgets.common_dialogs import ConfirmDialog
from app.widgets.elided_label import _ElidedLabel

_AVATAR_COLORS = [
    "#c71d23",
    "#e74c3c",
    "#e67e22",
    "#f39c12",
    "#27ae60",
    "#2ecc71",
    "#1abc9c",
    "#3498db",
    "#2980b9",
    "#9b59b6",
    "#8e44ad",
    "#34495e",
]

# QEvent 枚举值：PyQt5 未暴露这两个名字（QEvent.DevicePixelRatioChange 访问即
# AttributeError），Qt5 的数值固定，直接写常量。用于 DPR 变化时使头像缩放缓存失效。
_EV_DEVICE_PIXEL_RATIO_CHANGE = 154  # QEvent::DevicePixelRatioChange (Qt 5.9+)
_EV_SCREEN_CHANGE_INTERNAL = 175  # QEvent::ScreenChangeInternal


def _color_for_name(name: str) -> str:
    idx = int(hashlib.md5(name.encode()).hexdigest(), 16) % len(_AVATAR_COLORS)
    return _AVATAR_COLORS[idx]


def _avatar_font(size: int) -> QFont:
    """头像字母字体：系统 UI 无衬线 + 像素档随头像尺寸

    不用 get_unified_font（楷体）：楷体字形斜倾且 em 框偏移，单字母在圆内
    视觉上偏斜偏心（用户实测 D 不在圆心）。缩写头像（GitHub/Teams 风）惯例
    用无衬线粗体。显式字体族链：QFont() 默认族在某些机器解析到缺拉丁字形的
    字体（实测 M/m 渲染成方块），Segoe UI/YaHei 链保证字形齐全。
    """
    f = QFont()
    f.setFamilies(["Segoe UI", "Microsoft YaHei UI", "Helvetica", "Arial"])
    f.setPixelSize(max(9, int(size * 0.65)))
    f.setBold(True)
    return f


def _avatar_palette(base: QColor) -> tuple:
    """头像配色：降饱和基色 + 对角渐变深端色

    12 色表本身高饱和，纯色填充在浅色卡片上很扎眼（用户反馈「难看」）；
    统一压饱和 28%，深端 darker(122) 约深 18%，立体感交给渐变而非色相差。
    """
    c = QColor(base)
    h, s, l, _a = c.getHslF()
    if h >= 0:  # 灰阶（h=-1）无饱和度可降
        c.setHslF(h, s * 0.72, l)
    return c, QColor(c).darker(122)


def _avatar_cache_path(owner: str):
    """头像磁盘缓存路径：按 owner 命名，进程间复用（头像变化低频）"""
    from app.utils.utils import get_app_data_dir

    return get_app_data_dir() / "cache" / f"gitee_avatar_{owner}.png"


def _fetch_avatar_bytes() -> bytes | None:
    """后台线程用：token → avatar_url → 下载图片字节；失败返回 None（不抛）

    全程 requests 同步调用（get_bound_info 内部可能触发 token 刷新，网络超时 15s），
    绝不能进主线程。账户行与设置卡共用，避免两套拉取逻辑漂移。
    """
    try:
        from app.gateway.auth import get_oauth_backend
        from app.gateway.auth.gitee import fetch_user_avatar_url

        info = get_oauth_backend("gitee").get_bound_info()
        if not info:
            return None
        url, err = fetch_user_avatar_url(info["token"])
        if not url:
            logger.warning(f"[GiteeAvatar] 拉取头像 URL 失败: {err}")
            return None
        import requests as _requests

        # 显式 UA：foruda CDN 对空 UA / 爬虫默认 UA 偶发返回降级图，带 UA 稳定给 512px
        resp = _requests.get(url, timeout=10, headers={"User-Agent": "DriFox/1.0"})
        if resp.status_code != 200 or not resp.content:
            logger.warning(f"[GiteeAvatar] 下载头像失败: HTTP {resp.status_code}")
            return None
        return resp.content
    except Exception as e:
        logger.warning(f"[GiteeAvatar] 加载真实头像异常: {e}")
        return None


class _AvatarCircleWidget(QWidget):
    """使用 QPainter 绘制的圆形头像 — DPI 感知

    替代 QLabel + QPixmap + setDevicePixelRatio 方案。
    直接在 paintEvent 中用 QPainter 绘制圆 + 文字，Qt 自动处理 DPI 缩放，
    避免物理像素四舍五入导致的逻辑尺寸不匹配和裁剪问题。
    """

    clicked = pyqtSignal()

    def __init__(self, text: str = "?", parent=None):
        super().__init__(parent)
        self._text = text[0].upper() if text else "?"
        self._bg_color = QColor(_color_for_name(text))
        self._size = 28
        self._invalid = False  # 绑定失效警示（红边 + 红点角标）
        self._image: QPixmap | None = None  # 真实头像原图（未缩放）；None 回退字母渐变球
        # 缩放结果缓存：键为 (逻辑边长, dpr)。高分屏下每帧重采样 512→58 物理像素
        # 约 0.1ms，卡片刷新/悬浮动画期间会被反复触发，缓存掉。
        self._scaled_cache: QPixmap | None = None
        self._scaled_key: tuple | None = None
        self.setFixedSize(self._size, self._size)
        self.setCursor(Qt.PointingHandCursor)

    def set_image(self, pixmap: "QPixmap | None"):
        """设置真实头像图（圆形裁剪渲染）；None 清除回退字母渐变球"""
        if self._image is None and pixmap is None:
            return
        self._image = pixmap
        self._scaled_cache = None
        self._scaled_key = None
        self.update()

    def image(self) -> "QPixmap | None":
        """当前真实头像图（未拉到时 None，调用方自行回退字母球）"""
        return self._image

    def has_image(self) -> bool:
        return self._image is not None and not self._image.isNull()

    def set_avatar(self, text: str):
        """更新头像文字和背景色"""
        self._text = text[0].upper() if text else "?"
        self._bg_color = QColor(_color_for_name(text))
        self.setToolTip(text)
        self.update()

    def set_invalid(self, invalid: bool):
        """设置绑定失效警示样式（红色圆环边框 + 右上角红点角标）"""
        if self._invalid == invalid:
            return
        self._invalid = invalid
        self.update()

    def set_size(self, size: int):
        """更新头像逻辑尺寸"""
        self._size = size
        self.setFixedSize(size, size)
        self.update()

    def _scaled_for(self, ellipse_rect: QRectF, device) -> QPixmap:
        """按物理像素预缩放头像（带 dpr），结果按 (边长, dpr) 缓存

        为什么缩到「物理」而不是「逻辑」：paintEvent 的 QPainter 已带 DPR 变换
        （逻辑 1 单位 = 物理 dpr 像素）。若把 pixmap 缩到逻辑尺寸再交给它绘制，
        等于 512 → 26 → 放大回 58 物理像素，先砍掉 ~82% 再插值，高分屏上头像发糊。
        缩到物理尺寸并 setDevicePixelRatio(dpr)，绘制时是 1:1 映射，细节全留
        （实测 2.25x 屏 Laplacian 方差 17 → 123）。dpr=1 时两者等价，无副作用。
        """
        dpr = float(device.devicePixelRatioF()) if device is not None else 1.0
        if dpr <= 0:
            dpr = 1.0
        side = max(1, int(round(ellipse_rect.width())))
        key = (side, round(dpr, 3))
        if self._scaled_cache is not None and self._scaled_key == key:
            return self._scaled_cache

        src = QPixmap(self._image)
        src.setDevicePixelRatio(1.0)  # 源按物理像素计，勿让它自带 dpr 参与换算
        px = max(1, int(round(ellipse_rect.width() * dpr)))
        out = src.scaled(px, px, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        out.setDevicePixelRatio(dpr)
        self._scaled_cache = out
        self._scaled_key = key
        return out

    def event(self, event):
        """跨屏拖动 / 系统缩放变更 → DPR 变化，缩放缓存失效

        不监听会使窗口移到不同缩放比的显示器后继续使用旧 dpr 的位图（发糊或过大）。

        用数值而不用 QEvent.DevicePixelRatioChange：PyQt5 未暴露这两个枚举
        （访问即 AttributeError，event() 里抛异常会刷爆日志），Qt5 的数值如下。
        """
        et = int(event.type())
        if et in (_EV_DEVICE_PIXEL_RATIO_CHANGE, _EV_SCREEN_CHANGE_INTERNAL):
            self._scaled_cache = None
            self._scaled_key = None
        return super().event(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        rect = self.rect()
        size = min(rect.width(), rect.height())

        # 圆形裁剪区域（留 1px 内边距避免抗锯齿溢出）
        ellipse_rect = QRectF(1, 1, size - 2, size - 2)

        if self.has_image():
            # 真实头像：cover 缩放 + 圆形裁剪，同字母球一致的描边与失效警示
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            clip = QPainterPath()
            clip.addEllipse(ellipse_rect)
            painter.setClipPath(clip)
            dpr = painter.device().devicePixelRatioF() if painter.device() else 1.0
            scaled = self._scaled_for(ellipse_rect, painter.device() or self)
            # source rect 必须按物理像素给：pixmap 带 dpr 时 Qt 直读物理源矩形，
            # 传逻辑坐标（26×26）会被当成 26 个物理像素，只取到原图左上角一小块
            # 再放大铺满整圆 —— 即「只有左上角一点点」。
            pw = max(1, int(round(ellipse_rect.width() * (dpr or 1.0))))
            ph = max(1, int(round(ellipse_rect.height() * (dpr or 1.0))))
            sx = (scaled.width() - pw) / 2.0
            sy = (scaled.height() - ph) / 2.0
            painter.drawPixmap(
                QPointF(ellipse_rect.left(), ellipse_rect.top()),
                scaled,
                QRectF(sx, sy, pw, ph),
            )
            painter.setClipping(False)
            painter.setPen(QPen(QColor(0, 0, 0, 30), 1.0))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(ellipse_rect.adjusted(0.5, 0.5, -0.5, -0.5))
        else:
            # 字母渐变球（无真实头像时的回退）
            # 对角渐变填充（左上亮 → 右下深）：纯色圆在卡片上扁平扎眼，渐变给球面感
            c1, c2 = _avatar_palette(self._bg_color)
            grad = QLinearGradient(ellipse_rect.topLeft(), ellipse_rect.bottomRight())
            grad.setColorAt(0.0, c1)
            grad.setColorAt(1.0, c2)
            painter.setPen(Qt.NoPen)
            painter.setBrush(grad)
            painter.drawEllipse(ellipse_rect)

            # 顶部柔高光：上半圆内白→透明垂直渐变，clip 在圆内（球面受光的点睛笔）
            painter.save()
            circle = QPainterPath()
            circle.addEllipse(ellipse_rect)
            painter.setClipPath(circle)
            hl = QLinearGradient(0, ellipse_rect.top(), 0, ellipse_rect.center().y())
            hl.setColorAt(0.0, QColor(255, 255, 255, 72))
            hl.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.setBrush(hl)
            painter.drawRect(ellipse_rect)
            painter.restore()

            # 1px 半透明深色内描边：浅色卡上让头像有清晰边界（无描边时亮色圆糊进底色）
            painter.setPen(QPen(QColor(0, 0, 0, 30), 1.0))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(ellipse_rect.adjusted(0.5, 0.5, -0.5, -0.5))

            # 居中白色文字（96% 不透明度）：按字形 ink rect 居中——AlignCenter 按字体
            # em 框对齐，大写字母会偏高偏心；boundingRect 给实际笔画范围，视觉正中
            painter.setPen(QColor(255, 255, 255, 245))
            font = _avatar_font(size)
            painter.setFont(font)
            br = QFontMetrics(font).boundingRect(self._text)
            baseline = QPointF(
                (size - br.width()) / 2.0 - br.x(),
                (size - br.height()) / 2.0 - br.y(),
            )
            painter.drawText(baseline, self._text)

        # 绑定失效警示：红色圆环边框 + 右上角红点角标
        if self._invalid:
            ring_width = max(1.5, size * 0.08)
            painter.setPen(QPen(QColor("#f85149"), ring_width))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(ellipse_rect)
            dot_r = max(2.5, size * 0.22)
            painter.setPen(QPen(QColor("#ffffff"), 1.0))
            painter.setBrush(QColor("#f85149"))
            painter.drawEllipse(QRectF(size - 2 * dot_r - 1, 0, 2 * dot_r, 2 * dot_r))

    def mousePressEvent(self, event: QMouseEvent):
        self.clicked.emit()


class _ClickableElidedLabel(_ElidedLabel):
    clicked = pyqtSignal()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


# ── 仓库可见性选择弹窗（参考 _ProjectExportChoiceDialog） ──


class _RepoVisibilityDialog(MaskDialogBase):
    """选择公开/私有仓库 — 与项目导出弹窗统一风格"""

    PUBLIC = False
    PRIVATE = True

    chosen = pyqtSignal(bool)  # True=私有, False=公开

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        Colors.refresh()
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))

        self.widget.setObjectName("repoVisibilityDialog")
        self.widget.setStyleSheet(f"""
            #{self.widget.objectName()} {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(12)

        title_label = QLabel("🔒 仓库可见性", self.widget)
        title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(16)}; font-weight: bold;"
        )
        layout.addWidget(title_label)

        hint_label = QLabel("选择要创建的仓库类型：", self.widget)
        hint_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(11)}; padding-left: 2px;"
        )
        layout.addWidget(hint_label)

        layout.addSpacing(4)

        btn_style = f"""
            QPushButton {{
                background-color: {Colors.CARD_BG.format(alpha=180)};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                padding: 8px 16px;
                text-align: left;
                {get_font_family_css()} {font_size_css(14)}
            }}
            QPushButton:hover {{
                background-color: {Colors.HOVER_BG};
                border-color: {Colors.INFO};
            }}
        """
        hint_style = (
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(10)}; padding-left: 4px;"
        )

        # ── 公开按钮 ──
        public_btn = QPushButton("🌐  公开仓库", self.widget)
        public_btn.setCursor(Qt.PointingHandCursor)
        public_btn.setFixedHeight(56)
        public_btn.setStyleSheet(btn_style)
        public_btn.clicked.connect(lambda: self._choose(False))
        layout.addWidget(public_btn)

        public_hint = QLabel("任何人可见，适合分享用途", self.widget)
        public_hint.setStyleSheet(hint_style)
        layout.addWidget(public_hint)

        # ── 私有按钮 ──
        private_btn = QPushButton("🔒  私有仓库", self.widget)
        private_btn.setCursor(Qt.PointingHandCursor)
        private_btn.setFixedHeight(56)
        private_btn.setStyleSheet(btn_style)
        private_btn.clicked.connect(lambda: self._choose(True))
        layout.addWidget(private_btn)

        private_hint = QLabel("仅自己可访问，链接需登录后查看", self.widget)
        private_hint.setStyleSheet(hint_style)
        layout.addWidget(private_hint)

        self.widget.setFixedSize(400, 280)
        self._center()

    def _choose(self, is_private: bool):
        self.close()
        self.chosen.emit(is_private)

    def _center(self):
        x = max(0, (self.width() - self.widget.width()) // 2)
        y = max(0, (self.height() - self.widget.height()) // 2)
        self.widget.move(x, y)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._center()


# ── Tab 模式紧凑账户行 ────────────────────────────────────


class GiteeAccountRow(QFrame):
    """Tab 模式底部的紧凑 Gitee 账户快捷栏。"""

    oauthResult = pyqtSignal(bool, str)
    # 真实头像下载完成（图片字节，后台线程发、主线程槽收→QPixmap 只能在主线程构造）
    _avatarFetched = pyqtSignal(bytes)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("giteeAccountRow")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.cfg = Settings.get_instance()
        self._binding = False
        # 绑定中止信号：与 GiteeCard 同一语义，用户点「中止授权」时置位
        self._bind_cancel: threading.Event | None = None
        self._bound_owner = ""
        self._bound_repo = ""
        self._compact = False  # 是否紧凑模式（收起时垂直堆叠）
        self._invalid = False  # 绑定是否已失效（token 失效，需重新绑定）
        self._avatar_only = False  # 仅头像形态（侧边栏折叠态）

        from app.core.sync.config_sync import ConfigSyncService

        self._sync_svc = ConfigSyncService.get_instance()
        self._avatar_fetching = False  # 头像下载防重入（主线程读写，无竞争）
        self._avatar_owner_loaded = ""  # 已成功拉到真实头像的 owner（进程内去重）
        self._avatarFetched.connect(self._on_avatar_fetched)
        self._setup_ui()
        self._connect_config_signals()
        self._connect_sync_signals()
        self._connect_uploader_signals()
        self._refresh_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 4, 6)
        layout.setSpacing(7)

        avatar_size = self._desired_avatar_size()
        self._avatar = _AvatarCircleWidget("?", self)
        self._avatar.set_size(avatar_size)
        layout.addWidget(self._avatar)

        # 单行布局：名字 + 齿轮设置按钮，状态文案移入 tooltip
        self._name_label = _ClickableElidedLabel("", self)
        layout.addWidget(self._name_label, 1)

        from qfluentwidgets import FluentIcon as _FIF
        from qfluentwidgets import TransparentToolButton as _TransparentToolButton

        # popup 关闭时刻（monotonic 秒）：防抖用，见 _toggle_popup
        self._popup_closed_at = 0.0

        def _make_icon_btn(icon, tooltip: str) -> "_TransparentToolButton":
            btn = _TransparentToolButton(self)
            btn.setIcon(icon)
            btn.setFixedSize(scale_font_size(20), scale_font_size(20))
            btn.setIconSize(QSize(scale_font_size(13), scale_font_size(13)))
            btn.setToolTip(tooltip)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFocusPolicy(Qt.NoFocus)
            return btn

        # 设置按钮（齿轮图标，点击打开完整设置卡片）
        self._settings_btn = _make_icon_btn(_FIF.SETTING, "设置")
        self._settings_btn.clicked.connect(self._toggle_settings_card)
        layout.addWidget(self._settings_btn)

        # 整块区域点击触发 Gitee 管理弹窗（头像/名称）
        # 图标按钮独立：外链打开仓库 / 齿轮打开完整设置卡片
        self.setCursor(Qt.PointingHandCursor)
        self._avatar.setCursor(Qt.PointingHandCursor)
        self._name_label.setCursor(Qt.PointingHandCursor)
        self._avatar.clicked.connect(self._toggle_popup)
        self._name_label.clicked.connect(self._toggle_popup)
        self.oauthResult.connect(self._on_oauth_result)
        self._popup: "_GiteeMorePopup" | None = None

    def _connect_config_signals(self):
        for item in (
            self.cfg.gitee_bound,
            self.cfg.gitee_user_owner,
            self.cfg.gitee_user_repo,
        ):
            item.valueChanged.connect(self._on_config_changed)

    def _on_config_changed(self, _value=None):
        """配置变化：重新绑定成功（bound+owner）→ 恢复绑定态；否则按现状刷新"""
        is_bound = bool(self.cfg.gitee_bound.value)
        owner = str(self.cfg.gitee_user_owner.value or "")
        if is_bound and owner:
            # 重新绑定成功 → 清除失效标志，恢复绑定态
            self._invalid = False
        self._refresh_ui()

    def _connect_sync_signals(self):
        """监听 ConfigSyncService.syncDone：token 失效 → 失效标识；成功 → 恢复"""
        try:
            self._sync_svc.syncDone.disconnect(self._on_sync_done)
        except TypeError:
            pass
        self._sync_svc.syncDone.connect(self._on_sync_done)

    def _connect_uploader_signals(self):
        """监听 GiteeUploader.tokenInvalid：上传 401 重试失败 → 失效标识"""
        try:
            from app.gateway.utils.gitee_uploader import GiteeUploader

            uploader = GiteeUploader.get_instance()
            try:
                uploader.tokenInvalid.disconnect(self._on_uploader_token_invalid)
            except TypeError:
                pass
            uploader.tokenInvalid.connect(self._on_uploader_token_invalid)
        except Exception as e:
            logger.warning(f"[GiteeAccountRow] 连接上传器失效信号失败: {e}")

    def _on_uploader_token_invalid(self):
        """上传 401 重试仍失败 → 绑定失效，置红标并触发全局提醒"""
        self.set_invalid(True)
        self._notify_token_invalid_reminder()

    def _notify_token_invalid_reminder(self):
        """触发全局「绑定已失效」提醒（去重/开关守卫在 controller 内）"""
        try:
            from app.widgets.cards.global_card_controller import get_global_card_controller

            ctrl = get_global_card_controller()
            if ctrl is not None:
                ctrl.check_gitee_token_invalid_reminder()
        except Exception as e:
            logger.warning(f"[GiteeAccountRow] 触发失效提醒失败: {e}")

    def _on_sync_done(self, success: bool, msg: str):
        """同步完成回调：维护绑定失效状态。

        - msg 含「已失效」→ token 被吊销，绑定已失效；
        - 同步成功（True）→ 绑定恢复有效；
        - 网络异常（刷新失败）不算失效，保持现状。
        """
        if "已失效" in msg:
            self.set_invalid(True)
        elif success:
            self.set_invalid(False)

    def set_invalid(self, invalid: bool):
        """设置绑定失效状态：True 显示红色失效标识，False 恢复绑定态"""
        if self._invalid == invalid:
            return
        self._invalid = invalid
        self._refresh_ui()

    def _refresh_ui(self, _value=None):
        is_bound = bool(self.cfg.gitee_bound.value)
        owner = str(self.cfg.gitee_user_owner.value or "")
        repo = str(self.cfg.gitee_user_repo.value or "")

        if self._invalid:
            # 绑定已失效：红色警示，明显区别于「从未绑定」（失效信号优先于 cfg 绑定态）
            self._bound_owner = ""
            self._bound_repo = ""
            self._avatar.set_image(None)
            self._avatar.set_avatar(owner or "?")
            self._avatar.set_invalid(True)
            self._avatar.setToolTip("绑定已失效，点击重新绑定")
            self._name_label.setText("绑定已失效")
            self._name_label.setToolTip("绑定已失效，点击重新绑定")
        elif is_bound and owner:
            self._bound_owner = owner
            self._bound_repo = repo
            self._avatar.set_avatar(owner)
            self._avatar.set_invalid(False)
            self._avatar.setToolTip(f"{owner}/{repo} · 点击打开仓库")
            self._name_label.setText(owner)
            self._name_label.setToolTip(f"{owner}/{repo} · 点击管理")
            self._load_avatar_async(owner)
        else:
            self._bound_owner = ""
            self._bound_repo = ""
            self._avatar.set_image(None)
            self._avatar.set_avatar("?")
            self._avatar.set_invalid(False)
            self._avatar.setToolTip("未绑定")
            self._name_label.setText("Gitee 未绑定")
            self._name_label.setToolTip("绑定后可备份与分享")

        self._settings_btn.setEnabled(not self._binding)
        self._apply_style()
        # 浮层操作按钮同步进行中态：绑定等待时变「中止授权」可点
        # 必须 shiboken isValid 硬判：wrapper 已析构时读 Python 侧属性不抛异常
        if self._popup is not None and sip.isValid(self._popup):
            self._popup._refresh_account_state()

    # ── 真实头像拉取（绑定态：API 拿 avatar_url → 下载 → 圆形渲染） ──

    def _load_avatar_async(self, owner: str):
        """异步加载真实头像：缓存命中先即时显示，再后台拉新（不阻塞 UI）"""
        if self._avatar_fetching or (owner == self._avatar_owner_loaded and self._avatar.has_image()):
            return
        cache = _avatar_cache_path(owner)
        if cache.exists():
            pm = QPixmap(str(cache))
            if not pm.isNull():
                self._avatar.set_image(pm)
                self._avatar_owner_loaded = owner
        worker = threading.Thread(target=self._fetch_avatar_worker, daemon=True)
        self._avatar_fetching = True
        worker.start()

    def _fetch_avatar_worker(self):
        """后台线程：拉取图片字节 → 信号回主线程（QPixmap 只能在主线程构造）"""
        try:
            data = _fetch_avatar_bytes()
            if data:
                self._avatarFetched.emit(data)
        finally:
            self._avatar_fetching = False

    def _on_avatar_fetched(self, data: bytes):
        """主线程槽：字节 → QPixmap → 圆形渲染 + 写磁盘缓存"""
        pm = QPixmap()
        if not pm.loadFromData(data):
            return
        self._avatar.set_image(pm)
        self._avatar_owner_loaded = self._bound_owner
        # 弹窗大头像同步（可能未创建）
        if self._popup is not None and sip.isValid(self._popup):
            self._popup._popup_avatar.set_image(pm)
        try:
            cache = _avatar_cache_path(self._bound_owner)
            cache.parent.mkdir(parents=True, exist_ok=True)
            pm.save(str(cache), "PNG")
        except Exception as e:
            logger.warning(f"[GiteeAccountRow] 头像缓存写盘失败: {e}")

    def _apply_style(self):
        self.setStyleSheet("""
            QFrame#giteeAccountRow {
                background: transparent;
                border: none;
            }
        """)
        # 失效时名称显示红色警示；字重降为 500 避免与头像/图标争抢视觉
        name_color = "#f85149" if self._invalid else Colors.TEXT_PRIMARY
        self._name_label.setStyleSheet(
            f"color: {name_color}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(12)}; font-weight: 500;"
        )
        # 图标染灰（弱于前景色）；主题/字号变化经 refresh_style → _refresh_ui → 本方法重染
        from qfluentwidgets import FluentIcon as _FIF

        muted = QColor(Colors.TEXT_MUTED)
        self._settings_btn.setIcon(_FIF.SETTING.colored(muted, muted))
        self._settings_btn.setStyleSheet("""
            TransparentToolButton {
                background: transparent;
                border: none;
            }
        """)

    def _open_repository(self):
        if self._bound_owner and self._bound_repo:
            webbrowser.open(f"https://gitee.com/{self._bound_owner}/{self._bound_repo}")

    def _on_bind(self):
        if self._binding:
            return

        dialog = _RepoVisibilityDialog(self.window())
        dialog.chosen.connect(self._start_oauth_with_backup)
        dialog.exec_()

    def _start_oauth_with_backup(self, repo_private: bool):
        self._sync_svc.backup_local()
        self._start_oauth(repo_private)

    def _start_oauth(self, repo_private: bool):
        self._binding = True
        self._bind_cancel = threading.Event()
        self._refresh_ui()
        worker = threading.Thread(
            target=self._do_oauth,
            args=(repo_private, self._bind_cancel),
            daemon=True,
        )
        worker.start()

    def _abort_binding(self):
        """中止进行中的绑定：置位取消信号，后台线程 0.5s 内退出等待"""
        if self._bind_cancel is not None:
            self._bind_cancel.set()
            self._bind_cancel = None

    def _do_oauth(self, repo_private: bool, cancel_event=None):
        try:
            from app.gateway.auth import get_oauth_backend

            success, message = get_oauth_backend("gitee").bind(
                repo_private=repo_private, cancel_event=cancel_event
            )
            self.oauthResult.emit(success, message)
        except Exception as error:
            logger.error(f"[GiteeAccountRow] OAuth 异常: {error}")
            self.oauthResult.emit(False, f"绑定异常：{error}")

    def _on_oauth_result(self, success: bool, message: str):
        self._binding = False
        self._bind_cancel = None
        if success:
            from app.gateway.utils.gitee_uploader import GiteeUploader

            GiteeUploader.get_instance().reset_config()
            self._refresh_ui()
            self._auto_enable_sync()
            InfoBar.success(
                title="绑定成功",
                content=message,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
            return

        self._refresh_ui()
        if message == CANCEL_MESSAGE:
            # 主动中止不是失败：安静提示，不用错误色
            InfoBar.info(
                title="已中止授权",
                content="绑定已中止，可随时重新发起。",
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
            return
        InfoBar.error(
            title="绑定失败",
            content=message,
            position=InfoBarPosition.TOP_RIGHT,
            duration=5000,
            parent=self.window(),
        )

    def _auto_enable_sync(self):
        if self._sync_svc._state != "disabled":
            return
        from app.gateway.auth import get_oauth_backend

        bound_info = get_oauth_backend("gitee").get_bound_info()
        if bound_info and bound_info.get("token") and bound_info.get("owner"):
            logger.info("[GiteeAccountRow] 检测到已绑定，自动启动配置同步")
            self._sync_svc.enable(bound_info["token"], bound_info["owner"])

    def _on_unbind(self):
        owner = str(self.cfg.gitee_user_owner.value or "")
        dialog = ConfirmDialog(
            title="确认解绑",
            content=f"解绑后上传将恢复使用共享图床仓库。\n当前绑定：{owner}",
            confirm_text="确定解绑",
            cancel_text="取消",
            parent=self.window(),
        )
        dialog.confirmed.connect(self._do_unbind)
        dialog.exec_()

    def _do_unbind(self):
        try:
            from app.gateway.auth import get_oauth_backend
            from app.gateway.utils.gitee_uploader import GiteeUploader

            self._sync_svc.disable()
            success, message = get_oauth_backend("gitee").unbind()
            if not success:
                raise RuntimeError(message)
            GiteeUploader.get_instance().reset_config()

            if self._sync_svc.restore_local():
                self.cfg.load()

            self._invalid = False  # 主动解绑 → 恢复未绑定态，不再显示失效警示
            self._refresh_ui()
            InfoBar.success(
                title="已解绑",
                content="Gitee 账号已解绑，配置已恢复",
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
        except Exception as error:
            logger.error(f"[GiteeAccountRow] 解绑异常: {error}")
            self._refresh_ui()
            InfoBar.error(
                title="解绑失败",
                content=str(error),
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )

    def set_show_only_avatar(self, avatar_only: bool):
        """收起侧边栏时仅显示头像，隐藏文字和图标按钮"""
        self._avatar_only = avatar_only
        self._settings_btn.setVisible(not avatar_only)
        self._name_label.setVisible(not avatar_only)
        # 仅头像形态左右 margin 对称（5px）：46px 窄条下头像放大后不贴边、水平居中
        self.layout().setContentsMargins(5, 6, 5, 6) if avatar_only else self.layout().setContentsMargins(
            8, 6, 4, 6
        )
        self._avatar.set_size(self._desired_avatar_size())

    def _desired_avatar_size(self) -> int:
        """头像尺寸：展开态跟随字号档放大；折叠窄条 clamp 到 36（46 - 对称 margin 10）

        真实头像可读性高，展开基准从 26 提到 32（用户要求）。
        """
        base = scale_font_size(32)
        return min(base, 36) if self._avatar_only else base

    def refresh_style(self):
        """主题或字号变化后重建头像、尺寸和样式。"""
        self._avatar.set_size(self._desired_avatar_size())
        btn_size = scale_font_size(20)
        self._settings_btn.setFixedSize(btn_size, btn_size)
        self._settings_btn.setIconSize(QSize(scale_font_size(13), scale_font_size(13)))
        self._refresh_ui()

    def _toggle_popup(self):
        """点击整块区域切换浮动卡片显示状态

        绑定等待中仍允许打开：浮层主操作此时是「中止授权」，是退出等待的唯一入口。
        """
        # 防抖：Qt.Popup 被点击外部关闭的瞬间，⚙ 按钮的 click 才到达；
        # 此时 _popup 已被置 None，不拦住就会「关闭后立刻重开」（表现为点切换失效）
        if time.monotonic() - self._popup_closed_at < 0.3:
            return

        # 防御性检查：如果 C++ 对象已被删除，清理引用
        if self._popup is not None and sip.isdeleted(self._popup):
            self._popup = None

        if self._popup and self._popup.isVisible():
            self._popup.close()
            self._popup = None
            return

        # 确保旧 popup 已清理
        if self._popup:
            self._popup.deleteLater()
            self._popup = None

        popup = _GiteeMorePopup(self)
        popup.destroyed.connect(self._on_popup_closed)
        popup.adjustSize()
        popup_width = max(popup.sizeHint().width(), 220)
        popup_height = popup.sizeHint().height()

        # 定位：在当前行上方弹出，与窗口左边缘对齐，确保不超出窗口
        row_global = self.mapToGlobal(QPoint(0, 0))
        window = self.window()
        if window:
            win_rect = window.frameGeometry()
        else:
            win_rect = QApplication.primaryScreen().availableGeometry()

        # X：贴近窗口左边框（留 3px 呼吸），不超出窗口左右边界
        x = win_rect.left() + 3
        if x + popup_width > win_rect.right() - 4:
            x = win_rect.right() - 4 - popup_width
        if x < win_rect.left() + 2:
            x = win_rect.left() + 2

        # Y：贴着本行（设置栏）上方弹出，留 3px 呼吸。
        # 按「可见卡片」高度算，不能用 popup 外框高度：外框底部有
        # _content_margins[3] 的透明阴影留白，算进去会凭空多出一截间距
        # （字号 delta 越大留白越宽，弹窗看起来离分隔线越远）。
        visible_height = popup_height - popup.content_margins()[3]
        y = row_global.y() - visible_height - 3
        if y < win_rect.top() + 4:
            # 空间不够则向下弹出
            y = row_global.y() + self.height() + 3
            # 向下弹出也超出底部时，对齐窗口底部
            if y + popup_height > win_rect.bottom() - 4:
                y = win_rect.bottom() - 4 - popup_height
        if y < win_rect.top() + 4:
            y = win_rect.top() + 4

        popup.setFixedSize(popup_width, popup_height)
        popup.move(x, y)
        popup.show()
        self._popup = popup

    def _toggle_settings_card(self):
        """打开完整设置卡片"""
        # 沿父链向上找 OpenAIChatToolWindow._toggle_settings_card
        parent = self.parent()
        while parent is not None:
            if hasattr(parent, "_toggle_settings_card"):
                parent._toggle_settings_card()
                return
            parent = parent.parent()
        # 兜底：通过 TabManagerWindow 获取当前窗口
        from app.widgets.tab_manager_window import TabManagerWindow

        tm = TabManagerWindow.get_instance()
        if tm:
            current = tm.get_current_window()
            if current and hasattr(current, "_toggle_settings_card"):
                current._toggle_settings_card()

    def _on_popup_closed(self):
        """浮动卡片关闭后的清理"""
        self._popup = None
        self._popup_closed_at = time.monotonic()


# ── 浮动卡片 ──────────────────────────────────────────────


def _menu_icon_pixmap(icon_src, size: int) -> QPixmap:
    """菜单行图标 → QPixmap：字符串走主题感知资源图标，FluentIcon 枚举走动态主题图标"""
    if isinstance(icon_src, str):
        return get_icon(icon_src).pixmap(size, size)
    if hasattr(icon_src, "qicon"):
        return icon_src.qicon().pixmap(size, size)
    if hasattr(icon_src, "icon"):
        return icon_src.icon().pixmap(size, size)
    return QPixmap()


class _MenuRow(QPushButton):
    """WorkBuddy 风格菜单行：图标 + 文字 + 右侧插槽（开关/箭头），hover 圆角高亮

    借 QPushButton 的 :hover/:pressed 伪类实现高亮；文字/图标子控件
    设鼠标穿透保证整行 hover 生效。右侧交互控件（SwitchButton）自身
    消费鼠标事件，点击开关时行高亮暂时消失属预期行为。
    """

    def __init__(self, icon_src, text: str, right: QWidget | None = None, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(scale_font_size(34))
        row_layout = QHBoxLayout(self)
        row_layout.setContentsMargins(scale_font_size(12), 0, scale_font_size(12), 0)
        row_layout.setSpacing(scale_font_size(10))

        icon_lbl = QLabel(self)
        icon_lbl.setPixmap(_menu_icon_pixmap(icon_src, scale_font_size(16)))
        icon_lbl.setFixedSize(scale_font_size(16), scale_font_size(16))
        icon_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        row_layout.addWidget(icon_lbl)

        text_lbl = QLabel(text, self)
        text_lbl.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; {get_font_family_css()} {font_size_css(12)};"
        )
        text_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        row_layout.addWidget(text_lbl)
        row_layout.addStretch(1)

        if right is not None:
            row_layout.addWidget(right)

        Colors.refresh()
        self.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 8px;
                text-align: left;
                padding: 0;
            }}
            QPushButton:hover {{ background: {Colors.HOVER_BG}; }}
            QPushButton:pressed {{ background: {Colors.SELECTED_BG}; }}
        """)


class _GiteeMorePopup(QWidget):
    """Gitee 更多操作浮动卡片 — WorkBuddy 风格悬浮菜单

    点击 GiteeAccountRow 的 ⚙ 按钮后弹出，分区结构：
    ① 账号区（头像 + 名称/仓库，点击跳仓库）+ 绑定/解绑
    ② 快捷开关：深色模式 / 窗口置顶
    ③ 快捷导航：外观 / 插件设置（直跳设置卡对应页）
    ③ 快捷导航：外观 / 插件设置（直跳设置卡对应页）
    ④ 帮助与反馈（新建会话发起 issue 提交引导）/ 检查更新
    ⑤ 设置（底部）
    """

    def __init__(self, account_row: "GiteeAccountRow", parent=None):
        # NoDropShadowWindowHint：禁掉 Windows 对 popup 的系统直角投影（会在透明边距外露出套层直角框）
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self._account_row = account_row
        self._cfg = account_row.cfg
        self._build_ui()

    def _build_ui(self):
        Colors.refresh()

        # ── 主容器：圆角卡片背景（阴影由 paintEvent 自绘；DropShadowEffect
        # 在 Qt.Popup + WA_TranslucentBackground 上会带出直角不透明底框，不可用）──
        self._container = QWidget(self)
        self._container.setObjectName("giteePopupContainer")
        self._container.setStyleSheet(f"""
            #giteePopupContainer {{
                background: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 12px;
            }}
        """)

        layout = QVBoxLayout(self._container)
        layout.setContentsMargins(scale_font_size(6), scale_font_size(6), scale_font_size(6), scale_font_size(6))
        layout.setSpacing(0)
        self._container.setFixedWidth(scale_font_size(252))

        # ── 账号信息区域（可点击跳转仓库） ──
        self._info_widget = QWidget(self._container)
        self._info_widget.setCursor(Qt.PointingHandCursor)
        info_layout = QHBoxLayout(self._info_widget)
        info_layout.setContentsMargins(scale_font_size(10), scale_font_size(8), scale_font_size(10), scale_font_size(8))
        info_layout.setSpacing(10)

        self._popup_avatar = _AvatarCircleWidget("?", self._info_widget)
        self._popup_avatar.set_size(scale_font_size(44))
        # 鼠标事件穿透，由 _info_widget 统一处理点击
        self._popup_avatar.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        info_layout.addWidget(self._popup_avatar)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)
        self._popup_name = QLabel("", self._info_widget)
        self._popup_name.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(13)}; font-weight: 600;"
        )
        self._popup_name.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._popup_repo = QLabel("", self._info_widget)
        self._popup_repo.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(10)};"
        )
        self._popup_repo.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        text_col.addWidget(self._popup_name)
        text_col.addWidget(self._popup_repo)
        info_layout.addLayout(text_col, 1)
        # 整行点击打开仓库
        self._info_widget.mousePressEvent = lambda ev: self._on_info_clicked(ev)
        layout.addWidget(self._info_widget)

        # ── 绑定/解绑按钮 ──
        self._popup_action_btn = QPushButton("", self._container)
        self._popup_action_btn.setCursor(Qt.PointingHandCursor)
        self._popup_action_btn.setFixedHeight(28)
        self._popup_action_btn.clicked.connect(self._on_action_clicked)
        layout.addWidget(self._popup_action_btn, 0, Qt.AlignCenter)

        layout.addSpacing(scale_font_size(6))

        # ── 分隔线 ──
        layout.addWidget(self._make_separator())

        # ── 快捷开关：深色模式 / 窗口置顶 ──
        dark_switch = SwitchButton(self._container)
        dark_switch.setOnText("")
        dark_switch.setOffText("")
        dark_switch.label.setVisible(False)  # 只留开关本体；_updateText 会重写文字，仅 setText 压不住
        dark_switch.setChecked(not self._cfg.ui_light_mode.value)
        dark_switch.checkedChanged.connect(self._on_dark_mode_toggled)
        self._dark_mode_row = _MenuRow(FluentIcon.BRIGHTNESS, "深色模式", right=dark_switch, parent=self._container)
        layout.addWidget(self._dark_mode_row)

        topmost_switch = SwitchButton(self._container)
        topmost_switch.setOnText("")
        topmost_switch.setOffText("")
        topmost_switch.label.setVisible(False)
        topmost_switch.setChecked(bool(self._cfg.window_always_on_top.value))
        topmost_switch.checkedChanged.connect(self._on_topmost_toggled)
        self._topmost_row = _MenuRow(FluentIcon.PIN, "窗口置顶", right=topmost_switch, parent=self._container)
        layout.addWidget(self._topmost_row)
        # 保持 Python 引用，防止 SwitchButton 被提前 GC 回收
        self._switch_refs = [dark_switch, topmost_switch]

        layout.addWidget(self._make_separator())

        # ── 快捷导航：直跳设置卡对应页 ──
        self._appearance_row = _MenuRow(
            "主题风格", "外观", right=self._make_chevron(), parent=self._container
        )
        self._appearance_row.clicked.connect(lambda: self._open_settings_tab("appearance"))
        layout.addWidget(self._appearance_row)

        self._plugins_row = _MenuRow(
            FluentIcon.APPLICATION, "插件设置", right=self._make_chevron(), parent=self._container
        )
        self._plugins_row.clicked.connect(lambda: self._open_settings_tab("plugins"))
        layout.addWidget(self._plugins_row)

        # ── 帮助与反馈 ──
        self._feedback_row = _MenuRow(
            FluentIcon.FEEDBACK, "帮助与反馈", right=self._make_chevron(), parent=self._container
        )
        self._feedback_row.clicked.connect(self._on_help_feedback)
        layout.addWidget(self._feedback_row)

        # ── 检查更新（右侧显示当前版本号，与托盘菜单同款）──
        version_lbl = QLabel(str(self._cfg.current_version or ""), self._container)
        version_lbl.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(11)};"
        )
        version_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._update_row = _MenuRow(FluentIcon.UPDATE, "检查更新", right=version_lbl, parent=self._container)
        self._update_row.clicked.connect(self._on_check_update)
        layout.addWidget(self._update_row)

        layout.addWidget(self._make_separator())

        # ── 设置（最底部） ──
        self._open_settings_btn = _MenuRow(
            "配置管理", "设置", right=self._make_chevron(), parent=self._container
        )
        self._open_settings_btn.clicked.connect(self._on_open_settings)
        layout.addWidget(self._open_settings_btn)

        # 阴影透明边距：投影画在容器外（popup 自身为透明层）；收紧边距让卡片贴近窗口边角
        main_layout = QVBoxLayout(self)
        pad = scale_font_size(5)
        pad_bottom = scale_font_size(7)
        main_layout.setContentsMargins(pad, pad, pad, pad_bottom)
        main_layout.addWidget(self._container)
        # 可见卡片（container）相对 popup 外框的透明边距 (左, 上, 右, 下)：
        # 定位与阴影绘制共用同一来源，避免「按外框高度定位」把底部阴影留白算进视觉高度
        self._content_margins = (pad, pad, pad, pad_bottom)

        # 刷新账号状态
        self._refresh_account_state()

    def _make_separator(self) -> QWidget:
        """带左右留白的分隔线（全宽直顶圆角边不够精致）"""
        wrap = QWidget(self._container)
        wrap.setStyleSheet("background: transparent;")
        wrap_layout = QVBoxLayout(wrap)
        wrap_layout.setContentsMargins(scale_font_size(10), 0, scale_font_size(10), 0)
        sep = QFrame(wrap)
        sep.setFrameShape(QFrame.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {Colors.DIVIDER_COLOR}; border: none;")
        wrap_layout.addWidget(sep)
        return wrap

    def _make_chevron(self) -> QLabel:
        """导航行右侧箭头"""
        lbl = QLabel("›", self._container)
        lbl.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(14)};"
        )
        lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        return lbl

    def _refresh_account_state(self):
        """根据当前绑定状态刷新账号信息区域"""
        is_bound = bool(self._cfg.gitee_bound.value)
        owner = str(self._cfg.gitee_user_owner.value or "")
        repo = str(self._cfg.gitee_user_repo.value or "")
        avatar_size = scale_font_size(44)

        if self._account_row is not None and getattr(self._account_row, "_binding", False):
            # 绑定等待中：主操作变「中止授权」，用户可随时退出而不必等超时
            self._popup_action_btn.setText("中止授权")
            self._popup_action_btn.setStyleSheet(f"""
                QPushButton {{
                    color: {Colors.ERROR};
                    background: transparent;
                    border: 1px solid {Colors.ERROR};
                    border-radius: 6px;
                    padding: 4px 20px;
                    {get_font_family_css()} {font_size_css(12)};
                }}
                QPushButton:hover {{
                    background: rgba(250, 81, 81, 0.10);
                }}
            """)
            return

        if is_bound and owner:
            self._popup_avatar.set_avatar(owner)
            self._popup_avatar.set_size(avatar_size)
            # 真实头像同步：popup 点击时才创建，多数情况下行头像已加载完
            # （缓存命中即时有图 / 下载晚到由 account_row._on_avatar_fetched 补推）
            row_avatar = self._account_row._avatar.image() if self._account_row else None
            self._popup_avatar.set_image(row_avatar if (row_avatar and not row_avatar.isNull()) else None)
            hint = f"点击打开 {owner}/{repo}"
            self._info_widget.setToolTip(hint)
            self._popup_name.setText(owner)
            self._popup_repo.setText(f"{repo} ↗")
            self._popup_action_btn.setText("解绑账号")
            self._popup_action_btn.setStyleSheet(f"""
                QPushButton {{
                    color: {Colors.ERROR};
                    background: transparent;
                    border: 1px solid {Colors.ERROR};
                    border-radius: 6px;
                    padding: 4px 20px;
                    {get_font_family_css()} {font_size_css(12)};
                }}
                QPushButton:hover {{
                    background: rgba(250, 81, 81, 0.10);
                }}
            """)
        else:
            self._popup_avatar.set_image(None)
            self._popup_avatar.set_avatar("?")
            self._popup_avatar.set_size(avatar_size)
            self._popup_avatar.setToolTip("未绑定")
            self._popup_name.setText("未绑定 Gitee")
            self._popup_repo.setText("绑定后可备份与分享")
            self._popup_action_btn.setText("绑定账号")
            self._popup_action_btn.setStyleSheet(f"""
                QPushButton {{
                    color: #ffffff;
                    background: {Colors.INFO};
                    border: none;
                    border-radius: 6px;
                    padding: 4px 20px;
                    {get_font_family_css()} {font_size_css(12)};
                }}
                QPushButton:hover {{
                    background: {Colors.INFO};
                }}
            """)

    def _on_info_clicked(self, event):
        """点击账号信息区域 → 打开仓库"""
        if event.button() == Qt.LeftButton and self._account_row:
            self._account_row._open_repository()

    def _on_action_clicked(self):
        """点击浮动卡片中的绑定/中止/解绑按钮"""
        if self._account_row is not None and self._account_row._binding:
            self._account_row._abort_binding()
            self.close()
            return
        if self._cfg.gitee_bound.value:
            self._account_row._on_unbind()
        else:
            self._account_row._on_bind()
        # 延迟关闭：_on_unbind/_on_bind 中的模态对话框可能已导致
        # Qt 自动关闭 Qt.Popup 并触发 WA_DeleteOnClose 销毁 C++ 对象，
        # 同步调用 self.close() 会触发 RuntimeError，故延迟到下一帧关闭。
        QTimer.singleShot(0, self.close)

    def _on_open_settings(self):
        """打开设置卡片（默认页）"""
        self.close()
        if self._account_row:
            self._account_row._toggle_settings_card()

    def _open_settings_tab(self, tab_id: str):
        """直跳设置卡并切换到指定导航页（appearance/plugins 等）"""
        self.close()
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc.open_settings(tab_id)
            return
        # 兜底：控制器不可用时走原 toggle 链（无指定页）
        if self._account_row:
            self._account_row._toggle_settings_card()

    def _on_help_feedback(self):
        """帮助与反馈：新建会话并自动发起 issue 提交引导（issue-reporter 技能）"""
        self.close()
        from app.widgets.tab_manager_window import TabManagerWindow

        tm = TabManagerWindow.get_instance()
        if tm is None:
            return
        source = tm.get_current_window()
        if source is None and self._account_row is not None:
            source = self._account_row.window()
        if source is None:
            return
        new_win = tm.spawn_tab(source, new_session=True)
        if new_win is None:
            return
        # 等新 tab 就绪再预填输入框（不自动发送，用户改完自己发）；经类名调用避免捕获已销毁的 self
        QTimer.singleShot(300, lambda w=new_win: _GiteeMorePopup._send_feedback_prompt(w))

    def _on_check_update(self):
        """检查更新：手动触发，结果 InfoBar 落地在 TabManagerWindow（与托盘同款）"""
        self.close()
        from app.widgets.tab_manager_window import TabManagerWindow

        tm = TabManagerWindow.get_instance()
        if tm is None:
            return
        try:
            from app.update_checker import UpdateChecker

            UpdateChecker.get_instance(tm).check_update()
        except Exception as e:
            logger.warning(f"[gitee-popup] 检查更新失败: {e}")

    @staticmethod
    def _send_feedback_prompt(win):
        """把 issue 提交引导 prompt 预填到新窗口输入框，由用户修改后自行发送"""
        try:
            if getattr(win, "_is_destroyed", False):
                return
            prompt = (
                "我想给 DriFox 提交一个 issue（反馈 bug / 建议）。"
                "请使用 issue-reporter 技能：先自动收集环境信息（版本、系统、最近错误日志），"
                "再引导我补充问题描述，与我确认后提交到 GitHub 仓库，并把 issue 链接贴给我。"
            )
            win.input_area.setPlainText(prompt)
            win.input_area.setFocus()
        except Exception:
            pass

    # ── 快捷开关回调 ──

    def _on_dark_mode_toggled(self, checked: bool):
        """深色模式切换

        完整刷新链（ui_light_mode.valueChanged → LLMSettingsCard._on_light_mode_changed
        → ui_theme_style → configChanged → 全量刷新）依赖懒构建设置卡；本次启动
        未打开过设置页时无人监听 valueChanged，界面不会切换。此处兜底：设置卡
        未构建时直写目标主题并 dispatch_refresh 显式全量刷新（与 config_sync 同款）。
        """
        is_light = not checked
        self._cfg.ui_light_mode.value = is_light
        self._cfg.save()

        from app.widgets.cards.global_card_controller import get_global_card_controller

        controller = get_global_card_controller()
        if controller is not None and controller._settings_popup is not None:
            # 设置卡已构建 → valueChanged 链自动完成主题切换，不重复触发
            return

        from app.utils.config import update_theme_options
        from app.utils.theme_manager import theme_manager

        # 写 ui_theme_style 前先刷新 validator 选项集，防止目标主题未注册时
        # 被 OptionsValidator.correct() 静默回退到 options[0]
        update_theme_options()
        target_theme = "lumia" if is_light else "fallout"
        if self._cfg.ui_theme_style.value != target_theme:
            self._cfg.set(self._cfg.ui_theme_style, target_theme, save=True)
        theme_manager.dispatch_refresh()

    def _on_topmost_toggled(self, checked: bool):
        """窗口置顶开关切换

        统一走 _apply_window_topmost：macOS 走软置顶（不加
        WindowStaysOnTopHint，避免最小化按钮被系统丢弃），其他平台
        加/摘 WS_EX_TOPMOST hint。

        注意：self.window() 返回的是 popup 自身（Qt.Popup 自带 Window 标志），
        必须通过 _account_row 的父链才能获取真正的应用顶层窗口（TabManagerWindow / OpenAIChatToolWindow）
        """
        self._cfg.window_always_on_top.value = checked
        self._cfg.save()

        window = self._account_row.window() if self._account_row else None
        if not window:
            return

        from app.widgets.tab_manager_window import _apply_window_topmost

        _apply_window_topmost(window)

    # ── 自绘柔和阴影（画在卡片下层的透明边距内）──

    def content_margins(self) -> tuple[int, int, int, int]:
        """可见卡片（container）相对 popup 外框的透明边距 (左, 上, 右, 下)"""
        return self._content_margins

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        Colors.refresh()
        left, top, right, bottom = self._content_margins
        radius = scale_font_size(12)
        # 阴影矩形与 container 几何严格重合（边距取自主布局），外环 grow 后在透明边距内露出。
        # 此前底部写成 margin + scale_font_size(2)：字号 delta 被算两次
        # （底线距底 = 7 + 2*delta > container 的 7 + delta），card 反被 container 整块遮住，
        # 阴影实际不可见。
        card = self.rect().adjusted(left, top, -right, -bottom)
        painter.setPen(Qt.NoPen)
        for grow, alpha in ((4, 8), (3, 12), (2, 16), (1, 22)):
            painter.setBrush(QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(card.adjusted(-grow, -grow, grow, grow), radius + grow, radius + grow)

    def closeEvent(self, event):
        if self._account_row:
            self._account_row._on_popup_closed()
        super().closeEvent(event)


# ── 卡片 ──────────────────────────────────────────────────


class GiteeCard(SettingCard):
    """Gitee 账号绑定 — SettingCard 子类，布局与其他设置卡片一致"""

    oauthResult = pyqtSignal(bool, str)
    # 真实头像下载完成（后台线程发、主线程槽收→QPixmap 只能在主线程构造）
    _avatarFetched = pyqtSignal(bytes)

    def __init__(self, parent=None):
        super().__init__(
            get_icon("gitee"),
            "Gitee 账号绑定",
            "云端备份配置、分享会话",
            parent,
        )
        self.cfg = Settings.get_instance()
        self._binding = False
        # 绑定中止信号：用户点「中止」时置位，授权流程 0.5s 内退出等待
        self._bind_cancel: threading.Event | None = None
        self._bound_owner = ""
        self._bound_repo = ""
        self._avatar_fetching = False  # 头像下载防重入（主线程读写，无竞争）
        self._avatar_owner_loaded = ""  # 已成功拉到真实头像的 owner（进程内去重）
        self._avatarFetched.connect(self._on_avatar_fetched)
        self.oauthResult.connect(self._on_oauth_result)

        self._setup_right()
        self._setup_sync_indicator()
        self._connect_config_signals()
        self._refresh_ui()

    def _connect_config_signals(self):
        """监听绑定配置变化（外部清绑/自动解绑/其他入口绑定）→ 刷新卡片按钮

        与 GiteeAccountRow 保持一致：绑定状态可能由 ConfigSync 自动清绑、
        GiteeAccountRow 解绑、GiteeOAuthBackend.unbind 等入口修改，
        卡片需同步响应，否则按钮停留在「解绑」。
        """
        for item in (
            self.cfg.gitee_bound,
            self.cfg.gitee_user_owner,
            self.cfg.gitee_user_repo,
        ):
            try:
                item.valueChanged.disconnect(self._on_config_changed)
            except TypeError:
                pass
            item.valueChanged.connect(self._on_config_changed)

    def _on_config_changed(self, _value=None):
        """配置变化（gitee 绑定态/owner/repo）→ 刷新卡片 UI"""
        self._refresh_ui()

    def _setup_right(self):
        avatar_size = scale_font_size(28)
        self._avatar = _AvatarCircleWidget("?", self)
        self._avatar.set_size(avatar_size)
        self._avatar.clicked.connect(self._on_avatar_clicked)
        self.hBoxLayout.addWidget(self._avatar)

        self.hBoxLayout.addSpacing(6)

        self._bind_btn = QPushButton("绑定")
        self._bind_btn.setFixedWidth(76)
        self._bind_btn.setMinimumHeight(30)
        self._bind_btn.setCursor(Qt.PointingHandCursor)
        self._bind_btn.setStyleSheet(
            f"QPushButton {{"
            f"background-color: #0078d4; color: #ffffff; border: none;"
            f"border-radius: 5px; padding: 5px 16px; {font_size_css(13)}"
            f"font-weight: bold;"
            f"}}"
            f"QPushButton:hover {{ background-color: {Colors.BORDER_ACCENT}; }}"
            f"QPushButton:pressed {{ background-color: {Colors.SELECTED_BG}; }}"
            f"QPushButton:disabled {{ background-color: #444; color: #888; }}"
        )
        self._bind_btn.clicked.connect(self._on_bind_clicked)
        self.hBoxLayout.addWidget(self._bind_btn)

        self.hBoxLayout.addSpacing(4)

    def _setup_sync_indicator(self):
        """同步状态指示圆点（显示在按钮右侧）"""
        dot_size = scale_font_size(7)
        self._sync_dot = QLabel()
        self._sync_dot.setFixedSize(dot_size, dot_size)
        self._sync_dot.hide()
        self.hBoxLayout.addWidget(self._sync_dot)
        self.hBoxLayout.addSpacing(2)

        from app.core.sync.config_sync import ConfigSyncService

        self._sync_svc = ConfigSyncService.get_instance()
        # 先断开旧连接，防止 GiteeCard 重建（如设置面板关闭再打开）导致重复连接
        try:
            self._sync_svc.stateChanged.disconnect(self._on_sync_state_changed)
        except TypeError:
            pass
        try:
            self._sync_svc.syncDone.disconnect(self._on_initial_sync_done)
        except TypeError:
            pass
        self._sync_svc.stateChanged.connect(self._on_sync_state_changed)
        self._sync_svc.syncDone.connect(self._on_initial_sync_done)

    # ── UI 刷新 ──────────────────────────────────────────

    def _refresh_ui(self):
        is_bound = self.cfg.gitee_bound.value
        owner = self.cfg.gitee_user_owner.value
        repo = self.cfg.gitee_user_repo.value
        avatar_size = scale_font_size(28)

        if is_bound and owner:
            self._bound_owner = owner
            self._bound_repo = repo
            self._avatar.set_avatar(owner)
            self._avatar.set_size(avatar_size)
            self._avatar.setToolTip(f"点击打开仓库 {owner}/{repo}")
            self._load_avatar_async(owner)
            self._bind_btn.setText("解绑")
            self._bind_btn.setCursor(Qt.PointingHandCursor)
            self._bind_btn.setStyleSheet(
                f"QPushButton {{"
                f"color: #fa5151; border: 1px solid #fa5151; border-radius: 6px;"
                f"padding: 5px 12px; font-size: {scale_font_size(12)}px;"
                f"background: transparent;"
                f"}}"
                f"QPushButton:hover {{ background-color: rgba(250, 81, 81, 0.1); }}"
            )
            # 已绑定 → 自动启动同步（如尚未启动）
            self._auto_enable_sync()
        else:
            self._bound_owner = ""
            self._bound_repo = ""
            self._avatar.set_image(None)
            self._avatar.set_avatar("?")
            self._avatar.set_size(avatar_size)
            self._avatar.setToolTip("未绑定")
            self._bind_btn.setText("绑定")
            self._bind_btn.setCursor(Qt.PointingHandCursor)
            self._bind_btn.setStyleSheet(
                f"QPushButton {{"
                f"background-color: #0078d4; color: #ffffff; border: none;"
                f"border-radius: 5px; padding: 5px 16px; {font_size_css(13)}"
                f"font-weight: bold;"
                f"}}"
                f"QPushButton:hover {{ background-color: {Colors.BORDER_ACCENT}; }}"
                f"QPushButton:pressed {{ background-color: {Colors.SELECTED_BG}; }}"
                f"QPushButton:disabled {{ background-color: #444; color: #888; }}"
            )
            self._sync_dot.hide()

    # ── 真实头像拉取（与 GiteeAccountRow 共用模块级 helpers） ──

    def _load_avatar_async(self, owner: str):
        """缓存命中先即时显示，再后台拉新（不阻塞 UI，与账户行语义一致）"""
        if self._avatar_fetching or (owner == self._avatar_owner_loaded and self._avatar.has_image()):
            return
        cache = _avatar_cache_path(owner)
        if cache.exists():
            pm = QPixmap(str(cache))
            if not pm.isNull():
                self._avatar.set_image(pm)
                self._avatar_owner_loaded = owner
        self._avatar_fetching = True
        threading.Thread(target=self._fetch_avatar_worker, daemon=True).start()

    def _fetch_avatar_worker(self):
        """后台线程：拉取图片字节 → 信号回主线程（QPixmap 只能在主线程构造）"""
        try:
            data = _fetch_avatar_bytes()
            if data:
                self._avatarFetched.emit(data)
        finally:
            self._avatar_fetching = False

    def _on_avatar_fetched(self, data: bytes):
        """主线程槽：字节 → QPixmap → 头像渲染 + 写磁盘缓存"""
        pm = QPixmap()
        if not pm.loadFromData(data):
            return
        if not self._bound_owner:
            return
        self._avatar.set_image(pm)
        self._avatar_owner_loaded = self._bound_owner
        try:
            cache = _avatar_cache_path(self._bound_owner)
            cache.parent.mkdir(parents=True, exist_ok=True)
            pm.save(str(cache), "PNG")
        except Exception as e:
            logger.warning(f"[GiteeCard] 头像缓存写盘失败: {e}")

    def _auto_enable_sync(self):
        """如果已绑定且同步未启动，自动 enable（重启恢复场景）"""
        if self._sync_svc._state != "disabled":
            return
        # 通过 OAuth 后端获取有效 token（支持自动刷新）
        from app.gateway.auth import get_oauth_backend

        bound_info = get_oauth_backend("gitee").get_bound_info()
        if bound_info and bound_info.get("token") and bound_info.get("owner"):
            logger.info("[GiteeCard] 检测到已绑定，自动启动配置同步")
            self._sync_svc.enable(bound_info["token"], bound_info["owner"])
        elif bool(self.cfg.gitee_bound.value):
            # ★ T8B 断点① 修复：get_bound_info 返回 None 但 cfg 仍标记已绑定
            # （refresh_token 被吊销，_ensure_valid_token 返回 None）→ 之前静默
            # 跳过导致 ConfigSync 永不启动、syncDone 永不发。此处主动执行失效处理。
            logger.warning("[GiteeCard] 绑定已失效（token 无法刷新），清除绑定并提示重新绑定")
            self._handle_binding_invalid()

    def _handle_binding_invalid(self):
        """绑定失效处理：清绑定 + 账户行失效红标 + 全局失效提醒"""
        # 1) 清除本地绑定（等效 ConfigSync 清绑逻辑）
        try:
            self.cfg.gitee_bound.value = False
            self.cfg.gitee_user_token.value = ""
            self.cfg.gitee_user_refresh_token.value = ""
            self.cfg.gitee_token_expires_at.value = 0.0
            self.cfg.save()
        except Exception as e:
            logger.warning(f"[GiteeCard] 清除绑定失败: {e}")

        # 2) 通知各窗口的 GiteeAccountRow 置失效红标
        try:
            from app.widgets.tab_manager_window import TabManagerWindow

            tm = TabManagerWindow.get_instance()
            if tm is not None:
                for win in list(getattr(tm, "_windows", []) or []):
                    panel = getattr(win, "_tab_panel", None)
                    if panel is None:
                        continue
                    row = getattr(panel, "_gitee_account_row", None)
                    if row is not None:
                        row.set_invalid(True)
        except Exception as e:
            logger.warning(f"[GiteeCard] 通知账户行失效失败: {e}")

        # 3) 全局失效提醒（去重/开关守卫在 controller 内）
        try:
            from app.widgets.cards.global_card_controller import get_global_card_controller

            ctrl = get_global_card_controller()
            if ctrl is not None:
                ctrl.check_gitee_token_invalid_reminder()
        except Exception as e:
            logger.warning(f"[GiteeCard] 触发失效提醒失败: {e}")

    # ── 同步状态指示 ─────────────────────────────────────

    def _on_sync_state_changed(self, state: str):
        """ConfigSyncService 状态变更 → 更新圆点颜色"""
        dot_size = scale_font_size(7)
        if state == "disabled":
            self._sync_dot.hide()
        elif state == "idle":
            self._sync_dot.setStyleSheet(f"background: #3fb950; border-radius: {dot_size // 2}px;")
            self._sync_dot.setToolTip("同步正常")
            self._sync_dot.show()
        elif state == "syncing":
            self._sync_dot.setStyleSheet(f"background: #58a6ff; border-radius: {dot_size // 2}px;")
            self._sync_dot.setToolTip("正在同步…")
            self._sync_dot.show()
        elif state == "error":
            self._sync_dot.setStyleSheet(f"background: #f85149; border-radius: {dot_size // 2}px;")
            self._sync_dot.setToolTip("同步失败，点击重试")
            self._sync_dot.setCursor(Qt.PointingHandCursor)
            self._sync_dot.mousePressEvent = self._on_sync_retry
            self._sync_dot.show()

    def _on_sync_retry(self, event):
        """点击红点重试"""
        import threading

        t = threading.Thread(target=self._sync_svc.upload, daemon=True)
        t.start()

    def _on_initial_sync_done(self, success: bool, message: str):
        """首次同步完成回调（仅绑定后首次检查远端时触发）"""
        if not success:
            # 失败时由状态机显示红点，不弹 InfoBar
            return

        # 远端配置已下载并覆盖本地。
        # 不再调用 _refresh_app_ui() — Settings.load() 已在主线程由
        # _reload_settings_on_main_thread 执行，所有 ConfigItem 的
        # valueChanged 信号已同步分发给 UI 槽，UI 已自然更新。
        # 移除额外的全量刷新避免了 findChildren 遍历全窗口树的 6s+ 卡顿。
        # 静默同步成功，不弹 InfoBar（避免启动时打扰）

    def _refresh_app_ui(self):
        """配置恢复后刷新整个 UI：通过标准配置变更链路逐窗口刷新"""
        try:
            # QTimer 回调可能在窗口已销毁后触发（极端情况：100ms 内关闭窗口）
            main_win = self.window()
            if main_win and getattr(main_win, "_is_destroyed", False):
                return


            # 1. 通过标准配置变更路径逐窗口刷新（与用户手动更改设置走同一路径）
            #    _apply_runtime_ui_settings 内部：
            #      - Colors.refresh() → 重算颜色 token
            #      - 按 scope 精准刷新 widget 树（message card / settings card / 圆环等）
            #      - 不触发 dispatch_refresh() 的全量 refresh_theme() 调用
            for win in window_registry.alive_window_instances():
                if getattr(win, "_is_destroyed", False):
                    continue
                try:
                    win._apply_runtime_ui_settings(scope=None)
                except Exception as e:
                    logger.warning(f"[GiteeCard] 窗口 {win._window_id} 刷新失败: {e}")

            # 2. 销毁全局设置弹窗：下次打开时重建，
            #    所有子卡片（provider/MCP/gateway/font 等）从 Settings 读取最新值
            from app.widgets.cards.global_card_controller import get_global_card_controller

            cc = get_global_card_controller()
            if cc is not None:
                cc.invalidate_settings_popup()

            logger.info("[GiteeCard] UI 已根据恢复的配置全面刷新")
        except Exception as e:
            logger.warning(f"[GiteeCard] UI 刷新失败: {e}")

    def _on_avatar_clicked(self):
        if self._bound_owner:
            webbrowser.open(f"https://gitee.com/{self._bound_owner}/{self._bound_repo}")

    def _on_bind_clicked(self):
        if self._binding:
            self._abort_binding()
            return
        if self.cfg.gitee_bound.value:
            self._on_unbind()
        else:
            self._on_bind()

    # ── 绑定 ─────────────────────────────────────────────

    def _on_bind(self):
        if self._binding:
            return

        dialog = _RepoVisibilityDialog(self.window())
        dialog.chosen.connect(self._start_oauth_with_backup)
        dialog.exec_()

    def _start_oauth_with_backup(self, repo_private: bool):
        """绑定前先备份本地配置"""
        from app.core.sync.config_sync import ConfigSyncService

        ConfigSyncService.get_instance().backup_local()
        self._start_oauth(repo_private)

    def _start_oauth(self, repo_private: bool):
        self._binding = True
        self._bind_cancel = threading.Event()
        # 按钮变「中止」且保持可点：关掉授权页后不必干等 120s 超时
        self._bind_btn.setText("中止")
        self._bind_btn.setEnabled(True)
        self._bind_btn.setToolTip("正在等待浏览器授权，点击中止")

        t = threading.Thread(
            target=self._do_oauth, args=(repo_private, self._bind_cancel), daemon=True
        )
        t.start()

    def _abort_binding(self):
        """中止进行中的绑定：置取消信号，后台线程 0.5s 内退出等待"""
        if self._bind_cancel is not None:
            self._bind_cancel.set()
            self._bind_cancel = None
        self._bind_btn.setEnabled(False)
        self._bind_btn.setToolTip("正在中止授权...")

    def _do_oauth(self, repo_private: bool, cancel_event=None):
        try:
            from app.gateway.auth import get_oauth_backend

            success, msg = get_oauth_backend("gitee").bind(
                repo_private=repo_private, cancel_event=cancel_event
            )
            self.oauthResult.emit(success, msg)
        except Exception as e:
            logger.error(f"[GiteeCard] OAuth 异常: {e}")
            self.oauthResult.emit(False, f"绑定异常：{e}")

    def _on_oauth_result(self, success: bool, msg: str):
        self._binding = False
        self._bind_cancel = None
        self._bind_btn.setEnabled(True)
        self._bind_btn.setToolTip("")

        if success:
            from app.gateway.utils.gitee_uploader import GiteeUploader

            GiteeUploader.get_instance().reset_config()
            self._refresh_ui()
            # _refresh_ui() → _auto_enable_sync() 已调 enable()，无需重复调用

            InfoBar.success(
                title="绑定成功",
                content=msg,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
        else:
            self._bind_btn.setText("绑定")
            if msg == CANCEL_MESSAGE:
                # 主动中止不是失败：安静提示，不用错误色
                InfoBar.info(
                    title="已中止授权",
                    content="绑定已中止，可随时重新发起。",
                    position=InfoBarPosition.TOP_RIGHT,
                    duration=3000,
                    parent=self.window(),
                )
                return
            InfoBar.error(
                title="绑定失败",
                content=msg,
                position=InfoBarPosition.TOP_RIGHT,
                duration=5000,
                parent=self.window(),
            )

    # ── 解绑 ─────────────────────────────────────────────

    def _on_unbind(self):
        owner = self.cfg.gitee_user_owner.value
        dialog = ConfirmDialog(
            title="确认解绑",
            content=f"解绑后上传将恢复使用共享图床仓库。\n当前绑定：{owner}",
            confirm_text="确定解绑",
            cancel_text="取消",
            parent=self.window(),
        )
        dialog.confirmed.connect(self._do_unbind)
        dialog.exec_()

    def _do_unbind(self):
        try:
            from app.gateway.auth import get_oauth_backend
            from app.gateway.utils.gitee_uploader import GiteeUploader

            # 先停止同步，再解绑
            self._sync_svc.disable()
            get_oauth_backend("gitee").unbind()
            GiteeUploader.get_instance().reset_config()

            # 恢复绑定前的本地配置
            if self._sync_svc.restore_local():
                try:
                    self.cfg.load()
                except Exception:
                    pass

            self._refresh_ui()
            InfoBar.success(
                title="已解绑",
                content="Gitee 账号已解绑，配置已恢复",
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
        except Exception as e:
            logger.error(f"[GiteeCard] 解绑异常: {e}")
            InfoBar.error(
                title="解绑失败",
                content=str(e),
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )

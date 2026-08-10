# -*- coding: utf-8 -*-
"""SquircleAvatar — 椭方块形插件头像

仿照 app/widgets/cards/settings/project_selector_card.py 的 _SquareAvatar
实现：纯色圆角矩形 + 白色加粗缩写。用于插件列表行，每个插件得到唯一
的视觉标识（颜色由名称 CRC32 哈希而来，缩写由智能规则提取）。

尺寸自适应：接受 font_size 参数，按 font_size * 1.7 缩放图标边长
（最低 20px）。字号变化时调用 set_font_size() 实时更新。

闭包约束（来自 plugin-manager 注释）：
- 不导入 app.core 或 app.widgets
- 不导入 app.utils.utils（避免与 Settings 单例耦合）
- 仅依赖 PyQt5 + stdlib
"""

import colorsys
import re
import zlib
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from PyQt5.QtCore import Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PyQt5.QtSvg import QSvgWidget
from PyQt5.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import isDarkTheme


# ── 尺寸自适应常量 ──────────────────────────────────
# 图标边长 = max(20, int(font_size * 1.7))
# 12px 字体 → 20px 图标
# 14px 字体 → 23px 图标（默认）
# 16px 字体 → 27px 图标
# 18px 字体 → 30px 图标
_AVATAR_SIZE_RATIO = 1.7
_AVATAR_MIN_SIZE = 20


def _compute_avatar_size(font_size: int) -> int:
    """根据上下文字体大小计算头像边长（px）"""
    if font_size <= 0:
        return _AVATAR_MIN_SIZE
    return max(_AVATAR_MIN_SIZE, int(font_size * _AVATAR_SIZE_RATIO))


def extract_initials(name: str) -> str:
    """从插件名提取最多 2 个字符的缩写

    优先级：中文 > 分隔符（_/-/空格）> 驼峰/帕斯卡 > 前 2 字母大写。
    与 project_selector_card.extract_project_initials 行为一致。
    """
    if not name:
        return "??"

    # ── 中文：首个汉字 ──
    has_cjk = any("\u4e00" <= c <= "\u9fff" for c in name)
    if has_cjk:
        for c in name:
            if "\u4e00" <= c <= "\u9fff":
                return c
        return name[0]

    # ── 分隔符拆分（下划线/中划线/空格）──
    for delim in ("_", "-", " "):
        if delim in name:
            parts = [p for p in name.split(delim) if p]
            if len(parts) >= 2:
                return (parts[0][0] + parts[-1][0]).upper()
            name = parts[0]
            break

    # ── 驼峰/帕斯卡：拆分为词 ──
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1|\2", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1|\2", s)
    words = [w for w in s.split("|") if w]

    if len(words) >= 2:
        return (words[0][0] + words[-1][0]).upper()

    # ── 普通单词：前 2 字母大写 ──
    if len(name) >= 2:
        return name[:2].upper()
    return name.upper()


def name_color(name: str, alpha: int = 255) -> str:
    """根据插件名计算固定 RGBA 颜色（HSL 全空间哈希）

    与 project_selector_card.get_project_color 算法完全一致：
    - H ∈ [0°, 360°)  crc % 360
    - S ∈ [55%, 85%]  55 + ((crc >> 8) % 31)
    - L ∈ [50%, 65%]  50 + ((crc >> 16) % 16)

    用 zlib.crc32 而非内置 hash()，避免 PYTHONHASHSEED 随机化导致颜色漂移。
    """
    crc = zlib.crc32(name.encode("utf-8"))
    h = crc % 360
    s = 55 + ((crc >> 8) % 31)
    l = 50 + ((crc >> 16) % 16)

    r, g, b = colorsys.hls_to_rgb(h / 360.0, l / 100.0, s / 100.0)
    return f"rgba({int(round(r * 255))}, {int(round(g * 255))}, {int(round(b * 255))}, {alpha})"


class SquircleAvatar(QWidget):
    """椭方块形插件头像 — flat design squircle 风格

    纯色圆角矩形 + 白色 1-2 字符缩写。QPainter 精确绘制，
    避免 QSS 在小尺寸下 border + border-radius 抗锯齿走样。

    尺寸自适应：
        构造时传入 font_size（如 14），按 font_size * 1.7 缩放图标边长。
        字号变化时调用 set_font_size() 实时更新，无需重建 widget。
    """

    def __init__(
        self,
        text: str,
        color: str,
        parent=None,
        size: int = 0,
        font_size: int = 0,
    ):
        """初始化头像

        Args:
            text: 缩写文字
            color: RGBA 颜色字符串
            parent: 父控件
            size: 显式尺寸（font_size > 0 时被忽略）
            font_size: 上下文字体大小（px），> 0 时按 1.7 倍放大作为图标尺寸
        """
        super().__init__(parent)
        self._text = text if text else "?"
        self._color = self._parse_rgba(color)
        # size 优先级提升：显式 size > 0 时直接用（让 PluginIconWidget 等容器
        # 可以强制覆盖，避免与 SVG 图标尺寸不一致）
        if size > 0:
            self._size = size
        elif font_size > 0:
            self._size = _compute_avatar_size(font_size)
        else:
            self._size = _AVATAR_MIN_SIZE
        self._font_size = font_size if font_size > 0 else 0
        self.setFixedSize(self._size, self._size)

    @staticmethod
    def _parse_rgba(rgba_str: str) -> QColor:
        """解析 'rgba(r,g,b,a)' 为 QColor，失败回退灰色"""
        if rgba_str.startswith("#"):
            return QColor(rgba_str)
        try:
            m = re.match(
                r"rgba?\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)"
                r"(?:\s*,\s*(\d+))?\s*\)",
                rgba_str,
            )
            if m:
                r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
                a = int(m.group(4)) if m.group(4) else 255
                return QColor(r, g, b, a)
        except Exception:
            pass
        return QColor(128, 128, 128)

    def set_avatar(self, text: str, color: str):
        """更新缩写和颜色（用于状态变化场景）"""
        self._text = extract_initials(text) if text else "?"
        self._color = self._parse_rgba(color)
        self.update()

    def set_font_size(self, font_size: int):
        """根据上下文字体大小动态调整头像尺寸

        Args:
            font_size: 上下文字体大小（px），<= 0 时无操作
        """
        if font_size <= 0:
            return
        self._font_size = font_size
        new_size = _compute_avatar_size(font_size)
        if new_size != self._size:
            self._size = new_size
            self.setFixedSize(new_size, new_size)
            self.update()

    def set_size(self, size: int):
        """直接覆盖头像尺寸（用于与 SVG 图标尺寸对齐）

        注意：调用此方法后，set_font_size() 不再按 font_size 自动调整。
        若需恢复字号联动，请重新构造 SquircleAvatar。
        """
        if size <= 0 or size == self._size:
            return
        self._size = size
        self.setFixedSize(size, size)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        rect = self.rect()
        # 微妙圆角（约 5px，like VS Code squircle）
        corner_radius = 5

        # 纯色填充背景
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._color)
        painter.drawRoundedRect(rect, corner_radius, corner_radius)

        # 居中白字
        painter.setPen(Qt.white)
        font = painter.font()
        # 字号按 size 比例缩放（参考源算法：14/24 ≈ 0.58）
        font.setPixelSize(max(8, self._size * 14 // 24))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, self._text)


# ── PluginIconWidget ──────────────────────────────────


class PluginIconWidget(QWidget):
    """插件图标组件：SVG 图标 + SquircleAvatar fallback

    根据当前主题自动选择 light/dark SVG。
    无 SVG 时回退到缩写哈希头像（SquircleAvatar）。
    尺寸自适应：font_size * 1.7，最低 20px。
    """

    def __init__(
        self,
        plugin_dir: Path,
        manifest: dict,
        font_size: int = 0,
        parent=None,
    ):
        super().__init__(parent)
        self._plugin_dir = plugin_dir
        self._manifest = manifest
        self._font_size = font_size
        self._svg_widget: Optional["QSvgWidget"] = None
        self._avatar: Optional[SquircleAvatar] = None
        self._setup_ui()

    def _resolve_icon_path(self) -> Optional[Path]:
        """根据 manifest 和当前主题解析实际图标路径"""
        raw = self._manifest.get("icon")
        if not raw:
            default = self._plugin_dir / "icon.svg"
            return default if default.exists() else None
        theme = "dark" if isDarkTheme() else "light"
        if isinstance(raw, str):
            p = self._plugin_dir / raw
            return p if p.exists() else None
        if isinstance(raw, dict):
            path_str = raw.get(theme) or raw.get("light", "")
            if path_str:
                p = (self._plugin_dir / path_str).resolve()
                return p if p.exists() else None
        return None

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        icon_path = self._resolve_icon_path()
        if icon_path is not None:
            self._svg_widget = QSvgWidget(str(icon_path), self)
            self._svg_widget.setFixedSize(self._icon_size(), self._icon_size())
            layout.addWidget(self._svg_widget)
        else:
            plugin_name = self._manifest.get("name", "?")
            # 把 icon_size 传给 SquircleAvatar，确保兜底头像与 SVG 图标尺寸一致
            self._avatar = SquircleAvatar(
                extract_initials(plugin_name),
                name_color(plugin_name),
                self,
                size=self._icon_size_override,
                font_size=self._font_size,
            )
            layout.addWidget(self._avatar)

    def set_font_size(self, font_size: int):
        """更新字号并重建组件（主题切换时也调用此方法）"""
        self._font_size = font_size
        while self.layout().count():
            item = self.layout().takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._setup_ui()

    def reload_icon(self):
        """主题变化后刷新图标（深浅切换）"""
        self.set_font_size(self._font_size)


# ── 远程 icon 解析 ──────────────────────────────────


# 共享 QNetworkAccessManager（Qt 推荐单例，避免每实例一连接池）
_icon_nam: Optional[QNetworkAccessManager] = None


def _get_icon_nam() -> QNetworkAccessManager:
    """获取/创建共享的 QNetworkAccessManager（用于异步下载 plugin icon）"""
    global _icon_nam
    if _icon_nam is None:
        _icon_nam = QNetworkAccessManager()
        # 跟随 Qt 事件循环自动清理（不显式 delete）
    return _icon_nam


def _icon_cache_dir() -> Path:
    """插件 icon 本地缓存目录（.drifox/cache/plugin_icons/）"""
    from .installer import _drifox_dir

    return _drifox_dir() / "cache" / "plugin_icons"


def resolve_remote_icon_urls(meta: dict) -> Optional[dict[str, str]]:
    """从 marketplace 元数据构造 GitHub raw 图标 URL

    仅识别 ``source.type == "git-subdir"`` 且 ``source.url`` 指向 github.com
    的来源（覆盖 DriFox 官方及遵循同一规范的市场）。其他来源（如纯 url
    指向 CDN）暂不处理。

    注意：从 ``meta["source"]`` 读插件源（git-subdir），而非
    ``meta["_marketplace_source"]``（后者是市场源 = marketplace.json URL，
    不是插件仓库）。

    Args:
        meta: marketplace 插件元数据（含 ``icon`` 与 ``source``）

    Returns:
        ``{"light": "...", "dark": "..."}`` 或 None（无法识别）
    """
    src = meta.get("source") or {}
    if src.get("type") != "git-subdir":
        return None
    url = src.get("url", "")
    if "github.com" not in urlparse(url).netloc:
        return None
    ref = src.get("ref", "main")
    subpath = src.get("path", "")
    # https://github.com/owner/repo(.git) → owner/repo
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) < 2:
        return None
    owner_repo = "/".join(parts[:2]).removesuffix(".git")
    base = f"https://raw.githubusercontent.com/{owner_repo}/{ref}/{subpath}"

    icon_value = meta.get("icon")
    if not icon_value:
        return None
    if isinstance(icon_value, str):
        return {"light": f"{base}/{icon_value}", "dark": f"{base}/{icon_value}"}
    if isinstance(icon_value, dict):
        out: dict[str, str] = {}
        for theme in ("light", "dark"):
            p = icon_value.get(theme) or icon_value.get("light", "")
            if p:
                out[theme] = f"{base}/{p}"
        return out or None
    return None


# ── 远程 icon 缓存命中检测 ──────────────────────────────────


def _cache_hit(name: str, theme: str) -> Optional[Path]:
    """检查插件指定主题的 icon 缓存是否存在（存在即返回路径）"""
    cache = _icon_cache_dir() / f"{name}__{theme}.svg"
    return cache if cache.is_file() else None


# ── PluginIconWidget 扩展：远程 URL 支持 ──────────────────────────────────


class PluginIconWidget(QWidget):
    """插件图标组件：本地 SVG → 远程 SVG → SquircleAvatar fallback

    优先级：
    1. 本地 ``plugin_dir/icon.svg``（已安装插件走这条）
    2. 远程 URL + 本地缓存命中（未安装但 manifest 有 icon）
    3. 远程 URL 异步拉取（拉取完成后通过 signal 渲染）
    4. SquircleAvatar 缩写头像（兜底）

    尺寸自适应：font_size * 1.7，最低 20px。
    """

    # 下载完成 signal：theme, svg_bytes（bytes，外部可写盘/读字节流）
    iconDownloaded = pyqtSignal(str, bytes)
    # 下载失败 signal：theme, error_str
    iconFailed = pyqtSignal(str, str)

    def __init__(
        self,
        plugin_dir: Optional[Path] = None,
        manifest: Optional[dict] = None,
        font_size: int = 0,
        parent=None,
        remote_urls: Optional[dict] = None,
        icon_size: int = 0,
    ):
        super().__init__(parent)
        self._plugin_dir = plugin_dir
        self._manifest = manifest or {}
        self._font_size = font_size
        self._remote_urls = remote_urls or {}
        self._icon_size_override = icon_size  # 0 = 用 font_size 推导
        self._svg_widget: Optional["QSvgWidget"] = None
        self._avatar: Optional[SquircleAvatar] = None
        # 异步下载相关
        self._inflight: dict[str, QNetworkReply] = {}
        self._setup_ui()

    def _resolve_local_icon(self) -> Optional[Path]:
        """解析本地 icon 路径（plugin_dir 已存在时调用）"""
        if self._plugin_dir is None:
            return None
        raw = self._manifest.get("icon")
        if not raw:
            default = self._plugin_dir / "icon.svg"
            return default if default.exists() else None
        theme = "dark" if isDarkTheme() else "light"
        if isinstance(raw, str):
            p = self._plugin_dir / raw
            return p if p.exists() else None
        if isinstance(raw, dict):
            path_str = raw.get(theme) or raw.get("light", "")
            if path_str:
                p = (self._plugin_dir / path_str).resolve()
                return p if p.exists() else None
        return None

    def _resolve_cached_remote(self) -> Optional[Path]:
        """解析远程 icon 缓存命中（plugin_dir 不存在但 remote_urls 提供时）"""
        if not self._remote_urls or not self._manifest:
            return None
        theme = "dark" if isDarkTheme() else "light"
        name = self._manifest.get("name", "?")
        return _cache_hit(name, theme)

    def _icon_size(self) -> int:
        # icon_size_override > 0 时直接用（如卡片行内需要放大图标）
        if self._icon_size_override > 0:
            return self._icon_size_override
        size = max(20, int(self._font_size * 1.7)) if self._font_size > 0 else 24
        return size

    def _show_svg(self, svg_path: Path):
        """在容器中渲染 SVG（尽可能复用现有 widget 以减少 layout 重算）

        性能说明：原实现每次都 clear + 重建 widget，会触发父 layout 重算。
        现在优先复用现有 _svg_widget：仅当尺寸变化或首次创建时才重建。
        """
        size = self._icon_size()
        if self._svg_widget is None:
            self._clear_children()
            self._svg_widget = QSvgWidget(str(svg_path), self)
            self._svg_widget.setFixedSize(size, size)
            self.layout().addWidget(self._svg_widget)
            return
        # 复用现有 widget：仅在尺寸变化时调整
        if self._svg_widget.size().width() != size or self._svg_widget.size().height() != size:
            self._svg_widget.setFixedSize(size, size)
        self._svg_widget.load(str(svg_path))
        # 若之前显示的是 avatar，重新显示 SVG
        if self._avatar is not None:
            self._avatar.hide()
            self._svg_widget.show()

    def _show_avatar(self):
        """在容器中渲染缩写头像（兜底）

        把 icon_size 传给 SquircleAvatar，保证有/无真实 SVG 时占位符与图标尺寸一致。
        """
        self._clear_children()
        plugin_name = self._manifest.get("name", "?")
        self._avatar = SquircleAvatar(
            extract_initials(plugin_name),
            name_color(plugin_name),
            self,
            size=self._icon_size_override,
            font_size=self._font_size,
        )
        self.layout().addWidget(self._avatar)

    def _clear_children(self):
        while self.layout().count():
            item = self.layout().takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._svg_widget = None
        self._avatar = None

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 优先级 1：本地 SVG
        local = self._resolve_local_icon()
        if local is not None:
            self._show_svg(local)
            return

        # 优先级 2：远程缓存命中
        cached = self._resolve_cached_remote()
        if cached is not None:
            self._show_svg(cached)
            return

        # 优先级 3：远程但缓存未命中 → 先显示 avatar，启动异步下载
        self._show_avatar()
        self._kick_off_remote_download()

    def _kick_off_remote_download(self):
        """按当前主题发起一次远程下载（去重：同 theme 已有 in-flight 则跳过）"""
        if not self._remote_urls:
            return
        theme = "dark" if isDarkTheme() else "light"
        url = self._remote_urls.get(theme) or self._remote_urls.get("light", "")
        if not url:
            return
        if theme in self._inflight:
            return  # 已在下载中
        nam = _get_icon_nam()
        req = QNetworkRequest(QUrl(url))
        req.setRawHeader(b"User-Agent", b"DriFox-PluginMarketplace/1.0")
        reply = nam.get(req)
        self._inflight[theme] = reply
        # finished 是 Qt 内置多态信号；reply 销毁时自动断连
        reply.finished.connect(lambda r=reply, t=theme: self._on_download_finished(r, t))

    def _on_download_finished(self, reply: "QNetworkReply", theme: str):
        """下载完成回调（成功 → 缓存并刷新 UI；失败 → 静默兜底）

        widget 可能已被销毁（异步回调触发时），需用 try/except 兜底
        避免污染 Qt 事件循环。

        性能优化：不在视口内时延迟到下一轮事件循环合并更新（避免
        多个下载同时完成触发频繁的 layout 重算导致滚动卡顿）。
        """
        try:
            self._inflight.pop(theme, None)
            if reply.error() != QNetworkReply.NoError:
                err = reply.errorString()
                reply.deleteLater()
                self.iconFailed.emit(theme, err)
                return
            data = bytes(reply.readAll())
            reply.deleteLater()
            if not data:
                self.iconFailed.emit(theme, "empty response")
                return
            # 写缓存
            name = self._manifest.get("name", "?")
            cache = _icon_cache_dir() / f"{name}__{theme}.svg"
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_bytes(data)
            except OSError:
                pass
            # 若当前主题匹配 → 立即替换 UI；不匹配则丢弃（主题切换时会重新加载）
            current_theme = "dark" if isDarkTheme() else "light"
            if theme == current_theme:
                self._apply_loaded_svg(cache)
            self.iconDownloaded.emit(theme, data)
        except RuntimeError:
            # widget 已被 Qt 删除（C++ 对象已释放），静默忽略
            pass
        except Exception as e:
            # 其他未知错误，避免污染 Qt 事件循环（回调异常会向上冒泡到 Qt 主循环）
            import logging

            logging.getLogger(__name__).debug(f"[IconWidget] download callback error: {e}")

    def _apply_loaded_svg(self, cache_path: Path):
        """应用已下载的 SVG 到 UI

        性能策略：
        - widget 在视口内 → 立即更新（用户能看到）
        - widget 不可见或不在视口 → 延迟到下一轮事件循环批量合并
          （避免滚动过程中多个 widget 同时完成下载导致频繁 layout 重算）
        """
        if not self._should_update_inline():
            # 延后到下一轮事件循环批量应用（合并多次重绘）
            from PyQt5.QtCore import QTimer

            QTimer.singleShot(0, lambda p=cache_path: self._do_show_svg_safe(p))
            return
        self._do_show_svg_safe(cache_path)

    def _should_update_inline(self) -> bool:
        """判断是否可立即更新（widget 在视口内且可见）"""
        if not self.isVisible():
            return False
        # 检查父窗口是否正在滚动（通过 viewport 偏移粗略判断）
        # 简化：只检查自身可见性 + 父级链至少有一层可见
        parent = self.parent()
        while parent is not None:
            if not parent.isVisible():
                return False
            parent = parent.parent()
        return True

    def _do_show_svg_safe(self, cache_path: Path):
        """安全显示 SVG（已通过可见性检查或延迟到下轮）"""
        try:
            self._show_svg(cache_path)
        except RuntimeError:
            # widget 已被销毁
            pass

    def set_font_size(self, font_size: int):
        """更新字号并重建组件（主题切换时也调用此方法）

        若父级调用方传入了 icon_size 覆盖，应改用 set_icon_size()，否则
        这里会把覆盖值丢失。仅在父级未传覆盖值时使用本方法。
        """
        self._font_size = font_size
        self._setup_ui()

    def set_icon_size(self, icon_size: int):
        """直接覆盖图标尺寸（用于卡片行内放大等场景）"""
        self._icon_size_override = icon_size
        self._setup_ui()

    def reload_icon(self):
        """主题变化后刷新图标（深浅切换）"""
        self.set_font_size(self._font_size)

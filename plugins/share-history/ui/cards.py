# -*- coding: utf-8 -*-
"""ShareHistoryCard 浮动卡片 — 浏览分享/导出历史记录

功能：
- 浏览历史分享记录（会话分享 + 项目导出）
- 按时间倒序排列
- 打开本地文件 / 复制上传链接
- 搜索筛选 / 单条删除 / 清空全部

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
- 所有文件操作通过 stdlib 完成
"""

import json as json_mod
import os
import re
import subprocess
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PyQt5.QtCore import QEvent, QObject, QSize, QThread, Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from loguru import logger
from qfluentwidgets import (
    FluentIcon,
    FluentLabelBase,
    IconWidget,
    InfoBar,
    ScrollArea,
    StrongBodyLabel,
    ToolButton,
    TransparentToolButton,
    isDarkTheme,
)

from .db import clear_all_records, delete_record, get_records, update_record_file_path


def _share_dir() -> Path:
    """获取分享根目录（不依赖外部模块）"""
    import sys as _sys

    if not hasattr(_sys, "_MEIPASS") and not getattr(_sys, "frozen", False):
        base = Path(".drifox")
    elif _sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Drifox" / ".drifox"
    else:
        base = Path.home() / ".drifox"
    return base / "share"


# ════════════════════════════════════════════════════════════
# 主题色/字体辅助
# ════════════════════════════════════════════════════════════


def _text_color(secondary: bool = False) -> str:
    if isDarkTheme():
        return "rgba(255,255,255,0.55)" if secondary else "rgba(255,255,255,0.9)"
    return "rgba(0,0,0,0.45)" if secondary else "rgba(0,0,0,0.85)"


def _ctx_font(ctx: dict) -> tuple:
    return ctx.get("font_family", "Microsoft YaHei"), ctx.get("font_size", 14)


def _ctx_text_color(ctx: dict, secondary: bool = False) -> str:
    colors = ctx.get("colors", {})
    key = "text_secondary" if secondary else "text_primary"
    val = colors.get(key, "")
    return val if val else _text_color(secondary)


def _ctx_border_color(ctx: dict) -> str:
    return ctx.get("colors", {}).get("border", "rgba(128,128,128,0.15)")


def _ctx_color(ctx: dict, key: str, fallback: str) -> str:
    return ctx.get("colors", {}).get(key, fallback)


# ════════════════════════════════════════════════════════════
# 默认主题配置
# ════════════════════════════════════════════════════════════

_DEFAULT_THEME: Dict[str, Any] = {
    "tc": "rgba(255,255,255,0.9)",
    "tcs": "rgba(255,255,255,0.55)",
    "border_c": "rgba(128,128,128,0.15)",
    "card_bg_dim": "rgba(128,128,128,0.06)",
    "hover_bg": "rgba(128,128,128,0.10)",
    "badge_bg": "rgba(128,128,128,0.10)",
    "btn_bg": "rgba(128,128,128,0.08)",
    "btn_border": "rgba(128,128,128,0.15)",
    "btn_disabled": "rgba(128,128,128,0.4)",
    "ff": "Microsoft YaHei",
    "fs": 14,
}

# ════════════════════════════════════════════════════════════
# 类型常量
# ════════════════════════════════════════════════════════════

_TYPE_ICONS = {"session": "💬", "project": "📦"}
_TYPE_LABELS = {"session": "会话", "project": "项目"}


# ════════════════════════════════════════════════════════════
# 异步 Worker
# ════════════════════════════════════════════════════════════


class _LoadWorker(QObject):
    """后台加载分享记录"""

    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def run(self):
        try:
            records = get_records(limit=500)
            self.finished.emit(records)
        except Exception as e:
            self.error.emit(f"{e}\n{traceback.format_exc()}")


# ════════════════════════════════════════════════════════════
# 单条记录行
# ════════════════════════════════════════════════════════════


class _RecordItem(QFrame):
    """单条分享记录展示行"""

    deleted = pyqtSignal(int)  # 删除请求，携带 record id
    downloaded = pyqtSignal(int)  # 下载完成，携带 record id（通知父卡片刷新）

    def __init__(
        self,
        record: Dict[str, Any],
        parent=None,
        theme: Optional[dict] = None,
    ):
        super().__init__(parent)
        self._record = record

        # 主题配置（合并默认值）
        self._theme = dict(_DEFAULT_THEME)
        if theme:
            self._theme.update(theme)

        # widget 引用（供 refresh_theme 用）
        self._title_lb: Optional[QLabel] = None
        self._time_lb: Optional[QLabel] = None
        self._info_lb: Optional[QLabel] = None
        self._badge: Optional[QLabel] = None
        self._open_btn: Optional[QPushButton] = None
        self._copy_btn: Optional[QPushButton] = None
        self._missing_btn: Optional[QPushButton] = None
        self._download_btn: Optional[QPushButton] = None
        self._delete_btn: Optional[QPushButton] = None

        # 下载线程/worker 引用（防止局部变量被 GC 导致线程信号丢失）
        self._download_thread: Optional[QThread] = None
        self._download_worker: Optional[_DownloadWorker] = None

        self._setup_ui()

    def _setup_ui(self):
        rec = self._record
        rtype = rec.get("type", "session")
        title = rec.get("title", "未命名")
        fmt = rec.get("format", "")
        created_at = rec.get("created_at", "")
        file_path = rec.get("file_path", "") or ""
        upload_url = rec.get("upload_url", "") or ""
        extra = rec.get("extra_info", {})
        if isinstance(extra, str):
            try:
                extra = json_mod.loads(extra)
            except Exception:
                extra = {}

        self.setObjectName("recordRow")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        # ── 第一行：类型图标 + 标题 + 格式标签 ──
        row1 = QHBoxLayout()
        row1.setSpacing(6)

        icon_lb = QLabel(_TYPE_ICONS.get(rtype, "📄"))
        icon_lb.setFixedWidth(24)
        icon_lb.setAlignment(Qt.AlignCenter)
        icon_lb.setStyleSheet("background: transparent; font-size: 14px;")
        row1.addWidget(icon_lb)

        self._title_lb = QLabel(title)
        self._title_lb.setObjectName("recordRowTitle")
        self._title_lb.setWordWrap(False)
        row1.addWidget(self._title_lb, 1)

        badge_text = fmt.upper() if fmt else _TYPE_LABELS.get(rtype, "")
        if badge_text:
            self._badge = QLabel(badge_text)
            self._badge.setObjectName("recordRowBadge")
            self._badge.setFixedHeight(20)
            row1.addWidget(self._badge)

        layout.addLayout(row1)

        # ── 第二行：时间 + 信息 ──
        row2 = QHBoxLayout()
        row2.setSpacing(12)

        self._time_lb = QLabel(created_at)
        self._time_lb.setObjectName("recordRowTime")
        row2.addWidget(self._time_lb)

        info_parts = []
        if rtype == "session":
            mc = extra.get("msg_count", 0)
            if mc:
                info_parts.append(f"{mc} 轮对话")
            proj = extra.get("project", "")
            if proj:
                info_parts.append(f"📁 {proj}")
        elif rtype == "project":
            sc = extra.get("session_count", 0)
            if sc:
                info_parts.append(f"{sc} 个会话")

        if info_parts:
            self._info_lb = QLabel(" · ".join(info_parts))
            self._info_lb.setObjectName("recordRowInfo")
            row2.addWidget(self._info_lb)

        row2.addStretch()
        layout.addLayout(row2)

        # ── 第三行：操作按钮 ──
        row3 = QHBoxLayout()
        row3.setSpacing(6)
        row3.addStretch()

        has_file = bool(file_path) and Path(file_path).exists()
        has_url = bool(upload_url)

        if has_file:
            self._open_btn = QPushButton("📂 打开")
            self._open_btn.setFixedHeight(26)
            self._open_btn.setCursor(Qt.PointingHandCursor)
            self._open_btn.clicked.connect(lambda: self._open_file(file_path))
            row3.addWidget(self._open_btn)
        elif has_url:
            self._download_btn = QPushButton("📥 下载")
            self._download_btn.setFixedHeight(26)
            self._download_btn.setCursor(Qt.PointingHandCursor)
            self._download_btn.clicked.connect(lambda: self._download_file(upload_url))
            row3.addWidget(self._download_btn)
        else:
            self._missing_btn = QPushButton("📂 文件缺失")
            self._missing_btn.setFixedHeight(26)
            self._missing_btn.setEnabled(False)
            row3.addWidget(self._missing_btn)

        if has_url:
            self._copy_btn = QPushButton("🔗 复制链接")
            self._copy_btn.setFixedHeight(26)
            self._copy_btn.setCursor(Qt.PointingHandCursor)
            self._copy_btn.clicked.connect(lambda: self._copy_link(upload_url))
            row3.addWidget(self._copy_btn)

        # 删除按钮
        self._delete_btn = QPushButton("🗑️")
        self._delete_btn.setFixedHeight(26)
        self._delete_btn.setFixedWidth(32)
        self._delete_btn.setCursor(Qt.PointingHandCursor)
        self._delete_btn.setToolTip("删除此记录")
        self._delete_btn.clicked.connect(self._on_delete_clicked)
        row3.addWidget(self._delete_btn)

        layout.addLayout(row3)

        # 最后用主题色装饰全部
        self._apply_theme()

    def _on_delete_clicked(self):
        """弹出确认对话框，确认后发射 deleted 信号"""
        parent = self.window()
        reply = QMessageBox.question(
            parent or self,
            "确认删除",
            f"确定要删除「{self._record.get('title', '未命名')}」这条分享记录吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            rid = self._record.get("id")
            if rid is not None:
                self.deleted.emit(rid)

    def _apply_theme(self):
        """用当前缓存的颜色值刷新全部样式表"""
        t = self._theme
        fs = t["fs"]
        ff = t["ff"]

        # QFrame 自身背景 + 边框
        self.setStyleSheet(f"""
            #recordRow {{
                background: {t["card_bg_dim"]};
                border: 1px solid {t["border_c"]};
                border-radius: 8px;
            }}
            #recordRow:hover {{
                background: {t["hover_bg"]};
                border: 1px solid {t["border_c"]};
            }}
        """)

        # 标题
        if self._title_lb:
            self._title_lb.setStyleSheet(
                f"font-weight: 600; background: transparent; font-size: {fs}px; color: {t['tc']}; font-family: '{ff}';"
            )

        # 时间
        if self._time_lb:
            self._time_lb.setStyleSheet(
                f"background: transparent; font-size: {max(fs - 1, 12)}px; color: {t['tcs']}; font-family: '{ff}';"
            )

        # 辅助信息
        if self._info_lb:
            self._info_lb.setStyleSheet(
                f"background: transparent; font-size: {max(fs - 1, 12)}px; color: {t['tcs']}; font-family: '{ff}';"
            )

        # 格式徽章
        if self._badge:
            self._badge.setStyleSheet(f"""
                QLabel {{
                    background: {t["badge_bg"]};
                    border-radius: 4px;
                    padding: 0 8px;
                    font-size: 11px;
                    font-weight: 500;
                    color: {t["tcs"]};
                    font-family: '{ff}';
                }}
            """)

        btn_fs = max(fs - 2, 11)

        # 打开按钮
        if self._open_btn:
            self._open_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {t["btn_bg"]};
                    border: 1px solid {t["btn_border"]};
                    border-radius: 4px;
                    padding: 0 10px;
                    color: {t["tc"]};
                    font-size: {btn_fs}px;
                    font-family: '{ff}';
                }}
                QPushButton:hover {{
                    background: {t["hover_bg"]};
                }}
            """)

        # 复制按钮
        if self._copy_btn:
            self._copy_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {t["btn_bg"]};
                    border: 1px solid {t["btn_border"]};
                    border-radius: 4px;
                    padding: 0 10px;
                    color: {t["tc"]};
                    font-size: {btn_fs}px;
                    font-family: '{ff}';
                }}
                QPushButton:hover {{
                    background: {t["hover_bg"]};
                }}
            """)

        # 文件缺失
        if self._missing_btn:
            self._missing_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {t["card_bg_dim"]};
                    border: 1px solid transparent;
                    border-radius: 4px;
                    padding: 0 10px;
                    color: {t["btn_disabled"]};
                    font-size: {btn_fs}px;
                    font-family: '{ff}';
                }}
            """)

        # 下载按钮
        if self._download_btn:
            self._download_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {t["btn_bg"]};
                    border: 1px solid {t["btn_border"]};
                    border-radius: 4px;
                    padding: 0 10px;
                    color: {t["tc"]};
                    font-size: {btn_fs}px;
                    font-family: '{ff}';
                }}
                QPushButton:hover {{
                    background: {t["hover_bg"]};
                }}
            """)

        # 删除按钮
        if self._delete_btn:
            self._delete_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    border: 1px solid transparent;
                    border-radius: 4px;
                    padding: 0 4px;
                    font-size: {btn_fs}px;
                }}
                QPushButton:hover {{
                    background: rgba(255,60,60,0.15);
                    border: 1px solid rgba(255,60,60,0.3);
                }}
            """)

    def refresh_theme(self, theme: dict):
        """主题切换时由父卡片调用，更新全部颜色"""
        self._theme.update(theme)
        self._apply_theme()

    def _open_file(self, path: str):
        if not path or not Path(path).exists():
            return
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                folder = str(Path(path).parent)
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            logger.warning(f"[ShareHistory] 打开文件失败: {e}")

    def _copy_link(self, url: str):
        if url:
            QApplication.clipboard().setText(url)
            parent = self.window()
            if parent:
                InfoBar.success(
                    title="",
                    content="链接已复制到剪贴板",
                    duration=2000,
                    parent=parent,
                )

    def _download_file(self, url: str):
        """后台下载分享文件到本地（QThread + signals，不卡 UI）"""
        rec = self._record
        record_id = rec.get("id")
        rtype = rec.get("type", "session")
        title = rec.get("title", "未命名")
        safe_title = "".join(c for c in title if c not in r'<>:"/\|?*').rstrip(". ") or "download"

        # 确定保存目录
        share_dir = _share_dir()
        if rtype == "session":
            save_dir = share_dir / "sessions"
        else:
            save_dir = share_dir / "projects"
        save_dir.mkdir(parents=True, exist_ok=True)

        # 从 URL 推断扩展名
        ext = ".html"
        url_lower = url.lower()
        if ".md" in url_lower or "markdown" in url_lower:
            ext = ".md"
        elif ".json" in url_lower:
            ext = ".json"
        elif ".drifox_project" in url_lower or ".zip" in url_lower:
            ext = ".drifox_project"

        filename = f"{safe_title}{ext}"
        save_path = save_dir / filename

        # 禁用按钮，显示进度
        if self._download_btn:
            self._download_btn.setText("⏳ 下载中…")
            self._download_btn.setEnabled(False)

        # ── QThread 方式：和 DownloadThread 同模式 ──
        # ⚠️ 必须保存为实例变量，防止 Python GC 在线程启动前回收 worker/thread
        self._cleanup_download()
        self._download_worker = _DownloadWorker(url, str(save_path))
        self._download_thread = QThread(self)
        self._download_worker.moveToThread(self._download_thread)

        self._download_thread.started.connect(self._download_worker.run)
        self._download_worker.finished.connect(lambda fp: self._on_download_done(fp, record_id))
        self._download_worker.error.connect(self._on_download_error)
        self._download_worker.finished.connect(self._download_thread.quit)
        self._download_worker.error.connect(self._download_thread.quit)
        self._download_thread.finished.connect(self._download_thread.deleteLater)
        self._download_thread.finished.connect(self._cleanup_download)
        self._download_thread.start()

    def _cleanup_download(self):
        """清理下载线程引用，防止内存泄漏"""
        self._download_worker = None
        self._download_thread = None

    def _on_download_done(self, file_path: str, record_id: int):
        """下载成功回调（主线程）"""
        self._cleanup_download()
        update_record_file_path(record_id, file_path)
        self.downloaded.emit(record_id)

        parent = self.window()
        if parent:
            InfoBar.success(
                title="",
                content=f"已下载到 {Path(file_path).name}",
                duration=3000,
                parent=parent,
            )

    def _on_download_error(self, err: str):
        """下载失败回调（主线程）"""
        logger.warning(f"[ShareHistory] 下载失败: {err}")
        self._cleanup_download()
        if self._download_btn:
            self._download_btn.setText("📥 重试")
            self._download_btn.setEnabled(True)

        parent = self.window()
        if parent:
            InfoBar.error(
                title="",
                content=f"下载失败: {err}",
                duration=3000,
                parent=parent,
            )


class _DownloadWorker(QObject):
    """文件下载 Worker — 与 app.utils.utils.DownloadThread 同模式，QThread + signals"""

    finished = pyqtSignal(str)  # 文件路径
    error = pyqtSignal(str)  # 错误信息

    def __init__(self, url: str, file_path: str):
        super().__init__()
        self._url = url
        self._file_path = file_path

    def run(self):
        from urllib.parse import urlsplit, urlunsplit, quote
        from urllib.request import Request, urlopen

        try:
            # 关键修复：对 URL 路径进行 percent-encoding，防止中文字符导致 UnicodeEncodeError
            parts = urlsplit(self._url)
            safe_path = quote(parts.path, safe="/:@!$&'()*+,;=-._~%")
            safe_url = urlunsplit(parts._replace(path=safe_path)) if safe_path != parts.path else self._url

            req = Request(safe_url, headers={"User-Agent": "DriFox/1.0"})
            with urlopen(req, timeout=30) as resp:
                data = resp.read()
            Path(self._file_path).write_bytes(data)
            self.finished.emit(self._file_path)
        except Exception as e:
            self.error.emit(str(e))


# ════════════════════════════════════════════════════════════
# 主卡片（严格对齐官方卡片视觉语言）
# ════════════════════════════════════════════════════════════


class ShareHistoryCard(QWidget):
    """分享记录管理浮动卡片"""

    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._context_provider: Optional[Callable[[], dict]] = None
        self._worker_thread: Optional[QThread] = None
        self._worker: Optional[_LoadWorker] = None
        self._header_icon: Optional[IconWidget] = None

        # 全量记录 + 主题配置
        self._all_records: List[Dict[str, Any]] = []
        self._theme = dict(_DEFAULT_THEME)

        # widget 引用
        self._search_input: Optional[QLineEdit] = None
        self._clear_btn: Optional[ToolButton] = None

        self._setup_ui()

    # ── 上下文注入 ──────────────────────────────────────

    def set_context_provider(self, provider: Callable[[], dict]):
        self._context_provider = provider

    def show_card(self):
        self._apply_latest_theme()
        self._apply_plugin_icon()
        self._load_records()
        self.setVisible(True)

    def _apply_plugin_icon(self):
        if self._context_provider is None or self._header_icon is None:
            return
        try:
            from PyQt5.QtGui import QIcon

            ctx = self._context_provider()
            icon_info = ctx.get("plugin_icon", {})
            theme = "dark" if isDarkTheme() else "light"
            icon_path = icon_info.get(theme, "")
            if icon_path:
                self._header_icon.setIcon(QIcon(icon_path))
        except Exception:
            pass

    # ── 主题 ────────────────────────────────────────────

    def _apply_latest_theme(self):
        if self._context_provider is None:
            return
        try:
            ctx = self._context_provider()
        except Exception:
            return

        font_family, font_size = _ctx_font(ctx)
        tc = _ctx_text_color(ctx)
        tcs = _ctx_text_color(ctx, secondary=True)
        border_c = _ctx_border_color(ctx)

        colors = ctx.get("colors", {})
        self._theme.update(
            {
                "tc": tc,
                "tcs": tcs,
                "border_c": border_c,
                "card_bg_dim": colors.get("card_bg_dim", "rgba(128,128,128,0.06)"),
                "hover_bg": colors.get("hover_bg", "rgba(128,128,128,0.10)"),
                "badge_bg": colors.get("card_bg_dim", "rgba(128,128,128,0.10)"),
                "btn_bg": colors.get("toolbar_bg", "rgba(128,128,128,0.08)"),
                "btn_border": border_c,
                "btn_disabled": colors.get("text_muted", "rgba(128,128,128,0.4)"),
                "ff": font_family,
                "fs": font_size,
            }
        )

        # 第 1 层：QFont 级联
        if font_family:
            self.setFont(QFont(font_family, font_size if font_size else 14))

        # 第 2+3 层
        self._retheme()

        # 分隔线
        try:
            self._sep.setStyleSheet(f"background: {border_c}; max-height: 1px;")
        except RuntimeError:
            pass

    def _retheme(self):
        """刷新全部子控件颜色+字体"""
        t = self._theme
        tc = t["tc"]
        tcs = t["tcs"]
        ff = t["ff"]
        fs = t["fs"]

        # 刷新 header 标题/状态
        try:
            self._header_title.setStyleSheet(f"color: {tc}; background: transparent;")
            self._status_lb.setStyleSheet(f"color: {tcs}; font-size: 12px; background: transparent;")
        except RuntimeError:
            pass

        # 搜索框样式
        try:
            if self._search_input:
                self._search_input.setStyleSheet(f"""
                    QLineEdit {{
                        background: {t["card_bg_dim"]};
                        border: 1px solid {t["border_c"]};
                        border-radius: 4px;
                        padding: 4px 8px;
                        color: {tc};
                        font-size: 12px;
                        font-family: '{ff}';
                        selection-background-color: {tc};
                    }}
                """)
        except RuntimeError:
            pass

        # QLabel 级联刷新
        for child in self.findChildren(QLabel):
            try:
                if isinstance(child, FluentLabelBase) and ff:
                    child.setFont(QFont(ff, fs))

                ss = child.styleSheet()
                if not ss:
                    continue
                new_ss = re.sub(r"color:\s*[^;]+;", f"color: {tc};", ss)
                if fs:
                    new_ss = re.sub(r"font-size:\s*[^;]+;", f"font-size: {fs}px;", new_ss)
                if ff and f"font-family: '{ff}'" not in new_ss:
                    new_ss += f" font-family: '{ff}';"
                child.setStyleSheet(new_ss)
            except RuntimeError:
                pass

        # QPushButton 字体
        for child in self.findChildren(QPushButton):
            try:
                cur = child.styleSheet()
                child.setStyleSheet(cur + f" font-size: {max(fs - 2, 11)}px; font-family: '{ff}';")
            except RuntimeError:
                pass

        # 刷新 _RecordItem 子项
        for child in self.findChildren(_RecordItem):
            try:
                child.refresh_theme(self._theme)
            except RuntimeError:
                pass

    # ── 界面搭建 ────────────────────────────────────────

    def _setup_ui(self):
        self.setMinimumHeight(0)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("ShareHistoryCard { background: transparent; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 头部 ──
        self._build_header(root)

        # ── 分隔线 ──
        self._sep = QFrame(self)
        self._sep.setFrameShape(QFrame.HLine)
        self._sep.setStyleSheet("background: rgba(128,128,128,0.15); max-height: 1px;")
        root.addWidget(self._sep)

        # ── 搜索条 ──
        self._build_search_bar(root)

        # ── 滚动内容区 ──
        self._scroll = ScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(
            "ScrollArea { background: transparent; border: none; }"
            "ScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical {"
            "    width: 6px; background: transparent;"
            "}"
            "QScrollBar::handle:vertical {"
            "    background: rgba(255,255,255,0.12);"
            "    border-radius: 3px; min-height: 30px;"
            "}"
            "QScrollBar::add-line:vertical,"
            "QScrollBar::sub-line:vertical { height: 0; }"
        )
        self._content = QWidget(self._scroll)
        self._content.setStyleSheet("background: transparent;")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(12, 8, 12, 8)
        self._content_layout.setSpacing(6)
        self._content_layout.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll, 1)

    def _build_header(self, root: QVBoxLayout):
        """标题栏：图标 + 标题 + 状态 + 清空 + 刷新 + 关闭"""
        header = QWidget(self)
        header.setStyleSheet("background: transparent;")
        hly = QHBoxLayout(header)
        hly.setContentsMargins(16, 12, 16, 4)
        hly.setSpacing(8)

        # 图标
        self._header_icon = IconWidget(FluentIcon.HISTORY, header)
        self._header_icon.setFixedSize(22, 22)
        hly.addWidget(self._header_icon)

        # 标题
        self._header_title = StrongBodyLabel("分享记录", header)
        self._header_title.setStyleSheet(f"color: {_text_color()}; background: transparent;")
        hly.addWidget(self._header_title)

        # 状态/计数
        self._status_lb = QLabel("", header)
        self._status_lb.setStyleSheet(
            f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;"
        )
        hly.addWidget(self._status_lb)

        hly.addStretch(1)

        # 清空按钮
        self._clear_btn = ToolButton(FluentIcon.DELETE, header)
        self._clear_btn.setToolTip("清空全部记录")
        self._clear_btn.clicked.connect(self._on_clear_all)
        hly.addWidget(self._clear_btn)

        # 刷新按钮
        self._refresh_btn = ToolButton(FluentIcon.SYNC, header)
        self._refresh_btn.setToolTip("刷新")
        self._refresh_btn.clicked.connect(self._load_records)
        hly.addWidget(self._refresh_btn)

        # 关闭按钮
        close_btn = TransparentToolButton(FluentIcon.CLOSE, header)
        close_btn.setFixedSize(24, 24)
        close_btn.setToolTip("关闭")
        close_btn.clicked.connect(self._on_close)
        hly.addWidget(close_btn)

        root.addWidget(header)

    def _build_search_bar(self, root: QVBoxLayout):
        """搜索输入条"""
        container = QWidget(self)
        container.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(container)
        layout.setContentsMargins(16, 6, 16, 2)
        layout.setSpacing(0)

        search_icon = QLabel("🔍")
        search_icon.setStyleSheet("background: transparent; font-size: 12px;")
        layout.addWidget(search_icon)

        self._search_input = QLineEdit(container)
        self._search_input.setPlaceholderText("搜索标题…")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.setFixedHeight(28)
        self._search_input.textChanged.connect(self._on_search_changed)
        layout.addWidget(self._search_input, 1)

        root.addWidget(container)

    # ── 搜索 ────────────────────────────────────────────

    def _on_search_changed(self, text: str):
        """搜索文本变化 → 重新过滤渲染"""
        self._apply_filter(text.strip())

    def _apply_filter(self, keyword: str):
        """根据关键字过滤并重新渲染"""
        if not keyword:
            self._render_records(self._all_records, update_search=False)
            return

        keyword_lower = keyword.lower()
        filtered = [r for r in self._all_records if keyword_lower in r.get("title", "").lower()]
        self._render_records(filtered, update_search=False)

    # ── 比例高度 ────────────────────────────────────────

    def sizeHint(self):
        base = super().sizeHint()
        win = self.window()
        if win and win.height() > 0:
            return QSize(max(base.width(), 200), int(win.height() * 0.85))
        return base

    def showEvent(self, event):
        super().showEvent(event)
        win = self.window()
        if win:
            win.installEventFilter(self)
            self.updateGeometry()

    def eventFilter(self, obj, event):
        if obj is self.window() and event.type() == QEvent.Resize:
            self.updateGeometry()
        return super().eventFilter(obj, event)

    # ── 数据加载 ────────────────────────────────────────

    def _load_records(self):
        """异步加载分享记录"""
        self._cleanup_worker()
        self._refresh_btn.setEnabled(False)
        self._status_lb.setText("读取中…")
        self._show_empty_state()

        # 清空搜索框
        if self._search_input:
            self._search_input.blockSignals(True)
            self._search_input.clear()
            self._search_input.blockSignals(False)

        self._worker = _LoadWorker()
        self._worker_thread = QThread(self)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_records_loaded)
        self._worker.error.connect(self._on_load_error)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.error.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _on_records_loaded(self, records: List[Dict[str, Any]]):
        self._refresh_btn.setEnabled(True)
        self._status_lb.setText("")
        self._cleanup_worker()

        self._all_records = records
        self._render_records(records, update_search=False)

    def _on_load_error(self, err: str):
        logger.warning(f"[ShareHistory] 加载记录失败: {err}")
        self._refresh_btn.setEnabled(True)
        self._status_lb.setText("")
        self._cleanup_worker()

    # ── 渲染 ────────────────────────────────────────────

    def _render_records(self, records: List[Dict[str, Any]], update_search: bool = True):
        """渲染记录列表"""
        # 清空内容
        self._clear_content()

        if not records:
            self._show_empty_state()
            self._status_lb.setText(f"共 {len(self._all_records)} 条" if self._all_records else "")
            return

        self._status_lb.setText(f"共 {len(records)} 条")
        for rec in records:
            item = _RecordItem(rec, theme=self._theme)
            item.deleted.connect(self._on_item_deleted)
            item.downloaded.connect(self._on_item_downloaded)
            self._content_layout.addWidget(item)

        self._content_layout.addStretch()

        # 不需要再调 _retheme() — _RecordItem 构造时已用最新 theme

    def _show_empty_state(self):
        """显示空状态"""
        self._clear_content()
        t = self._theme
        empty_lb = QLabel("暂无分享记录\n分享会话或导出项目后，记录将出现在这里")
        empty_lb.setAlignment(Qt.AlignCenter)
        empty_lb.setWordWrap(True)
        empty_lb.setStyleSheet(
            f"color: {t['tcs']}; background: transparent; padding: 40px;"
            f" font-family: '{t['ff']}'; font-size: {t['fs']}px;"
        )
        self._content_layout.addWidget(empty_lb, 1, Qt.AlignCenter)

    def _clear_content(self):
        """清空内容布局"""
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

    # ── 删除 ────────────────────────────────────────────

    def _on_item_deleted(self, record_id: int):
        """处理单条记录删除"""
        if delete_record(record_id):
            # 从内存缓存中移除
            self._all_records = [r for r in self._all_records if r.get("id") != record_id]
            # 重新应用搜索过滤
            keyword = self._search_input.text().strip() if self._search_input else ""
            self._apply_filter(keyword)

            parent = self.window()
            if parent:
                InfoBar.success(
                    title="",
                    content="记录已删除",
                    duration=2000,
                    parent=parent,
                )
        else:
            parent = self.window()
            if parent:
                InfoBar.error(
                    title="",
                    content="删除失败",
                    duration=2000,
                    parent=parent,
                )

    def _on_item_downloaded(self, record_id: int):
        """处理下载完成后刷新记录列表"""
        # 重新加载全量记录并刷新渲染
        self._load_records()

    def _on_clear_all(self):
        """清空全部记录"""
        if not self._all_records:
            return

        parent = self.window()
        reply = QMessageBox.question(
            parent or self,
            "确认清空",
            f"确定要清空全部 {len(self._all_records)} 条分享记录吗？\n此操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            if clear_all_records():
                self._all_records.clear()
                self._render_records([], update_search=False)

                if parent:
                    InfoBar.success(
                        title="",
                        content="已清空全部记录",
                        duration=2000,
                        parent=parent,
                    )
            else:
                if parent:
                    InfoBar.error(
                        title="",
                        content="清空失败",
                        duration=2000,
                        parent=parent,
                    )

    # ── 生命周期 ────────────────────────────────────────

    def _cleanup_worker(self):
        if self._worker_thread is not None:
            try:
                self._worker_thread.quit()
                self._worker_thread.wait(500)
            except RuntimeError:
                pass
            self._worker_thread = None
        self._worker = None

    def _on_close(self):
        self.closed.emit()

    def deleteLater(self):
        self._cleanup_worker()
        super().deleteLater()

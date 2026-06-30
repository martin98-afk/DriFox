# -*- coding: utf-8 -*-
"""MarketplaceCard 浮动卡片 — 完整的插件市场浏览界面

功能：
- 异步拉取市场列表（不阻塞 UI）
- 异步安装/卸载插件
- 插件搜索过滤
- 安装状态实时反馈
"""

import traceback
from typing import Optional

from PyQt5.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    IconWidget,
    LineEdit,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
    ToolButton,
    TransparentToolButton,
    isDarkTheme,
)

from .data import get_marketplace
from .installer import get_installer

# ── 主题色辅助 ──────────────────────────────────────────────


def _text_color(secondary: bool = False) -> str:
    """返回当前主题下的文字颜色"""
    if isDarkTheme():
        return "rgba(255,255,255,0.55)" if secondary else "rgba(255,255,255,0.9)"
    return "rgba(0,0,0,0.45)" if secondary else "rgba(0,0,0,0.85)"


# ── 异步工作器 ──────────────────────────────────────────────


class _MarketplaceWorker(QObject):
    """在后台线程执行阻塞操作，通过信号返回结果"""

    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self):
        try:
            result = self._fn(*self._args, **self._kwargs)
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(f"{e}\n{traceback.format_exc()}")


# ── 单行插件卡片 ────────────────────────────────────────────


class _PluginRow(QFrame):
    """单个插件的展示行（简约卡片风格）"""

    installRequested = pyqtSignal(dict)  # plugin_meta

    def __init__(self, plugin_meta: dict, installed: bool, parent=None):
        super().__init__(parent)
        self._meta = plugin_meta
        self._installed = installed
        self._busy = False
        self._setup_ui()

    def _setup_ui(self):
        self.setObjectName("pluginRow")
        # 透明背景 + 悬停微亮
        self.setStyleSheet(
            "#pluginRow { background: transparent; border: 1px solid rgba(128,128,128,0.15); border-radius: 8px; padding: 0px; }"
            "#pluginRow:hover { background: rgba(128,128,128,0.05); }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        # 图标
        icon = IconWidget(FluentIcon.APPLICATION, self)
        icon.setFixedSize(20, 20)
        layout.addWidget(icon)

        # 信息区
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)

        name = self._meta.get("name", "未知")
        ver = self._meta.get("version", "")
        title = StrongBodyLabel(f"{name}  v{ver}" if ver else name, self)
        title.setStyleSheet(f"color: {_text_color()}; background: transparent;")
        info_layout.addWidget(title)

        desc = self._meta.get("description", "")
        if desc:
            desc_label = QLabel(desc[:120], self)
            desc_label.setWordWrap(True)
            desc_label.setStyleSheet(f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;")
            info_layout.addWidget(desc_label)

        layout.addLayout(info_layout, 1)

        # 操作按钮
        self._btn = PushButton(self)
        self._btn.setFixedWidth(100)
        self._update_btn_text()
        self._btn.clicked.connect(self._on_click)
        layout.addWidget(self._btn)

    def _update_btn_text(self):
        if self._busy:
            self._btn.setText("处理中…")
            self._btn.setEnabled(False)
        elif self._installed:
            self._btn.setText("已安装")
            self._btn.setEnabled(False)
        else:
            self._btn.setText("安装")
            self._btn.setEnabled(True)

    def _on_click(self):
        if self._busy or self._installed:
            return
        self._busy = True
        self._update_btn_text()
        self.installRequested.emit(self._meta)

    def set_installed(self, installed: bool):
        """安装完成后刷新状态"""
        self._installed = installed
        self._busy = False
        self._update_btn_text()

    def set_error(self):
        """安装失败后恢复按钮"""
        self._busy = False
        self._btn.setText("安装失败，重试")
        self._btn.setEnabled(True)


# ── 市场主卡片 ──────────────────────────────────────────────


class MarketplaceCard(QWidget):
    """插件市场浮动卡片"""

    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._worker_thread: Optional[QThread] = None
        self._worker: Optional[_MarketplaceWorker] = None
        self._plugin_data: list = []
        self._setup_ui()
        # 启动时自动拉取（延迟确保容器就绪）
        from PyQt5.QtCore import QTimer

        QTimer.singleShot(100, self._async_refresh)

    # ── 界面搭建 ──

    def _setup_ui(self):
        self.setMinimumHeight(400)
        # 半透明背景
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("MarketplaceCard { background: transparent; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 头部 ──
        header = QWidget(self)
        header.setStyleSheet("background: transparent;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 4)
        header_layout.setSpacing(8)

        icon = IconWidget(FluentIcon.SHOPPING_CART, header)
        icon.setFixedSize(22, 22)
        header_layout.addWidget(icon)

        title = StrongBodyLabel("插件市场", header)
        title.setStyleSheet(f"color: {_text_color()}; background: transparent;")
        header_layout.addWidget(title)

        header_layout.addStretch(1)

        # 搜索框
        self._search_edit = LineEdit(header)
        self._search_edit.setPlaceholderText("搜索插件…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setFixedWidth(160)
        self._search_edit.setStyleSheet(
            f"background: rgba(128,128,128,0.1); border-radius: 8px; padding: 4px 8px; color: {_text_color()};"
        )
        self._search_edit.textChanged.connect(self._filter_plugins)
        header_layout.addWidget(self._search_edit)

        # 刷新按钮
        self._refresh_btn = ToolButton(FluentIcon.SYNC, header)
        self._refresh_btn.setToolTip("刷新")
        self._refresh_btn.clicked.connect(self._async_refresh)
        header_layout.addWidget(self._refresh_btn)

        # 关闭按钮
        self._close_btn = TransparentToolButton(FluentIcon.CLOSE, header)
        self._close_btn.setFixedSize(24, 24)
        self._close_btn.setToolTip("关闭")
        self._close_btn.clicked.connect(self._on_close)
        header_layout.addWidget(self._close_btn)

        # 状态标签
        self._status_label = QLabel("", header)
        self._status_label.setStyleSheet(
            f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;"
        )
        header_layout.addWidget(self._status_label)

        root.addWidget(header)

        # ── 分隔线 ──
        sep = QFrame(self)
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background: rgba(128,128,128,0.15); max-height: 1px;")
        root.addWidget(sep)

        # ── 滚动内容区 ──
        self._scroll = ScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(
            "ScrollArea { background: transparent; border: none; }"
            "ScrollArea > QWidget > QWidget { background: transparent; }"
        )
        self._content = QWidget(self._scroll)
        self._content.setStyleSheet("background: transparent;")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(12, 8, 12, 8)
        self._content_layout.setSpacing(6)
        self._content_layout.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll, 1)

        # ── 空状态提示 ──
        self._empty_label = StrongBodyLabel("暂无可用插件", self)
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setStyleSheet(f"color: {_text_color(secondary=True)}; background: transparent;")
        self._empty_label.setVisible(False)
        root.addWidget(self._empty_label)

    # ── 异步刷新 ──

    def _async_refresh(self):
        """在后台线程拉取市场数据"""
        self._set_loading(True)
        self._cleanup_worker()
        self._worker = _MarketplaceWorker(lambda: get_marketplace().list_plugins())
        self._worker_thread = QThread(self)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_refresh_done)
        self._worker.error.connect(self._on_refresh_error)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.error.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _on_refresh_done(self, plugins: list):
        """刷新完成"""
        self._plugin_data = plugins or []
        self._set_loading(False)
        self._render_plugins(self._plugin_data)

    def _on_refresh_error(self, err: str):
        """刷新失败"""
        self._set_loading(False)
        self._status_label.setText("加载失败")
        self._status_label.setStyleSheet("color: rgba(255,80,80,0.7); font-size: 12px; background: transparent;")
        self._clear_plugin_list()
        self._empty_label.setText(f"无法加载市场数据：{err[:60]}")
        self._empty_label.setVisible(True)

    def _set_loading(self, loading: bool):
        """设置加载状态"""
        if loading:
            self._status_label.setText("加载中…")
            self._status_label.setStyleSheet(
                f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;"
            )
            self._refresh_btn.setEnabled(False)
        else:
            self._status_label.setText("")
            self._refresh_btn.setEnabled(True)

    # ── 渲染 ──

    def _render_plugins(self, plugins: list):
        """渲染插件列表"""
        self._clear_plugin_list()
        query = self._search_edit.text().strip().lower()
        installer = get_installer()
        count = 0
        for p in plugins:
            name = p.get("name", "")
            if query and query not in name.lower() and query not in (p.get("description", "")).lower():
                continue
            installed = installer.is_installed(name)
            row = _PluginRow(p, installed, self._content)
            row.installRequested.connect(self._async_install)
            self._content_layout.addWidget(row)
            count += 1

        if count == 0:
            self._empty_label.setText("没有匹配的插件" if query else "暂无可用插件")
            self._empty_label.setVisible(True)
        else:
            self._empty_label.setVisible(False)

    def _clear_plugin_list(self):
        """清空插件列表"""
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _filter_plugins(self):
        """搜索过滤（复用已有数据）"""
        self._render_plugins(self._plugin_data)

    # ── 异步安装 ──

    def _async_install(self, plugin_meta: dict):
        """在后台线程安装插件"""
        self._status_label.setText("安装中…")
        self._status_label.setStyleSheet(
            f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;"
        )
        name = plugin_meta.get("name", "")

        self._cleanup_worker()
        self._worker = _MarketplaceWorker(lambda m=plugin_meta: get_installer().install(m))
        self._worker_thread = QThread(self)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.finished.connect(lambda ok: self._on_install_done(name, bool(ok)))
        self._worker.error.connect(lambda e: self._on_install_error(name, e))
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.error.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _on_install_done(self, name: str, success: bool):
        """安装完成"""
        self._status_label.setText("")
        if success:
            self._update_row_state(name, installed=True)
        else:
            self._update_row_state(name, installed=False, error=True)

    def _on_install_error(self, name: str, err: str):
        """安装出错"""
        self._status_label.setText("安装失败")
        self._status_label.setStyleSheet("color: rgba(255,80,80,0.7); font-size: 12px; background: transparent;")
        self._update_row_state(name, installed=False, error=True)

    def _update_row_state(self, name: str, installed: bool, error: bool = False):
        """更新某插件行的状态"""
        for i in range(self._content_layout.count()):
            item = self._content_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), _PluginRow):
                row: _PluginRow = item.widget()
                if row._meta.get("name") == name:
                    if error and not installed:
                        row.set_error()
                    else:
                        row.set_installed(installed)
                    break

    # ── 清理 ──

    def _on_close(self):
        """关闭卡片"""
        self.setVisible(False)
        self.closed.emit()

    def _cleanup_worker(self):
        """安全清理旧的 worker/thread"""
        if self._worker_thread is not None:
            try:
                self._worker_thread.quit()
                self._worker_thread.wait(500)
            except RuntimeError:
                pass
            self._worker_thread = None
        self._worker = None

    def deleteLater(self):
        self._cleanup_worker()
        super().deleteLater()

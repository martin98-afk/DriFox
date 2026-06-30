# -*- coding: utf-8 -*-
"""PluginManagerCard 浮动卡片 — 管理已安装插件的启用/禁用/卸载

功能：
- 列出所有已安装插件（系统 + 用户）
- 启用已禁用的插件
- 禁用已启用的用户插件
- 卸载用户插件
- 搜索过滤
- 全部操作异步执行，不阻塞 UI

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
- 所有文件操作直接通过 shutil/stdlib 完成
- 基于插件目录的文件结构推理状态
"""

import json
import shutil
import traceback
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
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
from loguru import logger


# ── 路径常量 ──────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_USER_PLUGINS_DIR = Path.home() / ".drifox" / "plugins"
_USER_DISABLED_DIR = Path.home() / ".drifox" / "plugins-disabled"


def _plugins_root() -> Path:
    """项目中的顶级 plugins/ 目录"""
    return _PROJECT_ROOT / "plugins"


# ── 主题色 ──────────────────────────────────────────────


def _text_color(secondary: bool = False) -> str:
    if isDarkTheme():
        return "rgba(255,255,255,0.55)" if secondary else "rgba(255,255,255,0.9)"
    return "rgba(0,0,0,0.45)" if secondary else "rgba(0,0,0,0.85)"


# ── 插件发现 ──────────────────────────────────────────────


def _read_plugin_json(plugin_dir: Path) -> Optional[dict]:
    """读取插件目录下的 plugin.json"""
    for meta_dir in (".drifox-plugin", ".claude-plugin"):
        p = plugin_dir / meta_dir / "plugin.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return None


class PluginInfo:
    """单个插件的运行时信息"""

    __slots__ = ("name", "description", "version", "status", "path")

    def __init__(
        self,
        name: str,
        description: str = "",
        version: str = "",
        status: str = "enabled",
        path: Optional[Path] = None,
    ):
        self.name = name
        self.description = description
        self.version = version
        self.status = status  # "system" | "enabled" | "disabled"
        self.path = path

    @property
    def is_system(self) -> bool:
        return self.status == "system"

    @property
    def is_disabled(self) -> bool:
        return self.status == "disabled"

    @property
    def is_enabled(self) -> bool:
        return self.status == "enabled"

    def status_label(self) -> str:
        return {
            "enabled": "✅ 启用",
            "disabled": "⛔ 禁用",
            "system": "🔒 系统",
        }.get(self.status, self.status)


def _discover_plugins() -> list[PluginInfo]:
    """扫描所有目录，收集已安装插件"""
    seen: dict[str, PluginInfo] = {}

    # 1. 系统插件（插件根目录 plugins/ 下）
    plugins_root = _plugins_root()
    if plugins_root.exists():
        for child in sorted(plugins_root.iterdir()):
            if not child.is_dir():
                continue
            meta = _read_plugin_json(child)
            if meta is None:
                continue
            name = meta.get("name", child.name)
            if name not in seen:
                seen[name] = PluginInfo(
                    name=name,
                    description=meta.get("description", ""),
                    version=meta.get("version", ""),
                    status="system",
                    path=child,
                )

    # 2. 用户已启用插件 ~/.drifox/plugins/
    if _USER_PLUGINS_DIR.exists():
        for child in sorted(_USER_PLUGINS_DIR.iterdir()):
            if not child.is_dir():
                continue
            meta = _read_plugin_json(child)
            if meta is None:
                continue
            name = meta.get("name", child.name)
            if name in seen:
                continue
            seen[name] = PluginInfo(
                name=name,
                description=meta.get("description", ""),
                version=meta.get("version", ""),
                status="enabled",
                path=child,
            )

    # 3. 用户已禁用插件 ~/.drifox/plugins-disabled/
    if _USER_DISABLED_DIR.exists():
        for child in sorted(_USER_DISABLED_DIR.iterdir()):
            if not child.is_dir():
                continue
            meta = _read_plugin_json(child)
            if meta is None:
                continue
            name = meta.get("name", child.name)
            if name in seen:
                continue
            seen[name] = PluginInfo(
                name=name,
                description=meta.get("description", ""),
                version=meta.get("version", ""),
                status="disabled",
                path=child,
            )

    return sorted(seen.values(), key=lambda p: (0 if p.is_system else 1, p.name))


# ── 文件操作 ──────────────────────────────────────────────


def _do_enable(plugin: PluginInfo) -> bool:
    """启用已禁用的插件"""
    src = _USER_DISABLED_DIR / plugin.name
    if not src.exists():
        return False
    try:
        _USER_PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(_USER_PLUGINS_DIR / plugin.name))
        return True
    except Exception as e:
        logger.error(f"[PluginManager] 启用失败 {plugin.name}: {e}")
        return False


def _do_disable(plugin: PluginInfo) -> bool:
    """禁用已启用的用户插件"""
    src = _USER_PLUGINS_DIR / plugin.name
    if not src.exists():
        return False
    try:
        _USER_DISABLED_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(_USER_DISABLED_DIR / plugin.name))
        return True
    except Exception as e:
        logger.error(f"[PluginManager] 禁用失败 {plugin.name}: {e}")
        return False


def _do_uninstall(plugin: PluginInfo) -> bool:
    """卸载用户插件"""
    for d in (_USER_PLUGINS_DIR / plugin.name, _USER_DISABLED_DIR / plugin.name):
        if d.exists():
            try:
                shutil.rmtree(d)
            except Exception as e:
                logger.error(f"[PluginManager] 卸载失败 {plugin.name}: {e}")
                return False
    return True


# ── 异步工作器 ──────────────────────────────────────────


class _Worker(QObject):
    """后台执行阻塞操作"""

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


# ── 插件行组件 ──────────────────────────────────────────


class _PluginRow(QFrame):
    """单个插件的展示行"""

    actionRequested = pyqtSignal(str, object)  # action, PluginInfo

    def __init__(self, plugin: PluginInfo, parent=None):
        super().__init__(parent)
        self._plugin = plugin
        self._busy = False
        self._setup_ui()

    # ── 界面 ──

    def _setup_ui(self):
        self.setObjectName("pluginRow")
        self.setStyleSheet(
            "#pluginRow { background: transparent;"
            " border: 1px solid rgba(128,128,128,0.15);"
            " border-radius: 8px; padding: 0px; }"
            "#pluginRow:hover { background: rgba(128,128,128,0.05); }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        icon = IconWidget(FluentIcon.APPLICATION, self)
        icon.setFixedSize(20, 20)
        layout.addWidget(icon)

        # 信息区
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)

        title_w = QWidget(self)
        title_w.setStyleSheet("background: transparent;")
        title_ly = QHBoxLayout(title_w)
        title_ly.setContentsMargins(0, 0, 0, 0)
        title_ly.setSpacing(6)

        ver = self._plugin.version
        title_str = f"{self._plugin.name}  v{ver}" if ver else self._plugin.name
        title_lb = StrongBodyLabel(title_str, title_w)
        title_lb.setStyleSheet(f"color: {_text_color()}; background: transparent;")
        title_ly.addWidget(title_lb)

        # 状态标签
        status_tag = QLabel(self._plugin.status_label(), title_w)
        status_tag.setStyleSheet(
            f"color: {self._status_color()}; font-size: 11px; font-weight: bold; background: transparent;"
        )
        title_ly.addWidget(status_tag)
        title_ly.addStretch(1)
        info_layout.addWidget(title_w)

        desc = self._plugin.description
        if desc:
            dl = QLabel(desc[:160], self)
            dl.setWordWrap(True)
            dl.setStyleSheet(f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;")
            info_layout.addWidget(dl)

        type_lb = QLabel("系统插件" if self._plugin.is_system else "用户插件", self)
        type_lb.setStyleSheet(f"color: {_text_color(secondary=True)}; font-size: 11px; background: transparent;")
        info_layout.addWidget(type_lb)

        layout.addLayout(info_layout, 1)

        # 操作按钮
        self._btn_layout = QHBoxLayout()
        self._btn_layout.setSpacing(4)
        layout.addLayout(self._btn_layout)
        self._build_buttons()

    def _status_color(self) -> str:
        if self._plugin.status == "enabled":
            return "#4CAF50"
        elif self._plugin.status == "disabled":
            return "#FF9800"
        return "#2196F3"

    # ── 按钮构建 ──

    def _build_buttons(self):
        while self._btn_layout.count():
            item = self._btn_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if self._busy:
            lbl = QLabel("处理中…", self)
            lbl.setStyleSheet(f"color: {_text_color(secondary=True)}; font-size: 12px;")
            self._btn_layout.addWidget(lbl)
            return

        if self._plugin.is_system:
            lbl = QLabel("🔒", self)
            lbl.setToolTip("系统插件只读")
            self._btn_layout.addWidget(lbl)
            return

        if self._plugin.is_disabled:
            self._add_action_btn("启用", "#4CAF50", self._on_enable)
            self._add_action_btn("卸载", "#F44336", self._on_uninstall)
        else:
            self._add_action_btn("禁用", "#FF9800", self._on_disable)
            self._add_action_btn("卸载", "#F44336", self._on_uninstall)

    def _add_action_btn(self, text: str, color: str, slot):
        btn = PushButton(text, self)
        btn.setFixedWidth(60)
        btn.setStyleSheet(
            f"PushButton {{ color: {color}; border: 1px solid {color};"
            f" border-radius: 4px; padding: 4px 8px; font-size: 12px; background: transparent; }}"
            f"PushButton:hover {{ background: rgba({','.join(str(int(color[i : i + 2], 16)) for i in (1, 3, 5))},0.1); }}"
        )
        btn.clicked.connect(slot)
        self._btn_layout.addWidget(btn)

    # ── 操作触发 ──

    def _on_enable(self):
        self._set_busy(True)
        self.actionRequested.emit("enable", self._plugin)

    def _on_disable(self):
        self._set_busy(True)
        self.actionRequested.emit("disable", self._plugin)

    def _on_uninstall(self):
        reply = QMessageBox.question(
            self,
            "确认卸载",
            f"确定要卸载插件「{self._plugin.name}」吗？\n此操作不可恢复。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._set_busy(True)
            self.actionRequested.emit("uninstall", self._plugin)

    def _set_busy(self, busy: bool):
        self._busy = busy
        self._build_buttons()

    def refresh_state(self, plugin: PluginInfo):
        self._plugin = plugin
        self._busy = False
        self._build_buttons()


# ── 主卡片 ──────────────────────────────────────────────


class PluginManagerCard(QWidget):
    """插件管理器浮动卡片"""

    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._worker_thread: Optional[QThread] = None
        self._worker: Optional[_Worker] = None
        self._plugins: list[PluginInfo] = []
        self._setup_ui()

        from PyQt5.QtCore import QTimer

        QTimer.singleShot(100, self._async_refresh)

    # ── 界面 ──

    def _setup_ui(self):
        self.setMinimumHeight(400)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("PluginManagerCard { background: transparent; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 头部
        header = QWidget(self)
        header.setStyleSheet("background: transparent;")
        hly = QHBoxLayout(header)
        hly.setContentsMargins(16, 12, 16, 4)
        hly.setSpacing(8)

        ic = IconWidget(FluentIcon.ALIGNMENT, header)
        ic.setFixedSize(22, 22)
        hly.addWidget(ic)

        tl = StrongBodyLabel("插件管理", header)
        tl.setStyleSheet(f"color: {_text_color()}; background: transparent;")
        hly.addWidget(tl)

        self._count_lb = QLabel("", header)
        self._count_lb.setStyleSheet(f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;")
        hly.addWidget(self._count_lb)
        hly.addStretch(1)

        self._search = LineEdit(header)
        self._search.setPlaceholderText("搜索插件…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(160)
        self._search.setStyleSheet(
            f"background: rgba(128,128,128,0.1); border-radius: 8px; padding: 4px 8px; color: {_text_color()};"
        )
        self._search.textChanged.connect(self._filter_plugins)
        hly.addWidget(self._search)

        self._refresh_btn = ToolButton(FluentIcon.SYNC, header)
        self._refresh_btn.setToolTip("刷新")
        self._refresh_btn.clicked.connect(self._async_refresh)
        hly.addWidget(self._refresh_btn)

        close_btn = TransparentToolButton(FluentIcon.CLOSE, header)
        close_btn.setFixedSize(24, 24)
        close_btn.setToolTip("关闭")
        close_btn.clicked.connect(self._on_close)
        hly.addWidget(close_btn)

        root.addWidget(header)

        # 分隔线
        sep = QFrame(self)
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background: rgba(128,128,128,0.15); max-height: 1px;")
        root.addWidget(sep)

        # 滚动内容
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

        # 空状态
        self._empty = StrongBodyLabel("", self)
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setStyleSheet(f"color: {_text_color(secondary=True)}; background: transparent;")
        self._empty.setVisible(False)
        root.addWidget(self._empty)

    # ── 异步刷新 ──

    def _async_refresh(self):
        self._set_loading(True)
        self._cleanup_worker()
        w = _Worker(_discover_plugins)
        t = QThread(self)
        w.moveToThread(t)
        t.started.connect(w.run)
        w.finished.connect(self._on_refresh_done)
        w.error.connect(self._on_refresh_error)
        w.finished.connect(t.quit)
        w.error.connect(t.quit)
        w.finished.connect(w.deleteLater)
        w.error.connect(w.deleteLater)
        t.finished.connect(t.deleteLater)
        self._worker, self._worker_thread = w, t
        t.start()

    def _on_refresh_done(self, plugins: list[PluginInfo]):
        self._plugins = plugins or []
        self._set_loading(False)
        total = len(self._plugins)
        sc = sum(1 for p in self._plugins if p.is_system)
        self._count_lb.setText(f"共 {total} 个")
        self._render_plugins(self._plugins)

    def _on_refresh_error(self, err: str):
        self._set_loading(False)
        self._empty.setText(f"扫描失败：{err[:60]}")
        self._empty.setVisible(True)

    def _set_loading(self, loading: bool):
        self._refresh_btn.setEnabled(not loading)
        if loading:
            self._count_lb.setText("扫描中…")

    def _render_plugins(self, plugins: list[PluginInfo]):
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        query = self._search.text().strip().lower()
        count = 0
        for p in plugins:
            if query and query not in p.name.lower() and query not in p.description.lower():
                continue
            row = _PluginRow(p, self._content)
            row.actionRequested.connect(self._on_action_requested)
            self._content_layout.addWidget(row)
            count += 1

        self._empty.setText("没有匹配的插件" if query else "暂无已安装的插件")
        self._empty.setVisible(count == 0)

    def _filter_plugins(self):
        self._render_plugins(self._plugins)

    # ── 管理操作 ──

    def _on_action_requested(self, action: str, plugin: PluginInfo):
        names = {"enable": "启用中", "disable": "禁用中", "uninstall": "卸载中"}
        self._count_lb.setText(names.get(action, "处理中…"))

        fn_map = {
            "enable": lambda p=plugin: _do_enable(p),
            "disable": lambda p=plugin: _do_disable(p),
            "uninstall": lambda p=plugin: _do_uninstall(p),
        }
        fn = fn_map.get(action)
        if fn is None:
            return

        self._cleanup_worker()
        w = _Worker(fn)
        t = QThread(self)
        w.moveToThread(t)
        t.started.connect(w.run)

        def _done(ok):
            ok_bool = bool(ok)
            if ok_bool:
                self._async_refresh()
            else:
                self._count_lb.setText(f"{action} 失败")
                self._count_lb.setStyleSheet("color: rgba(255,80,80,0.7); font-size: 12px; background: transparent;")
                from PyQt5.QtCore import QTimer

                QTimer.singleShot(3000, self._restore_count)
                # 刷新恢复按钮状态
                self._async_refresh()

        w.finished.connect(_done)
        w.error.connect(lambda e: _done(False))
        w.finished.connect(t.quit)
        w.error.connect(t.quit)
        w.finished.connect(w.deleteLater)
        w.error.connect(w.deleteLater)
        t.finished.connect(t.deleteLater)
        self._worker, self._worker_thread = w, t
        t.start()

    def _restore_count(self):
        total = len(self._plugins)
        sc = sum(1 for p in self._plugins if p.is_system)
        self._count_lb.setText(f"共 {total} 个")
        self._count_lb.setStyleSheet(f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;")

    # ── 清理 ──

    def _on_close(self):
        self.setVisible(False)
        self.closed.emit()

    def _cleanup_worker(self):
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

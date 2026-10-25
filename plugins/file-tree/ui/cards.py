# -*- coding: utf-8 -*-
"""FileTreeCard 浮动卡片 — 项目文件树浏览（重构版）

功能：
- QTreeView + FileTreeModel + FilterProxy：多选 / 懒加载 / 搜索过滤
- 完整的文件 CRUD：新建 / 重命名(F2) / 删除 / 剪切 / 复制 / 粘贴
- 键盘快捷键：Ctrl+C/V/X、F2、Delete、Ctrl+A
- 右键菜单全功能
- 内部拖拽移动 + Ctrl拖拽复制
- 实时文件监听 + 异步扫描
- 主题色跟随主程序

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
"""

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from loguru import logger
from PyQt5.QtCore import QEvent, QItemSelectionModel, QModelIndex, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    IconWidget,
    MaskDialogBase,
    ScrollArea,
    SmoothScrollDelegate,
    StrongBodyLabel,
    TransparentPushButton,
    TransparentToolButton,
    isDarkTheme,
)

from .preview import FilePreviewPage, detect_kind
from .scanner import _DirEntry, _TreeScanner
from .tree_widget import (
    FileTreeFilterProxy,
    FileTreeModel,
    FileTreeView,
    RenameDelegate,
)
from .watcher import _DirWatcher

# ── 常量 ──────────────────────────────────────────────────

# 恢复队列队首目录等待扫描的超时秒数（超时跳过，防单个异常目录堵死整队）
_EXPAND_WAIT_TIMEOUT_S = 3.0

# _stack 页索引
_PAGE_TREE = 0
_PAGE_PLACEHOLDER = 1
_PAGE_PREVIEW = 2


def _same_path(a: str, b: str) -> bool:
    """路径同一性判定（Windows 大小写不敏感 + 分隔符 / 尾斜杠归一）

    裸字符串比较会把 ``D:/work/X`` 与 ``d:\\work\\X\\`` 判为不同根目录，
    导致同一项目的扫描结果被误当「换项目」处理（整树 reset → 展开状态全丢）。
    """
    if not a or not b:
        return False
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


# ── 主题色辅助 ────────────────────────────────────────────


def _text_color(secondary: bool = False) -> str:
    if isDarkTheme():
        return "rgba(255,255,255,0.55)" if secondary else "rgba(255,255,255,0.9)"
    return "rgba(0,0,0,0.45)" if secondary else "rgba(0,0,0,0.85)"


def _ctx_text_color(ctx: dict, secondary: bool = False) -> str:
    colors = ctx.get("colors", {})
    key = "text_secondary" if secondary else "text_primary"
    val = colors.get(key, "")
    return val if val else _text_color(secondary)


def _parse_theme_color(color_str: str) -> QColor:
    if not color_str:
        return QColor(33, 33, 38)
    m = re.match(r"rgba\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", color_str)
    if m:
        return QColor(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    m = re.match(r"rgb\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", color_str)
    if m:
        return QColor(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return QColor(color_str)


def _make_colors_from_context(ctx: dict) -> dict:
    raw = ctx.get("colors", {})
    is_dark = ctx.get("is_dark", True)

    def _qcolor(key: str, fallback_light: str, fallback_dark: str) -> QColor:
        val = raw.get(key, "")
        if val:
            return _parse_theme_color(val)
        return QColor(fallback_dark if is_dark else fallback_light)

    accent = _qcolor("accent", "#2878dc", "#62a0ea")
    border = _qcolor("border", "#cccccc80", "#ffffff1e")
    bg = ctx.get("card_bg", None) or ctx.get("colors", {}).get("card_bg", None)

    return {
        "accent": accent,
        "border": border,
        "text": _qcolor("text_primary", "#000000", "#ffffff"),
        "text_secondary": _qcolor("text_secondary", "#666666", "#aaaaaa"),
        "card_bg": _parse_theme_color(bg) if bg else QColor(33, 33, 38),
        "is_dark": is_dark,
        "font_family": ctx.get("font_family", "Microsoft YaHei"),
        "font_size": ctx.get("font_size", 14),
    }


def _menu_icon(icon: FluentIcon, color: QColor) -> QIcon:
    """按主题文字色给菜单图标着色（菜单项用 icon 代替 emoji）

    FluentIcon 资源是 SVG，``icon(theme=..., color=...)`` 才会重新着色；
    裸 ``icon()`` 取的是固定黑/白版，在亮底或暗底菜单里会看不清。
    """
    try:
        return icon.icon(color=color)
    except Exception:
        return icon.icon()


# ══════════════════════════════════════════════════════════
# 主题对话框辅助
# ══════════════════════════════════════════════════════════


def _styled_message_box(
    colors: dict,
    icon: QMessageBox.Icon,
    title: str,
    text: str,
    buttons: QMessageBox.StandardButtons = QMessageBox.Yes | QMessageBox.No,
    default_button: QMessageBox.StandardButton = QMessageBox.No,
    parent_widget: Optional[QWidget] = None,
) -> int:
    """创建适配主题色的消息框 — 统一 MaskDialogBase 风格"""
    bg = colors.get("card_bg", QColor(33, 33, 38))
    tc = colors.get("text", QColor(255, 255, 255))
    accent = colors.get("accent", QColor(102, 198, 255))
    border = colors.get("border", QColor(61, 61, 61))
    font_size = colors.get("font_size", 14)
    ff = colors.get("font_family", "Microsoft YaHei")
    is_dark = colors.get("is_dark", True)

    hover_bg = bg.lighter(115) if is_dark else bg.darker(110)

    has_yes = bool(buttons & QMessageBox.Yes)
    has_no = bool(buttons & QMessageBox.No)
    has_ok = bool(buttons & QMessageBox.Ok)
    has_cancel = bool(buttons & QMessageBox.Cancel)

    class _Dialog(MaskDialogBase):
        def __init__(self, pw):
            super().__init__(pw)
            self._result = QMessageBox.No
            self._setup()

        def _setup(self):
            self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
            self.setClosableOnMaskClicked(True)
            self.setDraggable(True)
            self.setMaskColor(QColor(0, 0, 0, 76))

            self.widget.setObjectName("fileTreeStyledDialog")
            self.widget.setStyleSheet(
                f"#fileTreeStyledDialog {{"
                f"  background-color: {bg.name()};"
                f"  border: 1px solid {border.name()};"
                f"  border-radius: 8px;"
                f"}}"
            )

            layout = QVBoxLayout(self.widget)
            layout.setContentsMargins(28, 28, 28, 20)
            layout.setSpacing(0)

            title_lb = BodyLabel(title, self.widget)
            title_lb.setWordWrap(True)
            title_lb.setStyleSheet(
                f"color: {tc.name()}; background: transparent; "
                f"font-family: '{ff}'; font-size: {font_size + 2}px; font-weight: bold;"
            )
            layout.addWidget(title_lb)
            layout.addSpacing(6)

            content_lb = BodyLabel(text, self.widget)
            content_lb.setWordWrap(True)
            content_lb.setStyleSheet(
                f"color: {tc.name()}; background: transparent; "
                f"font-family: '{ff}'; font-size: {font_size - 1}px; line-height: 1.6;"
            )
            layout.addWidget(content_lb)
            layout.addStretch()

            btn_layout = QHBoxLayout()
            btn_layout.setSpacing(10)

            def _make_btn(label_text: str, result_code, is_default: bool, is_primary: bool):
                btn = TransparentPushButton(label_text, self.widget)
                btn.setCursor(Qt.PointingHandCursor)
                btn.setFixedHeight(36)
                if is_primary:
                    btn.setStyleSheet(
                        f"QPushButton {{"
                        f"  background-color: {accent.name()}; color: #ffffff;"
                        f"  border: none; border-radius: 8px; padding: 4px 28px;"
                        f"  font-family: '{ff}'; font-size: {font_size - 1}px; font-weight: bold;"
                        f"}}"
                    )
                else:
                    btn.setStyleSheet(
                        f"QPushButton {{"
                        f"  background-color: {bg.name()}; color: {tc.name()};"
                        f"  border: 1px solid {border.name()}; border-radius: 8px;"
                        f"  padding: 4px 28px; font-family: '{ff}';"
                        f"  font-size: {font_size - 1}px;"
                        f"}}"
                        f"QPushButton:hover {{"
                        f"  background-color: {hover_bg.name()}; border-color: {accent.name()};"
                        f"}}"
                    )
                if is_default:
                    btn.setDefault(True)
                    btn.setFocus()
                btn.clicked.connect(lambda: [setattr(self, "_result", result_code), self.close()])
                return btn

            btn_layout.addStretch()

            if has_ok:
                btn_layout.addWidget(_make_btn("确定", QMessageBox.Ok, True, True))
            else:
                if has_no:
                    btn_layout.addWidget(_make_btn("否", QMessageBox.No, default_button == QMessageBox.No, False))
                if has_yes:
                    btn_layout.addWidget(_make_btn("是", QMessageBox.Yes, default_button == QMessageBox.Yes, True))
                if has_cancel:
                    btn_layout.addWidget(
                        _make_btn("取消", QMessageBox.Cancel, default_button == QMessageBox.Cancel, False)
                    )

            layout.addLayout(btn_layout)
            # 布局管理 + 居中：显式 AlignCenter 避免默认无对齐导致
            # widget 高度 = layout 高度（dialog 全屏）；勿 removeWidget——
            # 脱离布局后位置固定 (0,0)，弹窗会钉在主窗口左上角
            self._hBoxLayout.addWidget(self.widget, 0, Qt.AlignCenter)
            # 内容自适应：minSize 保底（420×200），maxSize 防撑爆（620×720）
            self.widget.setMinimumSize(420, 200)
            self.widget.setMaximumSize(620, 720)

    dialog = _Dialog(parent_widget)
    dialog.exec_()
    return dialog._result


# ══════════════════════════════════════════════════════════
# FileTreeCard — 主卡片
# ══════════════════════════════════════════════════════════


class FileTreeCard(QWidget):
    """项目文件树浮动卡片（重构版）"""

    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None, context: Optional[dict] = None):
        super().__init__(parent)
        self._context_provider: Optional[Callable[[], dict]] = None
        # 工作台页形态：宿主 _build_ui_context() 一次性注入；转为 provider 复用
        # 浮动卡的取数链路（project_root / 主题色 / 字体等字段同构）
        # 挂到实例属性：WorkbenchPanel._page_context_incomplete 据此检测
        # 「启动早期宿主未就绪 → 拿到残缺 context」的坏页并触发重建
        self._context = context if isinstance(context, dict) else {}
        # 宿主上下文拉取入口（工作台页由 WorkbenchPanel._make_page_widget 注入）：
        # 构造时的 context 是快照，项目切换后 project_root 会过期，refresh_data
        # 时经此重取最新上下文。
        self._host_context_provider: Optional[Callable[[], dict]] = (
            self._context.get("context_provider") if self._context else None
        )
        if not callable(self._host_context_provider):
            self._host_context_provider = None
        if self._context:

            def _ctx_provider(_ctx=self._context):
                return _ctx

            self._context_provider = _ctx_provider
        self._worker_thread: Optional[QThread] = None
        self._scanner: Optional[_TreeScanner] = None
        # 扫描串行队列：[(dir, dedup_key, on_done)]。任何时刻只一个扫描在跑，
        # 其余排队 —— 不杀线程（quit 打断不了 os.scandir，wait 会阻塞主线程）
        self._scan_queue: List[Tuple[str, str, Callable]] = []
        self._scan_queued: set = set()  # 已排队/在途的 dedup_key（幂等去重）
        self._scan_gen: int = 0  # 树世代：换根时 +1，在途扫描的旧世代回调作废
        self._colors: dict = {}
        self._project_root: str = ""
        self._scrollbar_qss: str = ""  # 主题滚动条样式（预览页文本视图复用）
        self._current_scan_target: str = ""  # 当前扫描目标目录
        # 当前在途扫描的派发上下文 (gen, key, scan_dir, on_done)。串行队列保证
        # 同一时刻至多一个在途扫描，回调经主线程槽读取无需加锁。
        self._current_dispatch: tuple = ()

        # 视图状态缓存：按项目根索引保存展开/选中/滚动（换项目时先存旧根，
        # 切回同根时还原 —— 整树 reset 唯一的兜底）。
        # ★ 仅内存不落盘：跨重启恢复不是需求（用户明确不要），重启后从零展开。
        self._view_state_cache: dict = {}
        # 当前展开目录（原路径，实时维护；换项目时并入 _view_state_cache）
        self._live_expanded: set = set()
        # 视图状态恢复队列（整树 reset 后按路径串行还原展开/选中/滚动）
        self._pending_expand: List[str] = []
        self._pending_selected: List[str] = []
        self._pending_scroll: int = 0
        # 队首目录扫描请求发起时刻（monotonic）：超时跳过防单层拖死整队
        self._pending_expand_since: float = 0.0
        # 主题签名缓存（_apply_latest_theme 产出全等时跳过全部 set，切页高频路径）
        self._theme_signature: Optional[tuple] = None

        # 剪贴板: [(路径, 是否剪切)]
        self._clipboard: List[Tuple[str, bool]] = []

        self._setup_ui()
        self._setup_model()
        self._setup_watcher()
        self._setup_connections()
        self._setup_shortcuts()

        # 销毁前收尾（插件热重载先杀旧实例）
        self.destroyed.connect(self._cleanup_worker)
        self._first_show_done = False

    # ── UI 初始化 ──

    def _setup_ui(self):
        self.setObjectName("file-tree-card")
        self.setAttribute(Qt.WA_StyledBackground, True)

        self._vbox = QVBoxLayout(self)
        self._vbox.setContentsMargins(0, 0, 0, 0)
        self._vbox.setSpacing(0)

        # ── 顶栏 ──
        self._top_bar = QFrame(self)
        self._top_bar.setObjectName("file-tree-top-bar")
        self._top_bar.setFixedHeight(48)
        top_layout = QHBoxLayout(self._top_bar)
        top_layout.setContentsMargins(16, 0, 12, 0)
        top_layout.setSpacing(8)

        self._icon_widget = IconWidget(FluentIcon.FOLDER, self._top_bar)
        self._icon_widget.setFixedSize(20, 20)

        self._title_label = StrongBodyLabel("文件树", self._top_bar)
        self._title_label.setObjectName("file-tree-title")

        self._refresh_btn = TransparentToolButton(FluentIcon.SYNC, self._top_bar)
        self._refresh_btn.setFixedSize(32, 32)
        self._refresh_btn.setToolTip("刷新文件树")

        top_layout.addWidget(self._icon_widget)
        top_layout.addWidget(self._title_label)
        top_layout.addStretch()
        top_layout.addWidget(self._refresh_btn)

        # ── 树控件区域 ──
        self._scroll_area = ScrollArea(self)
        self._scroll_area.setObjectName("file-tree-scroll")
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._tree_view = FileTreeView(self._scroll_area)
        self._tree_view.setObjectName("file-tree-widget")
        self._tree_view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # 滚轮实际落点是内层 QTreeView（外层 ScrollArea 不滚），平滑引擎必须挂在它身上
        SmoothScrollDelegate(self._tree_view)

        # 占位页面（QStackedWidget 切换，避免子控件无布局时左上角残留）
        # ★ 外层 stack：0=树区（含 ScrollArea）/ 1=居中占位页 / 2=文件预览页。
        # 预览页不能放进 ScrollArea：图片自绘缩放与文本自身滚动都会被外层
        # 滚动拦截，且预览页需要自己填满高度。
        self._stack = QStackedWidget(self)
        self._stack.setObjectName("file-tree-stack")
        self._stack.addWidget(self._scroll_area)  # index 0: 文件树视图

        self._placeholder = QLabel("正在加载文件树...")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._ph_page = QWidget()
        _ph_layout = QVBoxLayout(self._ph_page)
        _ph_layout.setAlignment(Qt.AlignCenter)
        _ph_layout.addWidget(self._placeholder)
        self._stack.addWidget(self._ph_page)  # index 1: 居中占位页

        self._preview_page = FilePreviewPage(self)
        self._preview_page.back_requested.connect(self._close_preview)
        self._stack.addWidget(self._preview_page)  # index 2: 文件预览页
        # 初始显示占位页，等待扫描完成
        self._stack.setCurrentIndex(1)

        self._scroll_area.setWidget(self._tree_view)
        self._vbox.addWidget(self._top_bar)
        self._vbox.addWidget(self._stack, 1)

    def _setup_model(self):
        """创建 Model → Proxy → View 链路"""
        self._source_model = FileTreeModel(self)
        self._proxy_model = FileTreeFilterProxy(self)
        self._proxy_model.setSourceModel(self._source_model)
        self._tree_view.setModel(self._proxy_model)

        # RenameDelegate
        self._rename_delegate = RenameDelegate(self._tree_view)
        self._tree_view.setItemDelegateForColumn(0, self._rename_delegate)

    def _setup_watcher(self):
        self._watcher = _DirWatcher(self)
        self._watcher.dir_changed.connect(self._on_dir_changed_externally)

    def _setup_connections(self):
        # 顶栏按钮
        self._refresh_btn.clicked.connect(self._on_refresh)

        # Model 信号
        self._source_model.need_scan.connect(self._on_lazy_scan)

        # View 信号
        self._tree_view.delete_requested.connect(self._on_delete_requested)
        self._tree_view.move_requested.connect(self._on_move_requested)
        self._tree_view.copy_requested.connect(self._on_copy_drop_requested)
        self._tree_view.double_clicked_file.connect(self._on_file_activated)
        self._tree_view.enter_expand_requested.connect(self._on_enter_expand)
        self._tree_view.context_menu_requested.connect(self._on_context_menu)
        self._tree_view.clipboard_copy.connect(self.copy_to_clipboard)
        self._tree_view.clipboard_cut.connect(self.cut_to_clipboard)
        self._tree_view.clipboard_paste.connect(self.paste_from_clipboard)

        # 目录展开/折叠 — 通过 expanded/collapsed 信号代理
        self._tree_view.expanded.connect(self._on_proxy_expanded)
        self._tree_view.collapsed.connect(self._on_proxy_collapsed)

        # RenameDelegate
        self._rename_delegate.rename_requested.connect(self._on_rename_file)

    def _setup_shortcuts(self):
        """快捷键已通过 FileTreeView 信号处理（clipboard_copy/cut/paste）"""
        pass

    # ── 公开接口 ──

    def set_context_provider(self, provider: Callable[[], dict]):
        self._context_provider = provider

    def set_context(self, context: dict):
        """工作台页形态：宿主补注 UI context（推模型，浮动卡契约兼容）

        context 到达即更新取数链并重载目录树（修「启动早期挂载时宿主未就绪、
        project_root 恒空、一直显示加载中」的问题）。
        """
        if not isinstance(context, dict) or not context:
            return
        self._context = context
        self._context_provider = lambda: context
        if self.isVisible():
            self.show_card()

    def show_card(self):
        self._apply_latest_theme()
        self._apply_plugin_icon()
        self._async_load_tree()
        self.setVisible(True)

    # ── 项目 / 工作目录联动（UI 插件可选协议） ──

    def on_project_changed(self, project: str = "", workdir: str = "", window_id: str = "") -> None:
        """项目 / 工作目录变更（宿主经 EV_PROJECT_CHANGED 派发）：立即重载目录树"""
        self.refresh_data()

    # ── 视图状态快照 / 恢复（整树 reset 的兜底） ──

    def _expanded_source_paths(self) -> List[str]:
        """当前展开中的目录 source 路径（BFS，父在子前 —— 恢复时须按此序串行展开）"""
        paths: List[str] = []
        stack = [QModelIndex()]
        while stack:
            parent = stack.pop(0)
            for row in range(self._source_model.rowCount(parent)):
                src_idx = self._source_model.index(row, 0, parent)
                if not src_idx.isValid() or not src_idx.data(FileTreeModel.COL_IS_DIR):
                    continue
                proxy_idx = self._proxy_model.mapFromSource(src_idx)
                if not proxy_idx.isValid() or not self._tree_view.isExpanded(proxy_idx):
                    continue
                path = src_idx.data(FileTreeModel.COL_PATH)
                if path:
                    paths.append(path)
                stack.append(src_idx)
        return paths

    def _snapshot_view_state(self) -> dict:
        """记录展开 / 选中 / 滚动位置 —— 必须在 ``beginResetModel`` **之前**调用

        ``beginResetModel`` 会连带清空 selectionModel 的选中项，reset 后再取只剩空集。
        """
        return {
            "expanded": self._expanded_source_paths(),
            "selected": self._tree_view.selected_source_paths(),
            "scroll": self._tree_view.verticalScrollBar().value(),
        }

    # ── 视图状态缓存（仅内存：会话内换项目 A→B→A 恢复，不跨重启） ──

    def _stash_view_state(self, root: str) -> None:
        """把当前视图状态存进按项目根索引的缓存（换项目前存旧根）"""
        if not root:
            return
        key = os.path.normcase(os.path.normpath(root))
        self._view_state_cache[key] = self._snapshot_view_state()
        if len(self._view_state_cache) > 32:  # 上限保护：丢掉最早的一项
            self._view_state_cache.pop(next(iter(self._view_state_cache)))

    def _restore_view_state(self, state: dict) -> None:
        """按快照还原视图状态：串行展开 → 选中 → 滚动"""
        self._pending_expand: List[str] = list(state.get("expanded") or [])
        self._pending_selected: List[str] = list(state.get("selected") or [])
        self._pending_scroll: int = int(state.get("scroll") or 0)
        self._pending_expand_since = 0.0
        self._process_expand_queue()

    def _process_expand_queue(self) -> None:
        """推进状态恢复队列：逐个展开，未加载目录先请求扫描

        ★ 完全串行：一次只处理队首一个目录，数据未就绪就 ``return``，由扫描
        回调回来继续 —— 下载式推进避免一次性连带多次扫描。
        ★ 不依赖 ``expand()`` 内部触发 ``fetchMore``：视图不可见 / 布局首帧时
        Qt 可能推迟该调用，展开时机不可控；这里显式请求扫描，展开必定紧跟数据。
        """
        while self._pending_expand:
            path = self._pending_expand[0]
            node = self._source_model.find_node(path)
            if node is None or not node.is_dir:
                logger.debug(f"[FileTree] 恢复队列跳过(节点不存在): {path}")
                self._pending_expand.pop(0)
                self._pending_expand_since = 0.0
                continue  # 路径已不在（换了项目 / 已被删除）
            if self._source_model.node_is_failed(path):
                logger.debug(f"[FileTree] 恢复队列跳过(已熔断): {path}")
                self._pending_expand.pop(0)
                self._pending_expand_since = 0.0
                continue  # 熔断：不反复重试（用户点刷新可恢复）
            src_idx = self._source_model._index_for_node(node)
            proxy_idx = self._proxy_model.mapFromSource(src_idx)
            if not proxy_idx.isValid():
                logger.debug(f"[FileTree] 恢复队列跳过(proxy 无效): {path}")
                self._pending_expand.pop(0)
                self._pending_expand_since = 0.0
                continue  # 父级尚未加载，当前不可达
            if not node.loaded:
                # 请求扫描（入队即占位 loading，幂等）；回调回来再推进。
                # ★ 超时熔断：扫描回调迟到/丢失（目录被占用、QThread 异常）时
                # 队首会永久堵死整条恢复队列，后面目录全部无法展开 —— 等待超
                # 过阈值就跳过该项继续，恢复进度不为单个异常目录买单。
                now = time.monotonic()
                if self._pending_expand_since and now - self._pending_expand_since > _EXPAND_WAIT_TIMEOUT_S:
                    logger.warning(f"[FileTree] 恢复队列等待扫描超时，跳过: {path}")
                    self._pending_expand.pop(0)
                    self._pending_expand_since = 0.0
                    self._source_model.clear_loading(path)
                    continue
                if not self._pending_expand_since:
                    self._pending_expand_since = now
                self._on_lazy_scan(path)
                return
            self._pending_expand.pop(0)
            self._pending_expand_since = 0.0
            if not self._tree_view.isExpanded(proxy_idx):
                t0 = time.perf_counter()
                self._tree_view.expand(proxy_idx)
                dt = (time.perf_counter() - t0) * 1000
                if dt > 50:
                    logger.warning(f"[FileTree] 恢复展开慢: {path} expand {dt:.0f}ms")
        self._finish_state_restore()

    def _finish_state_restore(self) -> None:
        """还原选中项与滚动位置（展开队列排空后调用；无恢复任务时为零操作）"""
        selected = self._pending_selected
        self._pending_selected = []
        sm = self._tree_view.selectionModel()
        if selected and sm is not None:
            sm.clearSelection()
            first = QModelIndex()
            for path in selected:
                node = self._source_model.find_node(path)
                if node is None:
                    continue
                proxy_idx = self._proxy_model.mapFromSource(self._source_model._index_for_node(node))
                if not proxy_idx.isValid():
                    continue
                sm.select(proxy_idx, QItemSelectionModel.Select | QItemSelectionModel.Rows)
                if not first.isValid():
                    first = proxy_idx
            if first.isValid():
                self._tree_view.setCurrentIndex(first)
        scroll = self._pending_scroll
        self._pending_scroll = 0
        if scroll > 0:
            # 延后一帧：展开后 scrollBar.maximum 需等布局重算，立即 setValue 会被旧上限截断
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self._restore_scroll(scroll))
            timer.start(0)

    def _restore_scroll(self, scroll: int) -> None:
        """还原滚动位置（钳到当前上限，避免越界跳顶）"""
        if not self._tree_view.isVisible():
            return
        bar = self._tree_view.verticalScrollBar()
        if bar is not None:
            bar.setValue(min(scroll, bar.maximum()))

    def refresh_data(self) -> None:
        """工作台页刷新协议入口（宿主切页 / 项目联动共用）

        重取宿主上下文 → 应用主题与 project_root → 重载目录树。切走再切回
        该页也会走这里，修掉「只加载一次」的旧行为。
        """
        self._refresh_host_context()
        self._apply_latest_theme()
        self._async_load_tree()

    def _refresh_host_context(self) -> None:
        """向宿主拉最新上下文（拿不到时保持旧 context，不抛）"""
        provider = self._host_context_provider
        if not callable(provider):
            return
        try:
            ctx = provider()
        except Exception:
            return
        if isinstance(ctx, dict) and ctx:
            self._context = ctx
            self._context_provider = lambda _ctx=ctx: _ctx

    def _apply_plugin_icon(self):
        if self._context_provider is None or self._icon_widget is None:
            return
        try:
            ctx = self._context_provider()
            icon_info = ctx.get("plugin_icon", {})
            theme = "dark" if isDarkTheme() else "light"
            icon_path = icon_info.get(theme, "")
            if icon_path:
                self._icon_widget.setIcon(QIcon(icon_path))
        except Exception:
            pass

    # ── 主题色 ──

    def _apply_latest_theme(self):
        if self._context_provider is None:
            return
        try:
            ctx = self._context_provider()
        except Exception:
            return

        self._colors = _make_colors_from_context(ctx)
        self._project_root = ctx.get("project_root", "")
        self._tree_view.project_root = self._project_root
        self._watcher.set_root(self._project_root)

        tc = _ctx_text_color(ctx)
        border_c = self._colors.get("border", QColor(255, 255, 255, 30))
        font_family = ctx.get("font_family", "Microsoft YaHei")
        font_size = ctx.get("font_size", 14)

        is_dark = ctx.get("is_dark", True)
        hover_bg = "rgba(255,255,255,0.08)" if is_dark else "rgba(0,0,0,0.06)"

        tc_hex = self._colors["text"].name()
        try:
            from app.plugins.sdk import get_unified_scrollbar_style

            scrollbar_style = get_unified_scrollbar_style()
        except Exception:
            scrollbar_style = ""
        self._scrollbar_qss = scrollbar_style

        title_style = (
            f"color: {tc}; background: transparent; "
            f"font-family: '{font_family}'; font-size: {font_size}px; font-weight: bold;"
        )
        top_bar_style = (
            f"#file-tree-top-bar {{"
            f"  background: transparent;"
            # ★ QSS/QColor 的 8 位 hex 是 #AARRGGBB（实测 #ffffff1e → rgb(255,255,30)），
            # 直接 name()+alpha 拼接会把 alpha 写进蓝通道 → 分隔线变黄/洋红亮线。
            # 主题 border 本身不透明（如 #bcc7d6），压到 ~35% 才不抢眼。
            f"  border-bottom: 1px solid "
            f"rgba({border_c.red()},{border_c.green()},{border_c.blue()},90);"
            f"}}"
        )
        accent_tint = (
            f"rgba({self._colors['accent'].red()},{self._colors['accent'].green()},"
            f"{self._colors['accent'].blue()},64)"
        )
        tree_style = (
            f"#file-tree-widget {{"
            f"  background: transparent; border: none;"
            f"  color: {tc_hex}; font-size: {font_size}px;"
            f"}}"
            f"#file-tree-widget::item:selected {{"
            f"  background: {accent_tint}; color: {tc_hex};"
            f"}}"
            f"#file-tree-widget::item:hover {{"
            f"  background: {hover_bg};"
            f"}}"
            f"{scrollbar_style}"
        )
        ph_color = "rgba(255,255,255,0.4)" if is_dark else "rgba(0,0,0,0.4)"
        placeholder_style = (
            f"color: {ph_color}; background: transparent; font-family: '{font_family}'; font-size: {font_size}px;"
        )

        # ★ 签名缓存：产出（全部样式串 + 字体）与上次全等时跳过所有 set。
        # 切工作台页 / 项目联动高频走这里，对整棵 QTreeView 重复
        # setStyleSheet + setFont 会触发整树 unpolish/polish + 全量重绘
        # （树 2000+ 行时单次 40-130ms，实测），而主题 / 字体 / 颜色未变时
        # 产出必然不变 —— 跳过后切页路径零成本。
        signature = (title_style, top_bar_style, tree_style, placeholder_style, font_family, font_size)
        if signature == self._theme_signature:
            return
        self._theme_signature = signature

        self._title_label.setStyleSheet(title_style)
        self._top_bar.setStyleSheet(top_bar_style)
        self._tree_view.setStyleSheet(tree_style)

        self._scroll_area.setStyleSheet("#file-tree-scroll { background: transparent; border: none;}")
        self._scroll_area.viewport().setStyleSheet("background: transparent; border: none;")

        self._tree_view.setFont(QFont(font_family, font_size))

        # 占位页主题色
        self._placeholder.setStyleSheet(placeholder_style)
        self._ph_page.setStyleSheet("background: transparent;")
        self._stack.setStyleSheet("background: transparent;")

        # 通知 delegate 更新主题色
        self._rename_delegate.set_colors(self._colors)
        self._preview_page.set_colors(
            self._colors,
            font_family=font_family,
            font_size=font_size,
            scrollbar_qss=scrollbar_style,
        )

    # ── 异步加载 ──

    def _async_load_tree(self, target_dir: Optional[str] = None):
        scan_dir = target_dir or self._project_root
        if not scan_dir or not os.path.isdir(scan_dir):
            self._show_error("项目目录不存在，请先设置工作目录")
            return

        if _same_path(scan_dir, self._project_root):
            self._request_scan(scan_dir, self._on_root_scan_done, urgent=True)
        else:
            self._request_scan(scan_dir, lambda entries, _d=scan_dir: self._on_child_scan_done(entries, _d))

    def _request_scan(self, scan_dir: str, on_done, *, urgent: bool = False) -> bool:
        """请求扫描（串行队列 + 幂等去重），返回是否新入队

        ★ 为什么不“杀掉上一个扫描再起新的”（旧实现）：``QThread.quit()`` 只停
          事件循环，**打断不了工作线程里正在执行的 ``os.scandir``**，``wait(3000)``
          会把主线程真阻塞住。用户连续展开 N 个目录 = N 次阻塞；更糟的是被杀
          目录的 loading 标记被清后回到「已展开且 canFetchMore」状态，Qt 下一轮
          布局再次请求它 —— 与扫描请求互相踩踏形成自激循环。实测 6 个目录触发
          223 次扫描、主线程累计阻塞 25s（卡死）。
        ★ 现改为串行队列：任何时刻只一个扫描在执行，其余排队等待；入队即占位
          loading，Qt 既不会重复请求，也不会有目录永远卡在空态。

        Args:
            urgent: 根目录扫描（树换血）—— 丢弃排队中的子目录扫描、世代 +1 让
                在途扫描的回调作废；但**不杀线程**（结果被丢弃即可）。
        """
        key = os.path.normcase(os.path.normpath(scan_dir))
        if urgent:
            self._scan_queue = []
            self._scan_queued.clear()
            self._source_model.clear_all_loading()
            self._source_model.clear_failed()  # 重扫重置熔断（点刷新可恢复失败目录）
            self._scan_gen += 1
        elif key in self._scan_queued:
            return False  # 已排队 / 在途：幂等，不重复排
        self._scan_queued.add(key)
        self._source_model.mark_loading(scan_dir)  # 入队即占位，覆盖整个生命周期
        item = (scan_dir, key, on_done)
        if urgent:
            self._scan_queue.insert(0, item)
        else:
            self._scan_queue.append(item)
        self._pump_scan_queue()
        return True

    def _pump_scan_queue(self) -> None:
        """推进扫描队列：空闲则取队首开扫（任何时刻至多一个扫描在跑）

        ★ 信号连接纪律（此前卡死根因，勿改回 lambda）：必须连接 **bound
          method**，PyQt 按 receiver 的线程亲和决定执行线程 ——
          - ``started → scanner.scan``：receiver=scanner（worker 亲和）→ 扫描
            真正落在 worker 线程；连 lambda 时会被 queued 回主线程，扫描全程
            卡 UI。
          - ``scanner.finished/error → card 槽``：receiver=card（主线程亲和）→
            结果排队回主线程处理，model 操作不越线程。
        ★ 生命周期：结果槽里 ``quit()`` 收线程 → ``_on_thread_finished``
          （线程真 finished 后）才置 None。直接置 None 会 GC 掉仍在运行的
          QThread，Qt5 ``~QThread`` 直接 qFatal（0xC0000409 整窗死）。
        """
        if self._worker_thread is not None or not self._scan_queue:
            return
        scan_dir, key, on_done = self._scan_queue.pop(0)
        gen = self._scan_gen
        self._current_scan_target = scan_dir
        self._current_dispatch = (gen, key, scan_dir, on_done)

        self._worker_thread = QThread()
        self._scanner = _TreeScanner(scan_dir)
        self._scanner.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(self._scanner.scan)
        self._scanner.finished.connect(self._on_scan_finished)
        self._scanner.error.connect(self._on_scan_error)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.finished.connect(self._on_thread_finished)

        self._worker_thread.start()

    def _on_scan_finished(self, entries: List[_DirEntry]):
        """扫描成功（主线程槽，scanner.finished queued 回）：落地结果 + 收线程"""
        gen, key, scan_dir, on_done = self._current_dispatch
        # key 无论世代都要摘：漏摘会让该目录永久留在 _scan_queued，此后
        # 所有请求都被当成重复而丢弃（该目录再也加载不出来）
        self._scan_queued.discard(key)
        if gen == self._scan_gen:
            self._source_model.clear_loading(scan_dir)
            on_done(entries)
            # 落地后仍未加载（节点丢失 / 目录不可读）→ 熔断，避免反反复复重扫
            if not _same_path(scan_dir, self._project_root):
                node = self._source_model.find_node(scan_dir)
                if node is not None and node.is_dir and not node.loaded:
                    self._source_model.mark_failed(scan_dir)
        if self._worker_thread is not None:
            self._worker_thread.quit()  # 异步退出 → finished → _on_thread_finished 收尾

    def _on_scan_error(self, error_msg: str):
        """扫描失败（主线程槽）：熔断 + 收线程（旧世代丢弃）"""
        gen, key, scan_dir, _on_done = self._current_dispatch
        # key 无论世代都要摘（同 _on_scan_finished：漏摘会让目录永久卡死）
        self._scan_queued.discard(key)
        if gen == self._scan_gen:
            self._source_model.clear_loading(scan_dir)
            self._source_model.mark_failed(scan_dir)  # 熔断：不再自动重试
            logger.error(f"[FileTree] 扫描失败: {error_msg}")
            if _same_path(scan_dir, self._project_root):
                self._show_error("扫描目录时出错")  # 仅根目录失败才报错（子目录失败不空整树）
        if self._worker_thread is not None:
            self._worker_thread.quit()

    def _on_thread_finished(self):
        """worker 线程真 finished 后的收尾（主线程槽）：此刻置 None 才不会
        GC 到仍在运行的 QThread（qFatal 0xC0000409 卡死根因），随后推进队列
        与展开恢复队列。"""
        self._worker_thread = None
        self._scanner = None
        self._pump_scan_queue()
        self._process_expand_queue()  # 队列推进：下一个目录入队 / 收尾

    def _cleanup_worker(self):
        """停止扫描并清空队列（仅供销毁 / 关闭路径）"""
        self._scan_queue = []
        self._scan_queued.clear()
        interrupted = ""
        if self._worker_thread is not None:
            interrupted = self._current_scan_target
            try:
                self._worker_thread.quit()
                self._worker_thread.wait(3000)
            except RuntimeError:
                pass
            self._worker_thread = None
            self._scanner = None
        if interrupted:
            self._source_model.clear_loading(interrupted)

    def _on_root_scan_done(self, entries: List[_DirEntry]):
        """项目根扫描完成：落地条目 + 重挂根目录监听"""
        scan_dir = self._project_root
        self._apply_root_entries(scan_dir, entries)
        if self._project_root and os.path.isdir(self._project_root):
            self._watcher.add_path(self._project_root)

    def _on_child_scan_done(self, entries: List[_DirEntry], dir_path: str):
        """指定子目录扫描完成（``_async_load_tree(target)`` 路径）"""
        self._source_model.populate_children(dir_path, entries)

    def _apply_root_entries(self, scan_dir: str, entries: List[_DirEntry]) -> None:
        """项目根扫描结果落地：同根走增量，异根才整体重建

        ★ 旧实现无条件 ``set_project``（``beginResetModel``）。切工作台页签、
        切对话标签、点刷新、项目上下文同步都经此重扫根目录，QTreeView 随之
        丢弃全部展开 / 选中 / 滚动状态 —— 用户展开的层级被「刷没」。同一项目根
        下目录内容只增删少量条目，``refresh_root`` 已实现增量同步（不 reset、
        未变节点保持原引用），状态天然保留。
        ★ 异根（真换项目）：reset 不可免，走视图状态缓存 —— 离开旧根先存，
        回到该根时还原，故 A→B→A 来回切不丢用户展开的层级。
        """
        model_root = self._source_model.project_root()
        if _same_path(scan_dir, model_root):
            self._source_model.refresh_root(entries)
        else:
            self._live_expanded.clear()  # 旧根的实时展开路径不写进新根条目
            self._stash_view_state(model_root)  # 必须先存：reset 后旧状态已不可取
            self._source_model.set_project(scan_dir, entries)
            cached = self._view_state_cache.get(os.path.normcase(os.path.normpath(scan_dir)))
            if cached:
                self._restore_view_state(cached)
        if self._stack.currentIndex() == _PAGE_PREVIEW:
            return  # 预览态不抢页面（换项目时保留当前阅读内容）
        self._stack.setCurrentIndex(0 if entries else 1)
        if not entries:
            self._placeholder.setText("项目目录为空")

    def _show_error(self, message: str):
        if self._stack.currentIndex() == _PAGE_PREVIEW:
            return  # 预览态不抢页面
        self._placeholder.setText(f"⚠️ {message}")
        self._stack.setCurrentIndex(1)

    # ── 懒加载（展开触发扫描） ──

    def _on_lazy_scan(self, dir_path: str):
        """Model 请求扫描某目录的子节点（走串行队列，幂等去重）"""
        self._request_scan(dir_path, lambda entries, _d=dir_path: self._on_lazy_scan_done(entries, _d))

    def _on_lazy_scan_done(self, entries: List[_DirEntry], dir_path: str):
        t0 = time.perf_counter()
        self._source_model.populate_children(dir_path, entries)
        if os.path.isdir(dir_path):
            self._watcher.add_path(dir_path)
        dt = (time.perf_counter() - t0) * 1000
        if dt > 50:
            logger.warning(f"[FileTree] 子目录落地慢: {dir_path} populate+watch {dt:.0f}ms ({len(entries)} 条)")
        self._process_expand_queue()  # 恢复队列：本层加载完成，展开下一层

    # ── 展开/折叠 ──

    def _on_proxy_expanded(self, proxy_idx: QModelIndex):
        src_idx = self._proxy_model.mapToSource(proxy_idx)
        if not src_idx.isValid():
            return
        dir_path = src_idx.data(FileTreeModel.COL_PATH)
        if not dir_path or not os.path.isdir(dir_path):
            return
        # fetchMore 已在 Qt 层自动触发扫描，这里只管理文件监听与展开态落盘
        if not self._source_model.node_is_loading(dir_path) and not self._source_model.node_is_loaded(src_idx):
            logger.debug(f"[FileTree] 展开未加载目录(应由 fetchMore 触发): {dir_path}")
        # ★ add_path 挪出 expanded 同步信号链：QFileSystemWatcher.addPath 在
        # Windows 上对被占用的目录（IDE/其他实例持有句柄、杀毒实时扫描）可能
        # 长时间阻塞，同步执行会把 expand() 整体冻结（实测恢复展开状态时出现
        # 10s 级卡顿）。延迟到下一轮事件循环，目录监听晚一拍无感知。
        QTimer.singleShot(0, lambda p=dir_path: self._watcher.add_path(p))
        self._live_expanded.add(dir_path)

    def _on_proxy_collapsed(self, proxy_idx: QModelIndex):
        src_idx = self._proxy_model.mapToSource(proxy_idx)
        if not src_idx.isValid():
            return
        dir_path = src_idx.data(FileTreeModel.COL_PATH)
        if dir_path:
            self._watcher.remove_path(dir_path)
            self._live_expanded.discard(dir_path)

    # ── Enter 展开 ──

    def _on_enter_expand(self, dir_path: str):
        node = self._source_model.find_node(dir_path)
        if not node:
            return
        src_idx = self._source_model._index_for_node(node)
        if not src_idx.isValid():
            return
        proxy_idx = self._proxy_model.mapFromSource(src_idx)
        if proxy_idx.isValid():
            if self._tree_view.isExpanded(proxy_idx):
                self._tree_view.collapse(proxy_idx)
            else:
                self._tree_view.expand(proxy_idx)

    # ── 目录变更监听 ──

    def _on_dir_changed_externally(self, dir_path: str):
        logger.debug(f"[FileTree] 外部变更: {dir_path}")
        # 根目录自身在 model 中无节点（_root_entries 只含根下条目），需单独处理
        is_root = os.path.normpath(dir_path) == os.path.normpath(self._project_root)
        if not is_root:
            node = self._source_model.find_node(dir_path)
            if node is None or not node.loaded:
                return
        # 异步重新扫描（走串行队列）
        self._request_scan(dir_path, lambda entries, _d=dir_path: self._on_watcher_reload(entries, _d))

    def _on_watcher_reload(self, entries: List[_DirEntry], dir_path: str):
        if _same_path(dir_path, self._project_root):
            self._source_model.refresh_root(entries)
            if self._stack.currentIndex() == _PAGE_PREVIEW:
                return  # 预览态不抢页面
            if entries:
                self._stack.setCurrentIndex(0)
            else:
                self._placeholder.setText("项目目录为空")
                self._stack.setCurrentIndex(1)
        else:
            self._source_model.refresh_children(dir_path, entries)

    # ── 剪贴板操作 ──

    def copy_to_clipboard(self):
        """Ctrl+C — 复制到内部剪贴板"""
        paths = self._tree_view.selected_source_paths()
        if not paths:
            return
        self._clipboard = [(p, False) for p in paths]
        logger.debug(f"[FileTree] 复制 {len(paths)} 项到剪贴板")

    def cut_to_clipboard(self):
        """Ctrl+X — 剪切到内部剪贴板"""
        paths = self._tree_view.selected_source_paths()
        if not paths:
            return
        # 排除项目根目录
        valid = [p for p in paths if os.path.normpath(p) != os.path.normpath(self._project_root)]
        if not valid:
            return
        self._clipboard = [(p, True) for p in valid]
        logger.debug(f"[FileTree] 剪切 {len(valid)} 项到剪贴板")

    def paste_from_clipboard(self):
        """Ctrl+V — 粘贴剪贴板内容"""
        if not self._clipboard:
            return

        # 找到目标目录
        idxs = self._tree_view.selectionModel().selectedRows()
        if idxs:
            src_idx = self._proxy_model.mapToSource(idxs[0])
            target_path = src_idx.data(FileTreeModel.COL_PATH)
            target_is_dir = src_idx.data(FileTreeModel.COL_IS_DIR)
            if target_is_dir:
                dest = target_path
            else:
                dest = os.path.dirname(target_path)
        else:
            dest = self._project_root

        if not dest or not os.path.isdir(dest):
            return

        # 执行操作
        for src_path, is_cut in self._clipboard:
            if not os.path.exists(src_path):
                logger.warning(f"[FileTree] 剪贴板项不存在: {src_path}")
                continue
            name = os.path.basename(src_path)
            dst = os.path.join(dest, name)
            if os.path.normpath(src_path) == os.path.normpath(dst):
                continue
            try:
                if is_cut:
                    if os.path.exists(dst):
                        reply = _styled_message_box(
                            self._colors,
                            QMessageBox.Warning,
                            "确认覆盖",
                            f"「{name}」已存在，确定覆盖？",
                            parent_widget=self.window(),
                        )
                        if reply != QMessageBox.Yes:
                            continue
                        if os.path.isdir(dst):
                            shutil.rmtree(dst)
                        else:
                            os.remove(dst)
                    shutil.move(src_path, dst)
                    self._source_model.remove_node(src_path)
                    logger.info(f"[FileTree] 移动: {src_path} → {dst}")
                else:
                    if os.path.exists(dst):
                        base, ext = os.path.splitext(name)
                        counter = 1
                        while os.path.exists(dst):
                            dst = os.path.join(dest, f"{base} ({counter}){ext}")
                            counter += 1
                    if os.path.isdir(src_path):
                        shutil.copytree(src_path, dst)
                    else:
                        shutil.copy2(src_path, dst)
                    logger.info(f"[FileTree] 复制: {src_path} → {dst}")

                # 更新目标目录
                self._refresh_dir_in_model(dest)
            except Exception as e:
                logger.error(f"[FileTree] 粘贴失败 {src_path}: {e}")
                _styled_message_box(
                    self._colors,
                    QMessageBox.Critical,
                    "操作失败",
                    f"无法粘贴「{name}」:\n{e}",
                    QMessageBox.Ok,
                    QMessageBox.Ok,
                    parent_widget=self.window(),
                )

        # 剪切后清空
        if self._clipboard and self._clipboard[0][1]:
            self._clipboard.clear()

    # ── 删除 ──

    def _on_delete_requested(self, paths: List[str]):
        deletable = [p for p in paths if os.path.normpath(p) != os.path.normpath(self._project_root)]
        if not deletable:
            _styled_message_box(
                self._colors,
                QMessageBox.Information,
                "提示",
                "不能删除项目根目录",
                QMessageBox.Ok,
                QMessageBox.Ok,
                parent_widget=self.window(),
            )
            return

        if len(deletable) == 1:
            msg = f"确定要永久删除「{os.path.basename(deletable[0])}」？\n\n路径: {deletable[0]}\n\n⚠️ 此操作不可撤销！"
        else:
            names = "\n".join(f"• {os.path.basename(p)}" for p in deletable)
            msg = f"确定要永久删除以下 {len(deletable)} 个项目？\n\n{names}\n\n⚠️ 此操作不可撤销！"

        reply = _styled_message_box(
            self._colors,
            QMessageBox.Warning,
            "确认永久删除",
            msg,
            parent_widget=self.window(),
        )
        if reply != QMessageBox.Yes:
            return

        deleted = 0
        for path in deletable:
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                self._source_model.remove_node(path)
                deleted += 1
                logger.info(f"[FileTree] 已删除: {path}")
            except Exception as e:
                logger.error(f"[FileTree] 删除失败: {path}: {e}")
                _styled_message_box(
                    self._colors,
                    QMessageBox.Critical,
                    "删除失败",
                    f"无法删除「{os.path.basename(path)}」:\n{e}",
                    QMessageBox.Ok,
                    QMessageBox.Ok,
                    parent_widget=self.window(),
                )

    # ── 拖拽移动/复制 ──

    def _on_move_requested(self, source_paths: List[str], dest_dir: str):
        names = "\n".join(os.path.basename(p) for p in source_paths)
        target_name = os.path.basename(dest_dir) or dest_dir
        reply = _styled_message_box(
            self._colors,
            QMessageBox.Question,
            "确认移动",
            f"确定要将以下项目移动到「{target_name}」？\n\n{names}",
            parent_widget=self.window(),
        )
        if reply != QMessageBox.Yes:
            return

        for src in source_paths:
            dst = os.path.join(dest_dir, os.path.basename(src))
            if os.path.exists(dst):
                logger.warning(f"[FileTree] 目标已存在，跳过: {dst}")
                continue
            try:
                shutil.move(src, dst)
                self._source_model.remove_node(src)
                logger.info(f"[FileTree] 移动: {src} → {dst}")
            except Exception as e:
                logger.error(f"[FileTree] 移动失败 {src}: {e}")
        self._refresh_dir_in_model(dest_dir)

    def _on_copy_drop_requested(self, source_paths: List[str], dest_dir: str):
        """拖拽复制 — 外部/内部统一确认"""
        names = "\n".join(os.path.basename(p) for p in source_paths)
        target_name = os.path.basename(dest_dir) or dest_dir
        reply = _styled_message_box(
            self._colors,
            QMessageBox.Question,
            "确认复制",
            f"确定要将以下项目复制到「{target_name}」？\n\n{names}",
            parent_widget=self.window(),
        )
        if reply != QMessageBox.Yes:
            return

        for src in source_paths:
            name = os.path.basename(src)
            dst = os.path.join(dest_dir, name)
            try:
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
                logger.info(f"[FileTree] 拖拽复制: {src} → {dst}")
            except Exception as e:
                logger.error(f"[FileTree] 拖拽复制失败 {src}: {e}")
        self._refresh_dir_in_model(dest_dir)

    # ── 重命名 ──

    def _on_rename_file(self, old_path: str, new_name: str):
        if not os.path.exists(old_path):
            logger.warning(f"[FileTree] 重命名失败，路径不存在: {old_path}")
            return

        new_path = os.path.join(os.path.dirname(old_path), new_name)
        if os.path.exists(new_path):
            _styled_message_box(
                self._colors,
                QMessageBox.Warning,
                "重命名失败",
                f"「{new_name}」已存在",
                QMessageBox.Ok,
                QMessageBox.Ok,
                parent_widget=self.window(),
            )
            return

        try:
            os.rename(old_path, new_path)
            result = self._source_model.rename_node(old_path, new_name)
            if result:
                logger.info(f"[FileTree] 重命名: {old_path} → {result}")
        except Exception as e:
            logger.error(f"[FileTree] 重命名失败: {e}")
            _styled_message_box(
                self._colors,
                QMessageBox.Critical,
                "重命名失败",
                str(e),
                QMessageBox.Ok,
                QMessageBox.Ok,
                parent_widget=self.window(),
            )

    # ── 新建文件/文件夹 ──

    def _create_new_file(self, parent_path: str):
        name, ok = QInputDialog.getText(
            self.window(),
            "新建文件",
            "请输入文件名:",
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        if "/" in name or "\\" in name:
            _styled_message_box(
                self._colors,
                QMessageBox.Warning,
                "无效名称",
                "文件名不能包含路径分隔符",
                QMessageBox.Ok,
                QMessageBox.Ok,
                parent_widget=self.window(),
            )
            return

        new_path = os.path.join(parent_path, name)
        if os.path.exists(new_path):
            _styled_message_box(
                self._colors,
                QMessageBox.Warning,
                "创建失败",
                f"「{name}」已存在",
                QMessageBox.Ok,
                QMessageBox.Ok,
                parent_widget=self.window(),
            )
            return

        try:
            Path(new_path).touch()
            logger.info(f"[FileTree] 新建文件: {new_path}")
            self._refresh_dir_in_model(parent_path)
        except Exception as e:
            logger.error(f"[FileTree] 新建文件失败: {e}")

    def _create_new_dir(self, parent_path: str):
        name, ok = QInputDialog.getText(
            self.window(),
            "新建文件夹",
            "请输入文件夹名:",
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        if "/" in name or "\\" in name:
            _styled_message_box(
                self._colors,
                QMessageBox.Warning,
                "无效名称",
                "文件夹名不能包含路径分隔符",
                QMessageBox.Ok,
                QMessageBox.Ok,
                parent_widget=self.window(),
            )
            return

        new_path = os.path.join(parent_path, name)
        if os.path.exists(new_path):
            _styled_message_box(
                self._colors,
                QMessageBox.Warning,
                "创建失败",
                f"「{name}」已存在",
                QMessageBox.Ok,
                QMessageBox.Ok,
                parent_widget=self.window(),
            )
            return

        try:
            os.mkdir(new_path)
            logger.info(f"[FileTree] 新建文件夹: {new_path}")
            self._refresh_dir_in_model(parent_path)
        except Exception as e:
            logger.error(f"[FileTree] 新建文件夹失败: {e}")

    def _refresh_dir_in_model(self, dir_path: str):
        """刷新模型中的单个目录（同步重扫）"""
        if not os.path.isdir(dir_path):
            return
        try:
            entries: List[_DirEntry] = []
            with os.scandir(dir_path) as it:
                from .scanner import _should_show

                for entry in sorted(it, key=lambda e: (not e.is_dir(), e.name.lower())):
                    try:
                        if not _should_show(entry.name, entry.is_dir()):
                            continue
                        entries.append(_DirEntry(name=entry.name, path=entry.path, is_dir=entry.is_dir()))
                    except OSError:
                        continue
            self._source_model.refresh_children(dir_path, entries)
        except Exception as e:
            logger.error(f"[FileTree] 刷新目录失败: {e}")

    # ── 右键菜单 ──

    def _on_context_menu(self, src_idx: QModelIndex, global_pos):
        menu = QMenu(self)
        # 应用统一字体和主题样式
        ff = self._colors.get("font_family", "Microsoft YaHei")
        fs = self._colors.get("font_size", 14)
        bg = self._colors.get("card_bg", QColor(33, 33, 38))
        tc = self._colors.get("text", QColor(255, 255, 255))
        border = self._colors.get("border", QColor(255, 255, 255, 30))
        is_dark = self._colors.get("is_dark", True)
        hover_bg = bg.lighter(120) if is_dark else bg.darker(110)
        sep_color = border.name()

        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {bg.name()};
                border: 1px solid {sep_color};
                border-radius: 8px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 24px 6px 10px;
                color: {tc.name()};
                font-family: '{ff}';
                font-size: {fs - 2}px;
            }}
            QMenu::item:selected {{
                background-color: {hover_bg.name()};
                border-radius: 4px;
            }}
            QMenu::separator {{
                height: 1px;
                background-color: {sep_color};
                margin: 4px 8px;
            }}
        """)

        # 获取右键目标路径
        if src_idx.isValid():
            item_path = src_idx.data(FileTreeModel.COL_PATH)
            item_name = src_idx.data(Qt.DisplayRole)
            is_dir = src_idx.data(FileTreeModel.COL_IS_DIR)
            parent_path = os.path.dirname(item_path) if not is_dir else item_path
        else:
            # 点击空白 → 在根目录操作
            item_path = self._project_root
            item_name = ""
            is_dir = True
            parent_path = self._project_root

        # 菜单图标按主题文字色着色（裸 icon() 是固定黑白版，暗底/亮底会看不清）
        icon_color = tc

        def _act(icon: FluentIcon, text: str):
            action = menu.addAction(_menu_icon(icon, icon_color), text)
            return action

        # 系统操作置顶：打开类动作是右键最高频入口，不再埋到菜单底部
        if item_path:
            if not is_dir:
                _act(FluentIcon.APPLICATION, "用默认程序打开").triggered.connect(
                    lambda: self._open_file(item_path)
                )
            _act(
                FluentIcon.FOLDER,
                "在资源管理器中打开" if is_dir else "打开所在文件夹",
            ).triggered.connect(lambda: self._open_in_explorer(item_path))
            if not is_dir and detect_kind(item_path):
                _act(FluentIcon.VIEW, "预览").triggered.connect(lambda: self._open_preview(item_path))
            menu.addSeparator()

        # 信息类
        if item_path:
            _act(FluentIcon.LINK, "复制路径").triggered.connect(lambda: self._copy_to_clipboard(item_path))

        if item_name:
            _act(FluentIcon.LABEL, "复制文件名").triggered.connect(lambda: self._copy_to_clipboard(item_name))

        if item_path:
            menu.addSeparator()

        # 剪贴板操作
        has_selection = bool(self._tree_view.selected_source_paths())
        action_cut = _act(FluentIcon.CUT, "剪切")
        action_cut.setEnabled(has_selection)
        action_cut.triggered.connect(self.cut_to_clipboard)

        action_copy = _act(FluentIcon.COPY, "复制")
        action_copy.setEnabled(has_selection)
        action_copy.triggered.connect(self.copy_to_clipboard)

        # 粘贴：剪贴板空时不显示（禁用态只添噪音，用户无内容可贴时也不需要入口）
        if self._clipboard:
            _act(FluentIcon.PASTE, "粘贴").triggered.connect(self.paste_from_clipboard)

        menu.addSeparator()

        # 编辑操作
        if item_path:
            _act(FluentIcon.EDIT, "重命名").triggered.connect(
                lambda: self._tree_view.start_rename_at_index(src_idx)
            )

            _act(FluentIcon.DELETE, "删除").triggered.connect(
                lambda: self._on_delete_requested([item_path])
            )

        menu.addSeparator()

        # 新建（在目录内操作）
        _act(FluentIcon.DOCUMENT, "新建文件...").triggered.connect(
            lambda: self._create_new_file(parent_path)
        )

        _act(FluentIcon.FOLDER_ADD, "新建文件夹...").triggered.connect(
            lambda: self._create_new_dir(parent_path)
        )

        menu.addSeparator()

        # 刷新
        if item_path and is_dir:
            _act(FluentIcon.SYNC, "刷新").triggered.connect(
                lambda: self._refresh_dir_in_model(item_path)
            )

        menu.exec_(global_pos)

    # ── 刷新与关闭 ──

    def _on_refresh(self):
        # 预览态：刷新当前预览文件（文件被外部改动可即时看），不动目录树
        if self._stack.currentIndex() == _PAGE_PREVIEW:
            if not self._preview_page.reload_current():
                self._close_preview()
            return
        # 增量刷新不重建节点，已展开目录的监听保持有效，只补缺失项
        self._rebuild_watcher_paths()
        self._async_load_tree()

    def _rebuild_watcher_paths(self):
        """补挂监听：根目录 + 全部展开中的目录（``add_path`` 幂等）"""
        if self._project_root and os.path.isdir(self._project_root):
            self._watcher.add_path(self._project_root)
        for path in self._expanded_source_paths():
            self._watcher.add_path(path)

    def _on_close(self):
        self._cleanup_worker()
        self._watcher.clear()
        self.closed.emit()

    def deleteLater(self):
        self._cleanup_worker()
        self._watcher.clear()
        super().deleteLater()

    # ── 系统操作 ──

    @staticmethod
    def _copy_to_clipboard(text: str):
        QApplication.clipboard().setText(text)

    @staticmethod
    def _open_in_explorer(path: str):
        """在系统文件管理器中打开路径

        - 目录：直接打开并展示其内容（explorer /select 会打开父目录，
          导致层级往外多一层）
        - 文件：打开所在文件夹并选中该文件（/select 对文件行为正确）
        """
        norm = os.path.normpath(path)
        try:
            if os.name == "nt":
                if os.path.isdir(norm):
                    subprocess.Popen(["explorer", norm])
                else:
                    subprocess.Popen(["explorer", "/select,", norm])
            elif sys.platform == "darwin":
                if os.path.isdir(norm):
                    subprocess.Popen(["open", norm])
                else:
                    subprocess.Popen(["open", "-R", norm])
            else:
                if os.path.isdir(norm):
                    subprocess.Popen(["xdg-open", norm])
                else:
                    subprocess.Popen(["xdg-open", os.path.dirname(norm)])
        except Exception as e:
            logger.error(f"[FileTree] 打开资源管理器失败: {e}")

    @staticmethod
    def _open_file(path: str):
        try:
            if os.name == "nt":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            logger.error(f"[FileTree] 打开文件失败: {e}")

    # ── 预览 ──

    def _open_preview(self, path: str) -> None:
        """卡片内预览（双击 / 右键菜单入口）；不支持的类型回退到默认程序"""
        if not path or not os.path.isfile(path):
            self._show_error(f"文件不存在: {path}")
            return
        if not detect_kind(path):
            self._open_file(path)
            return
        self._preview_page.set_colors(
            self._colors,
            font_family=self._colors.get("font_family", ""),
            font_size=self._colors.get("font_size", 0),
            scrollbar_qss=self._scrollbar_qss,
        )
        self._preview_page.load(path)
        self._stack.setCurrentIndex(_PAGE_PREVIEW)

    def _close_preview(self) -> None:
        """返回文件树（树页未销毁，展开 / 选中 / 滚动原样保留）"""
        self._stack.setCurrentIndex(_PAGE_TREE if self._source_model.rowCount(QModelIndex()) else _PAGE_PLACEHOLDER)
        self._tree_view.setFocus()

    def _on_file_activated(self, path: str) -> None:
        """双击分流：可预览类型进预览，其余仍交系统默认程序"""
        if detect_kind(path):
            self._open_preview(path)
        else:
            self._open_file(path)

    # ── 比例尺寸（按停靠方位自适应） ──

    def _in_horizontal_dock(self) -> bool:
        """是否停靠在左/右横向停靠区（容器沿宽度轴折叠）"""
        try:
            from app.plugins.sdk import ContainerType

            ct = getattr(self.parent(), "container_type", None)
            return ct in (ContainerType.LEFT, ContainerType.RIGHT)
        except Exception:
            return False

    def sizeHint(self):
        base = super().sizeHint()
        win = self.window()
        if self._in_horizontal_dock():
            # 左/右停靠：自然宽度 300px（展开动画目标），高度填满停靠区；
            # 展开后宽度由 dockSplitter 拖拽控制，此处仅提供初始值
            h = win.height() if win and win.height() > 0 else base.height()
            return QSize(300, h)
        if win and win.height() > 0:
            return QSize(max(base.width(), 200), int(win.height() * 0.85))
        return base

    def showEvent(self, event):  # noqa: N802 (Qt 命名)
        """显示时：首次应用主题 + 懒加载目录树，随后跟随宿主窗口尺寸

        ★ 本类只允许存在**一个** showEvent：早先工作台页形态的"首次显示加载"
        曾另起同名方法定义在 ``__init__`` 之后，被这里的后定义覆盖 → 页面显示
        时从不调用 ``show_card()``，永远停在「正在加载文件树...」占位，点刷新
        又因 ``_project_root`` 未初始化而报「项目目录不存在」。
        """
        super().showEvent(event)
        if not self._first_show_done:
            self._first_show_done = True
            self.show_card()
        win = self.window()
        if win:
            win.installEventFilter(self)
            self.updateGeometry()

    def eventFilter(self, obj, event):
        if obj is self.window() and event.type() == QEvent.Resize:
            self.updateGeometry()
        return super().eventFilter(obj, event)

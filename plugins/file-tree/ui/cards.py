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
from PyQt5.QtCore import (
    QEvent,
    QItemSelectionModel,
    QModelIndex,
    QPoint,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt5.QtGui import QColor, QCursor, QFont, QIcon
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QProxyStyle,
    QSplitter,
    QStackedWidget,
    QStyle,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    MaskDialogBase,
    ScrollArea,
    SmoothScrollDelegate,
    TransparentPushButton,
    TransparentToolButton,
    isDarkTheme,
)
from app.utils.app_icon import AppIcon

from .global_preview import GlobalPreviewDialog
from .openwith import (
    DEFAULT_SUFFIX,
    icon_for_exe,
    launch,
    list_folder_openers,
    list_openers,
)
from .quote import quote_to_input
from .preview import (
    FilePreviewPage,
    QuickPreviewPanel,
    _ElidedLabel,
    _SplitButton,
    _wrap_icon,
    detect_kind,
)
from .scanner import _DirEntry, _TreeScanner
from .sticky_scroll import StickyScrollOverlay
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

# 系统文件管理器叫法（菜单 / 提示文案；mac 是访达）
if os.name == "nt":
    _FILE_MANAGER = "资源管理器"
elif sys.platform == "darwin":
    _FILE_MANAGER = "访达"
else:
    _FILE_MANAGER = "文件管理器"


def _same_path(a: str, b: str) -> bool:
    """路径同一性判定（Windows 大小写不敏感 + 分隔符 / 尾斜杠归一）

    裸字符串比较会把 ``D:/work/X`` 与 ``d:\\work\\X\\`` 判为不同根目录，
    导致同一项目的扫描结果被误当「换项目」处理（整树 reset → 展开状态全丢）。
    """
    if not a or not b:
        return False
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


# ── 主题色辅助 ────────────────────────────────────────────


def _on_solid(bg: str) -> str:
    """实心底上的前景文字色：亮底用深字，暗底用白字。

    真黑类主题（true-black / amoled-black 等）把 accent 定为纯白，
    按钮再写死白字就是白底白字。由主程序 token 统一判定，插件不自己拍阈值。
    """
    from app.plugins.sdk import readable_text_on

    return readable_text_on(bg, "#ffffff")


def _text_color(secondary: bool = False) -> str:
    if isDarkTheme():
        return "rgba(255,255,255,0.55)" if secondary else "rgba(255,255,255,0.9)"
    return "rgba(0,0,0,0.45)" if secondary else "rgba(0,0,0,0.85)"


def _file_tree_context(ctx: dict) -> dict:
    # 工作台页可能比会话窗口长寿。仅保留绘制和目录定位用到的值，
    # backend 只作 WorkbenchPanel 的就绪标志，不持有后端实例。
    fields = ("colors", "is_dark", "card_bg", "font_family", "font_size", "project_root")
    return {**{k: ctx[k] for k in fields if k in ctx}, "backend": bool(ctx.get("backend"))}

def _ctx_text_color(ctx: dict, secondary: bool = False) -> str:
    colors = ctx.get("colors", {})
    key = "text_secondary" if secondary else "text_primary"
    val = colors.get(key, "")
    return val if val else _text_color(secondary)


def _parse_theme_color(color_str: str) -> QColor:
    if not color_str:
        return QColor(33, 33, 38)
    # alpha 兼容小数（CSS 风格 rgba(...,0.66)，主题 YAML 常用）与整数（rgba(...,160)）两种写法；
    # 小数 alpha 不匹配会落到 QColor() 兜底，而 QColor 不认 CSS 函数格式 → invalid 纯黑
    m = re.match(r"rgba\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)", color_str)
    if m:
        a = float(m.group(4))
        alpha = int(round(a * 255)) if a <= 1 else int(a)
        return QColor(int(m.group(1)), int(m.group(2)), int(m.group(3)), min(255, max(0, alpha)))
    m = re.match(r"rgb\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", color_str)
    if m:
        return QColor(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    c = QColor(color_str)
    return c if c.isValid() else QColor(150, 150, 150)


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


class _MenuIconStyle(QProxyStyle):
    """菜单样式代理：图标尺寸 + 禁画原生面板

    1. QMenu 没有 setIconSize，只能改 PM_SmallIconSize。默认值随系统 DPI 放大
       （150% 屏上 16→24），菜单项会被图标和行高一起撑胖。
    2. ★ 原生 PE_PanelMenu 会先画一块**矩形底板**，QSS 的 border-radius 只裁
       内容、裁不掉底板，四角就留方角（暗色主题下是黑角）。拦掉面板绘制后，
       圆角完全由 QSS 背景决定，配合透明窗口四角才真透明。
    """

    def __init__(self, icon_size: int = 16):
        super().__init__()
        self._icon_size = int(icon_size)

    def pixelMetric(self, metric, option=None, widget=None):  # noqa: N802 (Qt 命名)
        if metric == QStyle.PM_SmallIconSize:
            return self._icon_size
        return super().pixelMetric(metric, option, widget)

    def drawPrimitive(self, element, option, painter, widget=None):  # noqa: N802
        if element == QStyle.PE_PanelMenu:
            return
        super().drawPrimitive(element, option, painter, widget)


def _menu_icon(icon: AppIcon, color: QColor) -> QIcon:
    """按主题文字色给菜单图标着色（菜单项用 icon 代替 emoji）

    AppIcon 资源是 SVG，``icon(theme=..., color=...)`` 才会重新着色；
    裸 ``icon()`` 取的是固定黑/白版，在亮底或暗底菜单里会看不清。
    """
    try:
        return icon.icon(color=color)
    except Exception:
        return icon.icon()

_OBJC_BRIDGE = None  # (objc_lib, msg, msg1) mac ObjC 桥接缓存

def _objc_bridge():
    global _OBJC_BRIDGE
    if _OBJC_BRIDGE is None:
        import ctypes
        import ctypes.util
        lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        lib.objc_getClass.restype = ctypes.c_void_p
        lib.sel_registerName.restype = ctypes.c_void_p
        msg = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
            ("objc_msgSend", lib)
        )
        msg1 = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        )(("objc_msgSend", lib))
        _OBJC_BRIDGE = (lib, msg, msg1)
    return _OBJC_BRIDGE

def _mac_clear_window_bg(widget) -> None:
    """mac：把菜单窗口的 NSWindow.backgroundColor 设为 clearColor

    ★ WA_TranslucentBackground 只把窗口标记为非 opaque，
    backgroundColor 照旧填充默认窗底色（浅色外观=白灰），从 QSS 圆角外的
    四角露出来 → 白角。必须显式 clearColor。渲染层与 isOpaque 都查不出：
    menu.grab() 四角 alpha=0、isOpaque=False，白角在合成层。
    实测（screencapture 像素统计）：四角白色 2% → 0%。仅 darwin 生效。

    ★ 时机必须在外层菜单 Show 之后（Qt 已按 popup 路径建好原生窗口）：
    提前用 winId() 强创窗口会绕开 Qt 的 popup 管理路径，菜单关闭后
    grab 不释放，整个界面僵死（点击无响应、不重绘）。已踩坑。
    """
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        lib, msg, msg1 = _objc_bridge()
        view = ctypes.c_void_p(int(widget.winId()))
        window = ctypes.c_void_p(msg(view, lib.sel_registerName(b"window")))
        nscolor = ctypes.c_void_p(lib.objc_getClass(b"NSColor"))
        clear = ctypes.c_void_p(msg(nscolor, lib.sel_registerName(b"clearColor")))
        msg1(window, lib.sel_registerName(b"setBackgroundColor:"), clear)
    except Exception as e:
        logger.warning(f"[FileTree] 清除菜单窗口底色失败: {e}")

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
                        f"  background-color: {accent.name()}; color: {_on_solid(accent.name())};"
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
        self._context = _file_tree_context(context) if isinstance(context, dict) and context else {}
        # 宿主上下文拉取入口（工作台页由 WorkbenchPanel._make_page_widget 注入）：
        # 构造时的 context 是快照，项目切换后 project_root 会过期，refresh_data
        # 时经此重取最新上下文。
        self._host_context_provider: Optional[Callable[[], dict]] = (
            context.get("context_provider") if self._context else None
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
        # 外部投递预览的「待定位路径」：首次打开工作台时页刚构建、根扫描尚未
        # 落地，此刻树里还没有该节点。记下来，根条目落地后补一次定位。
        self._pending_reveal_path: str = ""
        # 待定位链路已请求过扫描的目录（防 populate 失败时反复重扫 → 递归）
        self._pending_reveal_scanned: set = set()
        # 主题签名缓存（_apply_latest_theme 产出全等时跳过全部 set，切页高频路径）
        self._theme_signature: Optional[tuple] = None

        # 快捷预览（树下方面板）：按钮开关状态 + 关闭前的分隔高度
        self._quick_preview_on = False
        self._quick_sizes: List[int] = []

        # 全局面板（遮罩式图片/文本预览）：同一时刻只留一个
        self._global_preview: Optional[GlobalPreviewDialog] = None

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

        # 吸顶滚动：顶部可见行的祖先链显示在树**上方一行区域**（真实占位，
        # 不遮挡任何内容）。条高变化时由 StickyScrollOverlay 内部做双向对称
        # 补偿（条高与滚动值同进同退），并在拖拽滚动条期间冻结刷新 —— 细节
        # 见 sticky_scroll.py 的 _apply_height / _is_dragging。
        self._tree_host = QWidget(self._scroll_area)
        _tree_host_layout = QVBoxLayout(self._tree_host)
        _tree_host_layout.setContentsMargins(0, 0, 0, 0)
        _tree_host_layout.setSpacing(0)
        self._sticky = StickyScrollOverlay(self._tree_view, self._tree_host)
        self._sticky.context_menu_requested.connect(self._on_sticky_context_menu)
        _tree_host_layout.addWidget(self._sticky)
        _tree_host_layout.addWidget(self._tree_view, 1)

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
        self._preview_page.open_with_requested.connect(self._show_open_with_menu)
        self._preview_page.open_default_requested.connect(self._open_file)
        self._preview_page.context_menu_requested.connect(
            lambda pos: self._on_preview_context_menu(self._preview_page, pos)
        )
        self._stack.addWidget(self._preview_page)  # index 2: 文件预览页
        # 初始显示占位页，等待扫描完成
        self._stack.setCurrentIndex(1)
        # 预览页自带顶栏（返回 + 文件名 + 视图开关）；切页时树顶栏让位。
        # 用 currentChanged 统一控制可见性，所有切页入口（预览 / 返回 / 占位）都覆盖。
        self._stack.currentChanged.connect(self._on_page_changed)

        # ── 树模式顶栏（路径 + 刷新 + 打开方式）──
        # 高度/底色/分隔线与预览页顶栏同口径，两页切换时观感连续。
        self._tree_header = QFrame(self)
        self._tree_header.setObjectName("file-tree-header")
        self._tree_header.setFixedHeight(38)
        head = QHBoxLayout(self._tree_header)
        head.setContentsMargins(10, 0, 6, 0)
        head.setSpacing(2)

        self._root_label = _ElidedLabel("", self._tree_header)
        self._root_label.setMinimumWidth(36)

        # 快捷预览开关（眼睛）：开启后树下方展开面板，随选中项实时预览
        self._quick_btn = TransparentToolButton(AppIcon.VIEW, self._tree_header)
        self._quick_btn.setFixedSize(26, 26)
        self._quick_btn.setCheckable(True)
        self._quick_btn.setToolTip("快捷预览：选中文件即在下方预览")
        self._quick_btn.toggled.connect(self._on_quick_preview_toggled)
        self._refresh_btn = TransparentToolButton(AppIcon.SYNC, self._tree_header)
        self._refresh_btn.setFixedSize(26, 26)
        self._refresh_btn.setToolTip("刷新文件树")
        self._refresh_btn.clicked.connect(self._on_refresh)

        # 分裂按钮：左半直接交给系统默认程序，右半拉候选列表
        self._folder_btn = _SplitButton(self._tree_header)
        self._folder_btn.set_icon(AppIcon.FOLDER.icon())
        self._folder_btn.set_tooltip(f"在{_FILE_MANAGER}中打开")
        self._folder_btn.default_clicked.connect(self._open_root_folder)
        self._folder_btn.menu_clicked.connect(self._show_root_open_with_menu)

        head.addWidget(self._root_label, 1)
        head.addWidget(self._quick_btn)
        head.addWidget(self._refresh_btn)
        head.addWidget(self._folder_btn)

        self._scroll_area.setWidget(self._tree_host)

        # ── 树下方快捷预览（可拖拽分隔条调高度，眼睛按钮开关）──
        self._quick_panel = QuickPreviewPanel(self)
        self._quick_panel.hide()
        # 快手预览内容区右键：菜单由卡片统一构建（全局面板 / 引用 / 打开方式）
        self._quick_panel.context_menu_requested.connect(
            lambda pos: self._on_preview_context_menu(self._quick_panel.page(), pos)
        )
        # 防抖：键盘连续导航时合并加载（单次文本高亮 ~170ms，逐项加载会卡）
        self._quick_timer = QTimer(self)
        self._quick_timer.setSingleShot(True)
        self._quick_timer.setInterval(120)
        self._quick_timer.timeout.connect(self._load_quick_preview)

        self._split = QSplitter(Qt.Vertical, self)
        self._split.setObjectName("file-tree-split")
        self._split.setChildrenCollapsible(False)
        self._split.setHandleWidth(4)
        self._split.addWidget(self._stack)
        self._split.addWidget(self._quick_panel)
        self._split.setStretchFactor(0, 3)
        self._split.setStretchFactor(1, 2)
        self._split.setSizes([600, 260])

        self._vbox.addWidget(self._tree_header)
        self._vbox.addWidget(self._split, 1)

    def _on_page_changed(self, index: int) -> None:
        """树顶栏只在树/占位页显示：预览页自带顶栏，不重复占一行

        全屏预览页占满整块区域，快捷面板让位；返回树页时按开关状态恢复，
        并补一次渲染（预览期间选中项可能已经变了）。
        """
        self._tree_header.setVisible(index != _PAGE_PREVIEW)
        panel = getattr(self, "_quick_panel", None)
        if panel is None:
            return
        if index != _PAGE_PREVIEW and self._quick_preview_on:
            panel.show()
            self._split.setSizes(self._quick_sizes or [600, 260])
            self._load_quick_preview()
        else:
            if panel.isVisible():
                self._quick_sizes = self._split.sizes()  # 全屏预览后返回时还原高度
            panel.hide()

    def _setup_model(self):
        """创建 Model → Proxy → View 链路"""
        self._source_model = FileTreeModel(self)
        self._proxy_model = FileTreeFilterProxy(self)
        self._proxy_model.setSourceModel(self._source_model)
        self._tree_view.setModel(self._proxy_model)
        self._sticky.bind_model(self._proxy_model)

        # RenameDelegate
        self._rename_delegate = RenameDelegate(self._tree_view)
        self._tree_view.setItemDelegateForColumn(0, self._rename_delegate)

    def _setup_watcher(self):
        self._watcher = _DirWatcher(self)
        self._watcher.dir_changed.connect(self._on_dir_changed_externally)

    def _setup_connections(self):
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
        # 树选中项变化 → 快捷预览跟随（selectionModel 已在 _setup_model 就位）
        self._tree_view.selectionModel().currentChanged.connect(self._on_tree_current_changed)

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
        self._context = _file_tree_context(context)
        self._context_provider = lambda ctx=self._context: ctx
        if self.isVisible():
            self.show_card()

    def show_card(self):
        self._apply_latest_theme()
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
            self._context = _file_tree_context(ctx)
            self._context_provider = lambda _ctx=self._context: _ctx

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
        self._root_label.setText(self._project_root)
        self._tree_view.project_root = self._project_root
        self._watcher.set_root(self._project_root)

        tc = _ctx_text_color(ctx)
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

        # 树顶栏：与预览页顶栏同口径（透明底 + 1px 分隔线）
        border = self._colors["border"]
        header_style = (
            f"#file-tree-header {{ background: transparent;"
            f"  border-bottom: 1px solid rgba({border.red()},{border.green()},{border.blue()},90); }}"
        )
        root_style = (
            f"color: {tc_hex}; background: transparent;"
            f"  font-family: '{font_family}'; font-size: {font_size}px;"
        )
        # 快捷预览分隔条：2px 冷线，悬停延迟点亮为强调色（属性 splitterHot
        # 由主程序全局角点控制器维护；插件侧只写样式，不重复实现延迟逻辑）
        accent = self._colors["accent"]
        split_style = "#file-tree-split { background: transparent; }"
        quick_handle_qss = (
            "QSplitterHandle {"
            "  background: transparent;"
            f"  border-top: 2px solid rgba({border.red()},{border.green()},{border.blue()},90);"
            "  margin: 0; border-radius: 1px;"
            "}"
            "QSplitterHandle[splitterHot=\"true\"] {"
            f"  border-top: 2px solid {accent.name()};"
            "}"
        )

        # ★ 签名缓存：产出（全部样式串 + 字体）与上次全等时跳过所有 set。
        # 切工作台页 / 项目联动高频走这里，对整棵 QTreeView 重复
        # setStyleSheet + setFont 会触发整树 unpolish/polish + 全量重绘
        # （树 2000+ 行时单次 40-130ms，实测），而主题 / 字体 / 颜色未变时
        # 产出必然不变 —— 跳过后切页路径零成本。
        signature = (tree_style, placeholder_style, header_style, root_style, split_style, quick_handle_qss, font_family, font_size)
        if signature == self._theme_signature:
            return
        self._theme_signature = signature

        self._tree_view.setStyleSheet(tree_style)
        self._tree_header.setStyleSheet(header_style)
        self._root_label.setStyleSheet(root_style)
        secondary = self._colors.get("text_secondary", QColor(150, 150, 150))
        self._refresh_btn.setIcon(AppIcon.SYNC.icon(color=secondary))
        self._quick_btn.setIcon(
            AppIcon.VIEW.icon(
                color=self._colors["accent"] if self._quick_preview_on else secondary
            )
        )
        # 主图标与刷新钮同口径显式带色：icon() 无 color 时按 qfluentwidgets
        # isDarkTheme() 翻转，深色模式下翻转未命中 → 图标仍是黑色
        self._folder_btn.set_icon(AppIcon.FOLDER.icon(color=secondary))
        self._folder_btn.set_icon_color(secondary)
        self._folder_btn.set_hover_color(
            QColor(255, 255, 255, 26) if is_dark else QColor(0, 0, 0, 18)
        )

        self._scroll_area.setStyleSheet("#file-tree-scroll { background: transparent; border: none;}")
        self._scroll_area.viewport().setStyleSheet("background: transparent; border: none;")

        self._tree_view.setFont(QFont(font_family, font_size))

        # 占位页主题色
        self._placeholder.setStyleSheet(placeholder_style)
        self._ph_page.setStyleSheet("background: transparent;")
        self._stack.setStyleSheet("background: transparent;")
        self._split.setStyleSheet(split_style)
        # handle 样式下沉到 handle 自身（与主程序同口径，避免 splitter 子树 repolish）
        for idx in range(self._split.count() + 1):
            handle = self._split.handle(idx)
            if handle is None:
                continue
            handle.setAttribute(Qt.WA_StyledBackground, True)
            handle.setStyleSheet(quick_handle_qss)

        # 通知 delegate 更新主题色
        self._rename_delegate.set_colors(self._colors)
        self._sticky.apply_colors(self._colors)
        self._preview_page.set_colors(
            self._colors,
            font_family=font_family,
            font_size=font_size,
            scrollbar_qss=scrollbar_style,
        )
        self._quick_panel.set_colors(
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
        # 页面首次拿到项目根（model_root 为空）时是**初始装载**而非换项目：
        # 外部投递的预览（工具卡路径点击 → preview_path）可能先于根扫描结果
        # 落地，此时不得把预览页抢回树页，否则「点开文件却看到文件树」。
        is_initial_load = not model_root
        if _same_path(scan_dir, model_root):
            self._source_model.refresh_root(entries)
            if self._stack.currentIndex() == _PAGE_PREVIEW:
                self._flush_pending_reveal()
                return  # 同根刷新不抢页面（预览中保留当前阅读内容）
        else:
            self._live_expanded.clear()  # 旧根的实时展开路径不写进新根条目
            self._stash_view_state(model_root)  # 必须先存：reset 后旧状态已不可取
            self._source_model.set_project(scan_dir, entries)
            # 换根：旧根的预览内容不再属于当前上下文（同根刷新保留）
            self._quick_timer.stop()
            self._quick_panel.clear()
            cached = self._view_state_cache.get(os.path.normcase(os.path.normpath(scan_dir)))
            if cached:
                self._restore_view_state(cached)
            # 换根（切对话标签页 / 换项目）：旧预览已不属于当前上下文，
            # 必须退出预览态，否则每个标签页都停在同一个旧预览（串台）
            if is_initial_load and self._stack.currentIndex() == _PAGE_PREVIEW:
                self._flush_pending_reveal()
                return  # 初始装载：保留外部投递的预览
        self._stack.setCurrentIndex(0 if entries else 1)
        if not entries:
            self._placeholder.setText("项目目录为空")
        self._flush_pending_reveal()

    def _flush_pending_reveal(self) -> None:
        """根条目落地后补一次树内定位（外部投递预览早于根扫描的场景）

        首次打开工作台点开文件时页刚构建、根扫描尚未落地，``_reveal_in_tree``
        找不到节点只能跳过；不补这一步，用户点返回箭头回来会看到「树里没有
        任何选中」，不知道刚才看的是哪个文件。
        """
        self._advance_pending_reveal()

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
        # 外部投递的深层文件定位：每落一层就往前推一步（未投递时为零操作）
        if self._pending_reveal_path:
            self._advance_pending_reveal()
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
        # 磁盘变更后，当前预览的正是该目录下的文件则重读，保持内容与磁盘一致
        self._refresh_quick_preview_for_dir(dir_path)

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

    # ── 菜单样式 ──

    def _style_menu(self, menu: QMenu) -> None:
        """右键 / 打开方式菜单统一主题样式

        ★ 原生 QMenu 默认度量很难看：图标按 style 的 PM_SmallIconSize 放大到 20-32px、
        行高跟字体一起膨胀、圆角吃不到窗口层。这里把字号收敛到 12-14px、图标固定
        16px、item 加圆角悬浮块，并开透明窗口让 10px 圆角真生效。
        """
        ff = self._colors.get("font_family", "Microsoft YaHei")
        fs = self._colors.get("font_size", 14)
        bg = self._colors.get("card_bg", QColor(33, 33, 38))
        tc = self._colors.get("text", QColor(255, 255, 255))
        border = self._colors.get("border", QColor(255, 255, 255, 30))
        is_dark = self._colors.get("is_dark", True)
        # 菜单比卡片亮/暗一档，才能从内容区“浮”出来；悬浮块用中性色不抢强调色
        menu_bg = bg.lighter(118) if is_dark else bg.darker(103)
        hover_bg = bg.lighter(150) if is_dark else bg.darker(112)
        disabled = tc.darker(190) if is_dark else tc.lighter(190)
        sep = f"rgba({border.red()},{border.green()},{border.blue()},{110 if is_dark else 90})"
        menu_fs = max(12, min(14, fs - 4))

        # ★ QWidget.setStyle 不接管所有权：必须把 style 挂到菜单上，
        # 否则 Python GC 后菜单持有已析构的 style 指针（悬空 → 绘制时崩）
        icon_style = _MenuIconStyle(16)
        icon_style.setParent(menu)
        menu.setStyle(icon_style)
        menu.setAttribute(Qt.WA_TranslucentBackground, True)
        # mac 上 popup 窗口透明未生效时，NSWindow 会拿默认底色（浅色外观=白）
        # 从 QSS 圆角外的四角露出来；禁掉系统背景填充才不漏
        menu.setAttribute(Qt.WA_NoSystemBackground, True)
        # 系统方角阴影会在透明圆角外画一圈黑灰边，直接关掉
        menu.setWindowFlag(Qt.NoDropShadowWindowHint, True)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {menu_bg.name()};
                border: 1px solid {sep};
                border-radius: 12px;
                padding: 6px;
            }}
            QMenu::item {{
                padding: 7px 26px 7px 10px;
                margin: 1px 3px;
                border-radius: 8px;
                color: {tc.name()};
                font-family: '{ff}';
                font-size: {menu_fs}px;
            }}
            QMenu::item:selected {{
                background-color: {hover_bg.name()};
                color: {tc.name()};
            }}
            QMenu::item:disabled {{
                color: {disabled.name()};
            }}
            QMenu::separator {{
                height: 1px;
                background-color: {sep};
                margin: 4px 10px;
            }}
        """)
        # mac：默认窗底色会从 QSS 圆角外四角露出（白角），设 clearColor 消除。
        # 不能在这里直接调：winId() 提前强创原生窗口会绕开 Qt popup 管理，
        # 菜单关闭后 grab 残留导致全界面僵死；挂 filter 等菜单 Show 时再设
        if sys.platform == "darwin":
            menu.installEventFilter(self)

    # ── 右键菜单 ──

    def _on_sticky_context_menu(self, proxy_idx: QModelIndex, global_pos):
        """吸顶条右键 → 复用树的右键菜单（映射 proxy → source 后走同一链路）"""
        src_idx = self._proxy_model.mapToSource(proxy_idx)
        self._on_context_menu(src_idx, global_pos)

    def _on_context_menu(self, src_idx: QModelIndex, global_pos):
        menu = QMenu(self)
        self._style_menu(menu)
        tc = self._colors.get("text", QColor(255, 255, 255))

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

        def _act(icon: AppIcon, text: str):
            action = menu.addAction(_menu_icon(icon, icon_color), text)
            return action

        # 系统操作置顶：打开类动作是右键最高频入口，不再埋到菜单底部
        if item_path:
            if not is_dir:
                _act(AppIcon.APPLICATION, "用默认程序打开").triggered.connect(
                    lambda: self._open_file(item_path)
                )
            _act(
                AppIcon.FOLDER,
                f"在{_FILE_MANAGER}中打开" if is_dir else "打开所在文件夹",
            ).triggered.connect(lambda: self._open_in_explorer(item_path))
            # 可预览文件：预览 / 全局面板 / 引用三件套（图片同样适用）
            # ★ QAction.triggered 会带一个 checked(bool)：lambda 必须先用 *_) 吃掉它，
            #   否则默认参数 p 会被覆盖成 False，动作静默失灵（踩过）
            if not is_dir and detect_kind(item_path):
                _act(AppIcon.VIEW, "预览").triggered.connect(
                    lambda *_, p=item_path: self._open_preview(p)
                )
                _act(AppIcon.FULL_SCREEN, "全局预览").triggered.connect(
                    lambda *_, p=item_path: self._open_global_preview(p)
                )
                _act(AppIcon.SHARE, "引用到输入框").triggered.connect(
                    lambda *_, p=item_path: self._quote_file(p)
                )
            menu.addSeparator()

        # 信息类
        if item_path:
            _act(AppIcon.LINK, "复制路径").triggered.connect(lambda: self._copy_to_clipboard(item_path))

        if item_name:
            _act(AppIcon.LABEL, "复制文件名").triggered.connect(lambda: self._copy_to_clipboard(item_name))

        if item_path:
            menu.addSeparator()

        # 剪贴板操作
        has_selection = bool(self._tree_view.selected_source_paths())
        action_cut = _act(AppIcon.CUT, "剪切")
        action_cut.setEnabled(has_selection)
        action_cut.triggered.connect(self.cut_to_clipboard)

        action_copy = _act(AppIcon.COPY, "复制")
        action_copy.setEnabled(has_selection)
        action_copy.triggered.connect(self.copy_to_clipboard)

        # 粘贴：剪贴板空时不显示（禁用态只添噪音，用户无内容可贴时也不需要入口）
        if self._clipboard:
            _act(AppIcon.PASTE, "粘贴").triggered.connect(self.paste_from_clipboard)

        menu.addSeparator()

        # 编辑操作
        if item_path:
            _act(AppIcon.EDIT, "重命名").triggered.connect(
                lambda: self._tree_view.start_rename_at_index(src_idx)
            )

            _act(AppIcon.DELETE, "删除").triggered.connect(
                lambda: self._on_delete_requested([item_path])
            )

        # 新建与刷新仅在目录（含空白→根目录）上出现，文件右键不显示
        if is_dir:
            menu.addSeparator()

            _act(AppIcon.DOCUMENT, "新建文件...").triggered.connect(
                lambda: self._create_new_file(parent_path)
            )

            _act(AppIcon.FOLDER_ADD, "新建文件夹...").triggered.connect(
                lambda: self._create_new_dir(parent_path)
            )

            menu.addSeparator()

            # 刷新（原顶栏刷新按钮入口，顶栏已移除）
            _act(AppIcon.SYNC, "刷新").triggered.connect(self._on_refresh)

        menu.adjustSize()
        self._round_menu(menu)
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
        self._release_global_preview()
        self._cleanup_worker()
        self._watcher.clear()
        self.closed.emit()

    def deleteLater(self):
        self._release_global_preview()
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

    # ── 打开方式 ──

    def _show_open_with_menu(self, path: str, anchor: Optional[QPoint] = None) -> None:
        """「打开方式」菜单：系统注册的程序 + 系统默认 + 显示位置

        候选来自注册表（与资源管理器右键菜单同源）；非 Windows 或无关联时
        只剩后两项，不会出现空菜单。
        """
        if not path:
            return
        if not os.path.exists(path):
            self._show_error(f"文件不存在: {path}")
            return
        if anchor is None and self._stack.currentIndex() == _PAGE_PREVIEW:
            anchor = self._preview_page.open_with_anchor()
        menu = QMenu(self)
        self._style_menu(menu)
        icon_color = self._colors.get("text", QColor(255, 255, 255))

        def _act(icon: AppIcon, text: str):
            return menu.addAction(_menu_icon(icon, icon_color), text)

        is_dir = os.path.isdir(path)
        openers = list_folder_openers(path) if is_dir else list_openers(path)
        for opener in openers:
            label = opener.label + (DEFAULT_SUFFIX if opener.is_default else "")
            action = menu.addAction(icon_for_exe(opener.exe), label)
            action.triggered.connect(lambda _checked=False, o=opener: self._launch_with(o, path))
        if is_dir:
            # 目录候选首项就是文件管理器（默认），再列通用项会重复
            if not openers:
                _act(AppIcon.FOLDER, f"在{_FILE_MANAGER}中显示").triggered.connect(
                    lambda: self._open_in_explorer(path)
                )
            menu.exec_(self._menu_position(menu, anchor))
            return
        if openers:
            menu.addSeparator()

        _act(AppIcon.APPLICATION, "用系统默认程序打开").triggered.connect(
            lambda: self._open_file(path)
        )
        _act(AppIcon.FOLDER, f"在{_FILE_MANAGER}中显示").triggered.connect(
            lambda: self._open_in_explorer(path)
        )
        menu.exec_(self._menu_position(menu, anchor))

    # ── 树顶栏：根目录打开方式 ──

    def _root_open_anchor(self) -> QPoint:
        """分裂按钮右下角的全局坐标（菜单右对齐，不会向右溢出卡片）"""
        btn = self._folder_btn
        return btn.mapToGlobal(QPoint(btn.width(), btn.height()))

    def _open_root_folder(self):
        """左半：用系统默认程序（资源管理器）打开项目根目录"""
        if self._project_root and os.path.isdir(self._project_root):
            self._open_in_explorer(self._project_root)

    def _show_root_open_with_menu(self):
        """右半：根目录的候选程序列表"""
        root = self._project_root
        if not root or not os.path.isdir(root):
            self._show_error("项目目录不存在，请先设置工作目录")
            return
        self._show_open_with_menu(root, self._root_open_anchor())

    @staticmethod
    def _round_menu(menu: QMenu, radius: int = 12) -> None:
        """把菜单窗口本身裁成圆角

        ★ Windows 下 QMenu 的 WA_TranslucentBackground 并不会让四角真透明：
        窗口仍是矩形，四角由系统填成纯黑（实测 rgba(0,0,0,255)），QSS 的
        border-radius 只影响绘制、遮不住窗口。setMask 直接把窗口形状裁圆，
        四角连像素都不存在，才不会露黑边（代价是边缘 1px 锯齿）。
        """
        from PyQt5.QtGui import QPainterPath, QRegion

        rect = menu.rect()
        if rect.isEmpty():
            return
        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), radius, radius)
        menu.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def _menu_position(self, menu: QMenu, anchor: Optional[QPoint] = None) -> QPoint:
        """算出菜单左上角：右对齐锚点 + 夹在窗口与屏幕内

        ★ 直接 ``QCursor.pos()`` 弹菜单，在窄卡片上会向右溢出到软件窗口之外。
        这里以锚点为**右边界**反向对齐，再由窗口与屏幕边界夹回来。
        """
        menu.adjustSize()
        self._round_menu(menu)
        point = anchor or QCursor.pos()
        x = point.x() - menu.width()
        y = point.y() + 4
        win = self.window()
        if win is not None and win.isVisible():
            tl = win.mapToGlobal(QPoint(0, 0))
            x = max(tl.x() + 8, min(x, tl.x() + win.width() - menu.width() - 8))
            y = max(tl.y() + 8, min(y, tl.y() + win.height() - menu.height() - 8))
        screen = QApplication.screenAt(point) or QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            x = max(geo.left() + 4, min(x, geo.right() - menu.width() - 4))
            y = max(geo.top() + 4, min(y, geo.bottom() - menu.height() - 4))
        return QPoint(int(x), int(y))

    def _launch_with(self, opener, path: str) -> None:
        if not launch(opener, path):
            logger.error(f"[FileTree] 用 {opener.label} 打开失败: {path}")

    def _on_preview_context_menu(self, page, global_pos: QPoint) -> None:
        """预览区右键：接管 QPlainTextEdit 自带的原生英文黑菜单

        page 由调用方传入：卡片内预览页与全局面板共用同一套菜单。
        """
        if page is None or not page.current_path():
            return
        menu = QMenu(self)
        self._style_menu(menu)
        icon_color = self._colors.get("text", QColor(255, 255, 255))

        def _act(icon: AppIcon, text: str):
            return menu.addAction(_menu_icon(icon, icon_color), text)

        if page.current_kind() == "image":
            image_path = page.current_path()
            # triggered(bool) 会把默认参数覆盖掉：一律用 *_ 吃掉 checked
            _act(AppIcon.SHARE, "引用到输入框").triggered.connect(
                lambda *_, p=image_path: self._quote_file(p)
            )
            if not self._is_global_preview_page(page):
                _act(AppIcon.FULL_SCREEN, "全局预览").triggered.connect(
                    lambda *_, p=image_path: self._open_global_preview(p)
                )
            _act(AppIcon.LINK, "复制路径").triggered.connect(
                lambda *_, p=image_path: self._copy_to_clipboard(p)
            )
            menu.addSeparator()
            _act(AppIcon.APPLICATION, "用默认程序打开").triggered.connect(
                lambda *_, p=image_path: self._open_file(p)
            )
            _act(AppIcon.SYNC, "重新加载").triggered.connect(lambda *_: page.reload_current())
            menu.exec_(self._menu_position(menu, global_pos))
            return

        path, _start, _end, selected = page.quote_payload()
        if path:
            label = "引用选中内容到输入框" if selected else "引用文件到输入框"
            _act(AppIcon.SHARE, label).triggered.connect(
                lambda *_, p=page: self._quote_from_page(p)
            )
            menu.addSeparator()

        copy_act = _act(AppIcon.COPY, "复制")
        copy_act.setEnabled(page.has_selection())
        copy_act.triggered.connect(page.copy_selection)
        _act(AppIcon.ACCEPT, "全选").triggered.connect(page.select_all_text)
        menu.addSeparator()

        wrap_act = menu.addAction(
            _wrap_icon(self._colors.get("accent", icon_color) if page.wrap_enabled() else icon_color),
            f"自动换行：{'开' if page.wrap_enabled() else '关'}",
        )
        wrap_act.triggered.connect(page.toggle_wrap)
        highlight_text = f"语法高亮：{'开' if page.highlight_enabled() else '关'}"
        highlight_act = menu.addAction(_menu_icon(AppIcon.CODE, icon_color), highlight_text)
        highlight_act.triggered.connect(page.toggle_highlight)
        menu.addSeparator()

        _act(AppIcon.SYNC, "重新加载").triggered.connect(page.reload_current)
        menu.exec_(self._menu_position(menu, global_pos))

    # ── 预览 ──

    def preview_path(self, path: str) -> bool:
        """外部投递（宿主 / 其它插件）预览指定文件，返回是否已展示

        供主程序的「打开本地文件」接管链使用：点击工具卡里的文件地址时，
        可预览的文件直接开在本页，不跳出应用。

        两条路径：
        - 文件在**当前项目树内**：展开祖先 → 选中 → 滚动到可见，再进预览页。
          用户返回树页时能直接看到自己在哪。
        - 不在树内 / 项目外临时文件：只进预览页，不动树的展开状态（避免为
          一个目录外的文件把整棵树展开到无关分支）。

        ★ 不做 ``detect_kind`` 判定以外的兜底：预览失败返回 False，由调用方
          决定是否降级（宿主会继续交系统默认程序）。
        """
        if not path or not os.path.isfile(path):
            return False
        if not detect_kind(path):
            return False
        self._pending_reveal_path = os.path.normpath(path)
        self._pending_reveal_scanned.clear()
        self._advance_pending_reveal()
        return self._render_preview(path)

    def _advance_pending_reveal(self) -> None:
        """推进「待定位路径」：逐层展开未加载的祖先目录，就绪后定位选中

        首次打开工作台时页面刚构建、根扫描尚未落地，树里连目标节点都还没有；
        深层文件（``app/core/store/x.py``）更要逐层扫描才能建出节点。所以定位
        是异步的：这里只推进一层（请求扫描最深未加载的祖先目录），由扫描回调
        ``_on_lazy_scan_done`` 再次推进，直到 ``_reveal_in_tree`` 命中。

        树外文件（项目根之外）直接放弃：树里本就没有它，定位无意义。
        """
        path = self._pending_reveal_path
        if not path:
            return
        if self._reveal_in_tree(path):
            self._pending_reveal_path = ""
            self._pending_reveal_scanned.clear()
            return
        root = self._source_model.project_root()
        if not root:
            return  # 根尚未落地：等 _apply_root_entries 里的 flush 再推进
        try:
            rel = os.path.relpath(path, root)
        except ValueError:
            rel = ""
        if not rel or rel.startswith(".."):
            self._pending_reveal_path = ""  # 项目外文件：树里没有，放弃
            self._pending_reveal_scanned.clear()
            return
        parts = rel.split(os.sep)
        for depth in range(1, len(parts)):
            dir_path = os.path.join(root, *parts[:depth])
            if self._source_model.node_is_failed(dir_path):
                self._pending_reveal_path = ""  # 目录扫描已熔断，不再重试
                self._pending_reveal_scanned.clear()
                return
            node = self._source_model.find_node(dir_path)
            if node is None:
                return  # 父级还没建出子节点，等其扫描回调再推
            if not node.is_dir:
                self._pending_reveal_path = ""  # 路径中间是文件，树里不存在该目标
                self._pending_reveal_scanned.clear()
                return
            if node.loaded:
                continue
            key = os.path.normcase(os.path.normpath(dir_path))
            if key in self._pending_reveal_scanned:
                return  # 该层已请求过（扫描失败/被过滤）：等下一次触发，不重排
            self._pending_reveal_scanned.add(key)
            self._request_scan(
                dir_path, lambda entries, _d=dir_path: self._on_lazy_scan_done(entries, _d)
            )
            return
        # 祖先全部已加载但目标仍不存在（已被删除 / 被过滤规则隐藏）
        self._pending_reveal_path = ""
        self._pending_reveal_scanned.clear()

    def _reveal_in_tree(self, path: str) -> bool:
        """若目标在已加载的树内：展开祖先、选中并滚动到可见

        Returns:
            True 已定位；False 节点尚未加载 / 定位失败（调用方可稍后重试）。
        """
        try:
            target = os.path.normpath(path)
            node = self._source_model.find_node(target)
            if node is None:
                return False
            # 祖先链自顶向下展开：父级未展开时子节点没有有效 proxy 索引
            ancestors: List[str] = []
            parent = getattr(node, "parent_node", None)
            while parent is not None and getattr(parent, "is_dir", False):
                ancestors.append(parent.path)
                parent = getattr(parent, "parent_node", None)
            for dir_path in reversed(ancestors):
                dir_node = self._source_model.find_node(dir_path)
                if dir_node is None or not dir_node.loaded:
                    continue
                src_idx = self._source_model._index_for_node(dir_node)
                if not src_idx.isValid():
                    continue
                proxy_idx = self._proxy_model.mapFromSource(src_idx)
                if proxy_idx.isValid() and not self._tree_view.isExpanded(proxy_idx):
                    self._tree_view.expand(proxy_idx)
            src_idx = self._source_model._index_for_node(node)
            if not src_idx.isValid():
                return False
            proxy_idx = self._proxy_model.mapFromSource(src_idx)
            if not proxy_idx.isValid():
                return False
            self._tree_view.setCurrentIndex(proxy_idx)
            self._tree_view.scrollTo(proxy_idx, QAbstractItemView.PositionAtCenter)
            return True
        except Exception as e:  # 定位失败不影响预览本身
            logger.debug(f"[FileTree] 树内定位失败（忽略）: {path} -> {e}")
            return False

    def _open_preview(self, path: str) -> None:
        """卡片内预览（双击 / 右键菜单入口）；预览不了回退到默认程序"""
        if not path or not os.path.isfile(path):
            self._show_error(f"文件不存在: {path}")
            return
        if not detect_kind(path):
            self._open_file(path)
            return
        if not self._render_preview(path):
            # 预览失败（超大 / 损坏 / 含二进制内容）→ 回树页并交系统默认程序
            self._close_preview()
            self._open_file(path)

    def _render_preview(self, path: str) -> bool:
        """把文件渲染进预览页并切到预览态；返回是否成功（不含任何降级动作）"""
        self._preview_page.set_colors(
            self._colors,
            font_family=self._colors.get("font_family", ""),
            font_size=self._colors.get("font_size", 0),
            scrollbar_qss=self._scrollbar_qss,
        )
        if not self._preview_page.load(path):
            return False
        self._stack.setCurrentIndex(_PAGE_PREVIEW)
        return True

    def _close_preview(self) -> None:
        """返回文件树（树页未销毁，展开 / 选中 / 滚动原样保留）"""
        self._stack.setCurrentIndex(_PAGE_TREE if self._source_model.rowCount(QModelIndex()) else _PAGE_PLACEHOLDER)
        self._tree_view.setFocus()
        # 返回树页时补一次外部投递定位：用户可能在根扫描落地前就点了返回，
        # 那次定位因节点尚不存在被跳过，这里立刻接上（无待办时为零操作）
        self._advance_pending_reveal()

    # ── 全局预览 / 引用到输入框 ──

    def _open_global_preview(self, path: str) -> bool:
        """遮罩式全局面板：不受工作台页宽度限制，图片可自由拖拽

        与卡片内预览页互不影响：同一套渲染链路换个宿主，卡片内选中/展开不变。
        """
        if not path or not os.path.isfile(path) or not detect_kind(path):
            return False
        self._release_global_preview()
        overlay = GlobalPreviewDialog(
            path,
            colors=self._colors,
            parent=self.window(),
            scrollbar_qss=self._scrollbar_qss,
        )
        page = overlay.page()
        page.open_with_requested.connect(self._show_open_with_menu)
        page.open_default_requested.connect(self._open_file)
        page.context_menu_requested.connect(
            lambda pos, p=page: self._on_preview_context_menu(p, pos)
        )
        # finished 在淡出结束后发：此刻释放引用，面板对象才真正回收
        overlay.finished.connect(lambda *_: self._release_global_preview(overlay))
        self._global_preview = overlay
        overlay.show_overlay()
        return True

    def _is_global_preview_page(self, page) -> bool:
        """该预览页是否承载在全局面板里（决定菜单是否给「全局预览」入口）"""
        overlay = self._global_preview
        return overlay is not None and overlay.page() is page

    def _release_global_preview(self, overlay=None) -> None:
        """释放全局面板：传对象时只回收该对象，不传则连同引用一起清掉

        面板淡出结束（finished）后由信号调到这里 —— 此时对象已不可见，
        deleteLater 不会打断动画；新旧面板交替时旧对象也能走到回收。
        """
        target = overlay if overlay is not None else self._global_preview
        if target is None:
            return
        if self._global_preview is target:
            self._global_preview = None
        try:
            target.deleteLater()
        except Exception:
            pass

    def _close_global_preview(self) -> None:
        overlay = self._global_preview
        if overlay is None:
            return
        try:
            overlay.reject()
        except Exception:
            self._release_global_preview()

    def _quote_file(self, path: str) -> None:
        """树里右键引用整个文件（图片走胶囊，文本走胶囊或 @路径）"""
        if not path:
            return
        ok, how = quote_to_input(path, root=self._project_root)
        if not ok:
            self._notify("引用失败：输入框不可用")
            return
        if how == "text":
            self._notify("输入框胶囊不可用，已改用纯文本 @路径")
        # 引用即收起全局面板（从面板里的右键菜单进来时遮罩还挡着输入框）
        self._close_global_preview()

    def _quote_from_page(self, page) -> None:
        """预览页（卡片内 / 全局面板）引用：有选中则带内容与行号"""
        path, start, end, text = page.quote_payload()
        self._on_quote_requested(path, start, end, text)

    def _on_quote_requested(self, path: str, start: int, end: int, text: str) -> None:
        if not path:
            return
        ok, _how = quote_to_input(
            path, start=start, end=end, text=text, root=self._project_root
        )
        if not ok:
            self._notify("引用失败：输入框不可用")
            return
        # 引用即收起全局面板：写入会把对话视图切回来，遮罩留着反而挡路
        self._close_global_preview()

    def _notify(self, message: str, kind: str = "warning") -> None:
        """轻提示（ui.notify 不可用时只记日志，不弹原生框打断）"""
        try:
            from app.plugins.sdk import caps_for

            notify = caps_for("ui.notify", plugin="file-tree", surface="ui")
            if notify is not None:
                notify("引用到输入框", message, kind=kind)
                return
        except Exception:
            pass
        logger.warning(f"[file-tree] {message}")

    # ── 快捷预览（树下方常驻面板） ──

    def _on_quick_preview_toggled(self, checked: bool) -> None:
        """眼睛按钮：展开 / 收起树下方快捷预览面板"""
        self._quick_preview_on = bool(checked)
        secondary = self._colors.get("text_secondary", QColor(150, 150, 150))
        self._quick_btn.setIcon(
            AppIcon.VIEW.icon(
                color=self._colors.get("accent", QColor(80, 150, 220)) if checked else secondary
            )
        )
        if not checked:
            self._quick_timer.stop()
            if self._quick_panel.isVisible():
                self._quick_sizes = self._split.sizes()  # 记住用户拖过的高度
            self._quick_panel.hide()
            return
        self._quick_panel.show()
        self._split.setSizes(self._quick_sizes or [600, 260])
        self._load_quick_preview()

    def _on_tree_current_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        """树选中项变化 → 防抖刷新快捷预览（连按方向键只加载最后一屏）"""
        if not self._quick_preview_on or not self._quick_panel.isVisible():
            return
        if not current.isValid():
            return  # 树刷新导致选中暂时失效：保留上一屏，避免闪烁
        self._quick_timer.start()

    def _selected_tree_path(self) -> Tuple[str, bool]:
        """当前选中项 → (路径, 是否目录)；无有效选中返回 ("", False)"""
        idx = self._tree_view.currentIndex()
        if not idx.isValid():
            return "", False
        src = self._proxy_model.mapToSource(idx)
        if not src.isValid():
            return "", False
        return src.data(FileTreeModel.COL_PATH) or "", bool(src.data(FileTreeModel.COL_IS_DIR))

    def _load_quick_preview(self) -> None:
        """按当前选中项渲染快捷预览（目录/不支持类型给一行说明）"""
        if not self._quick_preview_on:
            return
        self._quick_timer.stop()
        path, is_dir = self._selected_tree_path()
        if not path:
            self._quick_panel.show_hint("选中文件即可在此预览")
        elif is_dir:
            self._quick_panel.show_hint("选中项是文件夹")
        else:
            self._quick_panel.show_path(path)

    def _refresh_quick_preview_for_dir(self, dir_path: str) -> None:
        """目录内容变化后，当前预览文件在该目录内则重读"""
        path = self._quick_panel.current_path()
        if not path or not os.path.isfile(path):
            return
        if _same_path(os.path.dirname(path), dir_path):
            self._quick_panel.show_path(path)

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
        elif event.type() == QEvent.Show:
            # 菜单原生窗口此刻已由 Qt 按 popup 路径正常创建，设底色不干扰其生命周期
            _mac_clear_window_bg(obj)
        return super().eventFilter(obj, event)

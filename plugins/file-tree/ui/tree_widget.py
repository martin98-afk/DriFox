# -*- coding: utf-8 -*-
"""文件树控件 — QTreeView + FileTreeModel + RenameDelegate + FilterProxy

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块

架构：
  FileTreeModel (QAbstractItemModel) — 懒加载数据模型
      ↓ need_scan 信号
  FileTreeFilterProxy (QSortFilterProxyModel) — 搜索过滤
      ↓
  FileTreeView (QTreeView) — 多选/拖拽/键盘
      ↓ delete/move/copy 信号
  cards.py 响应信号，处理实际操作
"""

import os
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from loguru import logger
from PyQt5.QtCore import (
    QAbstractItemModel,
    QFileInfo,
    QMimeData,
    QModelIndex,
    QObject,
    QRunnable,
    QSize,
    QSortFilterProxyModel,
    QThreadPool,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt5.QtGui import QColor, QIcon, QImage, QKeyEvent, QKeySequence, QPixmap
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileIconProvider,
    QFrame,
    QLineEdit,
    QStyle,
    QStyledItemDelegate,
    QTreeView,
    QWidget,
)

# ══════════════════════════════════════════════════════════
# 文件图标 — 后台加载 + 超时熔断
#
# ★ 主线程卡死根因（修复前）：data(DecorationRole) 同步调
#   QFileIconProvider.icon()，Windows 上落到 Win32 SHGetFileInfo()。
#   Shell 对该批文件的图标查询一次阻塞 5s 且结果不入缓存，目录首次
#   展开 / 展开状态恢复时整批重查 —— 实测 9 个 .csv（WPS 文件关联）
#   连续超时 45.2s，主线程整窗冻结（qt_messages 日志 9 条
#   "SHGetFileInfo() timed out"，每条间隔精确 5s）。
#
# ★ 现方案（A 后台化 + C 熔断）：
#   A. 主线程只读缓存，未命中先返占位图标 + 提交后台任务；后台线程
#      ctypes 直调 SHGetFileInfoW 取 HICON 转 QImage（QImage 可在工作
#      线程构造；不用 QtWinExtras —— 打包版未包含该模块），信号回主
#      线程转 QIcon 入缓存 → dataChanged 刷新该行。
#   C. 真实路径查询连续超时达阈值 → 熔断降级为 SHGFI_USEFILEATTRIBUTES
#      快速模式（按扩展名查注册表，实测 <1ms，不触发文件关联 Shell
#      扩展），冷却期后自动放行真实探测恢复。
# ══════════════════════════════════════════════════════════

_IS_WIN = sys.platform == "win32"

# 图标缓存：path -> QIcon（主线程独占读写）
_ICON_CACHE: Dict[str, QIcon] = {}
_ICON_CACHE_MAX = 8192

_ICON_TASK_STALE_SEC = 20.0  # 单任务超过此值未回 → 看门狗判定 Shell 病态
_BREAKER_THRESHOLD = 3  # 连续 N 次超时 → 打开熔断
_BREAKER_COOLDOWN_S = 60.0  # 熔断冷却时长（期间走快速模式）
_SHELL_SLOW_SEC = 3.0  # 单次真实查询耗时超过此值记为「慢」

_BREAKER_SLOW_STREAK = 0  # 连续慢计数（主线程独占）
_BREAKER_OPEN_UNTIL = 0.0  # 熔断打开截止时刻（time.monotonic）

_ICON_LOADER: Optional["_IconLoader"] = None
_FILE_ICON_PROVIDER: Optional[QFileIconProvider] = None
_PLACEHOLDER_FILE_ICON: Optional[QIcon] = None
_DIR_ICON: Optional[QIcon] = None

if _IS_WIN:
    import ctypes
    from ctypes import wintypes

    class _SHFILEINFOW(ctypes.Structure):
        _fields_ = [
            ("hIcon", ctypes.c_void_p),
            ("iIcon", ctypes.c_int),
            ("dwAttributes", wintypes.DWORD),
            ("szDisplayName", ctypes.c_wchar * 260),
            ("szTypeName", ctypes.c_wchar * 80),
        ]

    class _ICONINFO(ctypes.Structure):
        _fields_ = [
            ("fIcon", wintypes.BOOL),
            ("xHotspot", wintypes.DWORD),
            ("yHotspot", wintypes.DWORD),
            ("hbmMask", ctypes.c_void_p),
            ("hbmColor", ctypes.c_void_p),
        ]

    class _BITMAP(ctypes.Structure):
        _fields_ = [
            ("bmType", wintypes.LONG),
            ("bmWidth", wintypes.LONG),
            ("bmHeight", wintypes.LONG),
            ("bmWidthBytes", wintypes.LONG),
            ("bmPlanes", wintypes.WORD),
            ("bmBitsPixel", wintypes.WORD),
            ("bmBits", ctypes.c_void_p),
        ]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    _SHGFI_ICON = 0x100
    _SHGFI_SMALLICON = 0x1
    _SHGFI_USEFILEATTRIBUTES = 0x10
    _FILE_ATTRIBUTE_NORMAL = 0x80

    # ★ 句柄参数一律 c_void_p：64 位下 ctypes 默认 c_int 会截断句柄
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    _SHGetFileInfoW = _shell32.SHGetFileInfoW
    _SHGetFileInfoW.restype = ctypes.c_void_p
    _SHGetFileInfoW.argtypes = [
        ctypes.c_wchar_p,
        wintypes.DWORD,
        ctypes.POINTER(_SHFILEINFOW),
        ctypes.c_uint,
        ctypes.c_uint,
    ]

    _DestroyIcon = _user32.DestroyIcon
    _DestroyIcon.restype = wintypes.BOOL
    _DestroyIcon.argtypes = [ctypes.c_void_p]

    _GetIconInfo = _user32.GetIconInfo
    _GetIconInfo.restype = wintypes.BOOL
    _GetIconInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ICONINFO)]

    _GetDC = _user32.GetDC
    _GetDC.restype = ctypes.c_void_p
    _GetDC.argtypes = [ctypes.c_void_p]

    _ReleaseDC = _user32.ReleaseDC
    _ReleaseDC.restype = ctypes.c_int
    _ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    _GetObjectW = _gdi32.GetObjectW
    _GetObjectW.restype = ctypes.c_int
    _GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]

    _GetDIBits = _gdi32.GetDIBits
    _GetDIBits.restype = ctypes.c_int
    _GetDIBits.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(_BITMAPINFOHEADER),
        wintypes.UINT,
    ]

    _DeleteObject = _gdi32.DeleteObject
    _DeleteObject.restype = wintypes.BOOL
    _DeleteObject.argtypes = [ctypes.c_void_p]

    def _hicon_to_qimage(hicon) -> Optional[QImage]:
        """HICON → QImage（工作线程可安全调用；QPixmap 不行留给主线程）"""
        info = _ICONINFO()
        if not _GetIconInfo(hicon, ctypes.byref(info)):
            return None
        hbm_color = info.hbmColor
        hbm_mask = info.hbmMask
        if not hbm_color:
            if hbm_mask:
                _DeleteObject(hbm_mask)
            return None
        try:
            bm = _BITMAP()
            if not _GetObjectW(hbm_color, ctypes.sizeof(_BITMAP), ctypes.byref(bm)):
                return None
            width, height = int(bm.bmWidth), int(bm.bmHeight)
            if width <= 0 or height <= 0:
                return None

            header = _BITMAPINFOHEADER()
            header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            header.biWidth = width
            header.biHeight = -height  # 负值 = top-down，与 QImage 行序一致
            header.biPlanes = 1
            header.biBitCount = 32
            header.biCompression = 0  # BI_RGB

            buf = ctypes.create_string_buffer(width * height * 4)
            hdc = _GetDC(None)
            if not hdc:
                return None
            try:
                if _GetDIBits(hdc, hbm_color, 0, height, buf, ctypes.byref(header), 0) == 0:
                    return None
            finally:
                _ReleaseDC(None, hdc)

            # ARGB32 在小端机器上的内存布局即 GetDIBits 输出的 BGRA
            return QImage(buf, width, height, QImage.Format_ARGB32).copy()
        finally:
            _DeleteObject(hbm_color)
            if hbm_mask:
                _DeleteObject(hbm_mask)

    def _query_shell_icon(path: str, fast: bool) -> Optional[QImage]:
        """工作线程查询文件图标；失败返回 None

        fast=True 走 SHGFI_USEFILEATTRIBUTES（按扩展名查注册表，不触
        文件关联 Shell 扩展）；False 走真实路径（可能触发 WPS 一类
        扩展，最坏数秒）。
        """
        sfi = _SHFILEINFOW()
        flags = _SHGFI_ICON | _SHGFI_SMALLICON
        if fast:
            flags |= _SHGFI_USEFILEATTRIBUTES
            lookup = "x" + os.path.splitext(path)[1]
            attrs = _FILE_ATTRIBUTE_NORMAL
        else:
            lookup = path
            attrs = 0
        ret = _SHGetFileInfoW(lookup, attrs, ctypes.byref(sfi), ctypes.sizeof(sfi), flags)
        if not ret or not sfi.hIcon:
            return None
        try:
            return _hicon_to_qimage(sfi.hIcon)
        finally:
            _DestroyIcon(sfi.hIcon)

else:

    def _query_shell_icon(path: str, fast: bool) -> Optional[QImage]:  # pragma: no cover
        return None


class _IconWorkerSignals(QObject):
    """工作线程 → 主线程的结果回传（PyQt 依 receiver 亲和走 queued）"""

    loaded = pyqtSignal(str, object, float, bool)  # path, QImage|None, 耗时秒, fast


class _IconTask(QRunnable):
    """单个文件的图标查询任务（线程池执行）

    ★ 实际模式在 run() 时动态决定：提交时刻熔断尚未打开时入队的任务，
      在真正执行时若熔断已开则改走快速模式 —— 否则大目录（数百文件）
      的前几个任务触发熔断后，剩下几百个排队任务仍挨个真实查询。
      跨线程读 _BREAKER_OPEN_UNTIL（float）在 CPython 下原子安全。
    """

    def __init__(self, path: str, fast: bool, signals: _IconWorkerSignals):
        super().__init__()
        self._path = path
        self._fast = fast
        self._signals = signals  # 持有引用：任务在跑期间信号对象不被 GC

    def run(self):
        t0 = time.monotonic()
        fast = self._fast or time.monotonic() < _BREAKER_OPEN_UNTIL
        image = None
        try:
            image = _query_shell_icon(self._path, fast)
        except Exception:
            logger.debug(f"[FileTree] 后台图标查询异常: {self._path}", exc_info=True)
        self._signals.loaded.emit(self._path, image, time.monotonic() - t0, fast)


class _IconLoader(QObject):
    """图标后台加载器（模块单例，主线程亲和）

    ★ 信号连接必须 bound method：receiver=loader（主线程亲和），结果槽
      才会被 PyQt queued 回主线程执行。
    ★ 徽报批量刷新：逐个路径回发会触发逐次全树查找（_find_node 递归），
      大项目下把主线程又拖慢；改为累积 + 防抖合并，到期只发一次。
    ★ 看门狗：SHGetFileInfo 若彻底挂死（后台线程无超时保护），任务永不
      回到 _on_task_done；主线程定时器兜底把超时任务摘除并按「慢」计，
      保证熔断在 Shell 病态时仍能推进。
    """

    icons_ready = pyqtSignal()  # 一批图标已入缓存（防抖合并后发一次）

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)  # 图标查询并发上限，防 Shell 拖慢整机
        self._signals = _IconWorkerSignals(self)
        self._signals.loaded.connect(self._on_task_done)
        self._pending: Dict[str, float] = {}  # path -> 提交时刻（主线程独占）
        self._stale_paths: Set[str] = set()  # 看门狗摘除过的路径（挡重复提交）
        self._dirty = False  # 有图标变更待广播
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(120)
        self._flush_timer.timeout.connect(self._flush_ready)
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(5000)
        self._watchdog.timeout.connect(self._check_stale)
        self._watchdog.start()

    def can_request(self, path: str) -> bool:
        return path not in self._pending and path not in self._stale_paths

    def submit(self, path: str, fast: bool) -> None:
        self._pending[path] = time.monotonic()
        self._pool.start(_IconTask(path, fast, self._signals))

    def _on_task_done(self, path: str, image, elapsed: float, fast: bool):
        """任务完成（主线程槽）：计数熔断 + 转 QIcon 入缓存 + 排队广播"""
        self._pending.pop(path, None)
        if not fast:
            if elapsed >= _SHELL_SLOW_SEC:
                _note_shell_slow(elapsed)
            else:
                _reset_slow_streak()

        icon = None
        if image is not None and not image.isNull():
            icon = QIcon(QPixmap.fromImage(image))
        if icon is None or icon.isNull():
            icon = _placeholder_file_icon()
        _store_icon(path, icon)
        self._dirty = True
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush_ready(self):
        if self._dirty:
            self._dirty = False
            self.icons_ready.emit()

    def _check_stale(self):
        """看门狗：摘除长时间未回的任务（后台线程可能已被 Shell 挂死）"""
        now = time.monotonic()
        stale = [p for p, t in self._pending.items() if now - t >= _ICON_TASK_STALE_SEC]
        for p in stale:
            self._pending.pop(p, None)
            self._stale_paths.add(p)
            _note_shell_slow(_ICON_TASK_STALE_SEC)
        if stale:
            logger.warning(f"[FileTree] 图标查询任务超时未回，摘除 {len(stale)} 个: {stale[:3]}")

    def reset_breaker(self):
        """熔断冷却到期时清空超时挡板（放行真实探测）"""
        self._stale_paths.clear()

    def shutdown(self):
        """热重载退役：停看门狗 + 清未开始任务

        ★ 不销毁 QThreadPool、不断开 loaded 信号：池子析构会同步等待在跑
          任务结束（实测主线程冻结 >120s）；在途任务应照常落地结果，旧
          模块若仍有存活 widget 还能继续用。
        """
        self._watchdog.stop()
        self._pool.clear()


def _get_icon_loader() -> _IconLoader:
    """获取图标加载器（模块单例）

    ★ 热重载安全：插件重载会重建本模块，旧模块失去引用后其 loader 会被
      GC；此时若线程池任务仍在跑，``QThreadPool`` 析构会同步等待线程结束，
      把主线程冻死（实测 >120s 无响应）。故新建实例前先把旧实例「退役」：
      停定时器 / 清未开始任务 / 断信号，并保留强引用直到池子空闲 —— 强引用
      挂在 QApplication 上，模块重建也丢不掉。
    """
    global _ICON_LOADER
    if _ICON_LOADER is not None:
        return _ICON_LOADER

    app = QApplication.instance()
    if app is not None:
        old = getattr(app, "_drifox_file_tree_icon_loader", None)
        if old is not None:
            _retire_loader(app, old)

    _ICON_LOADER = _IconLoader()
    if app is not None:
        try:
            app._drifox_file_tree_icon_loader = _ICON_LOADER
        except AttributeError:
            pass
    return _ICON_LOADER


def _retire_loader(app, old) -> None:
    """退役旧 loader：停机 + 保活到池子空闲（立即析构池子会阻塞主线程）"""
    try:
        old.shutdown()
    except Exception:
        logger.debug("[FileTree] 退役旧图标加载器失败", exc_info=True)
    lst = getattr(app, "_drifox_file_tree_retired_loaders", None)
    if lst is None:
        try:
            lst = []
            app._drifox_file_tree_retired_loaders = lst
        except AttributeError:
            return
    lst.append(old)
    QTimer.singleShot(15000, lambda: _sweep_retired_loaders(app))


def _sweep_retired_loaders(app) -> None:
    """清掉池子已空闲的退役实例（此刻析构不会阻塞主线程）

    仍有活跃任务的继续保活并重排下一轮；任务被 Shell 彻底挂死时实例
    永久保活 —— 宁漏内存不冻 UI。
    """
    lst = getattr(app, "_drifox_file_tree_retired_loaders", None)
    if not isinstance(lst, list):
        return
    busy = [o for o in lst if _pool_is_busy(o)]
    lst[:] = busy
    if busy:
        QTimer.singleShot(15000, lambda: _sweep_retired_loaders(app))


def _pool_is_busy(loader) -> bool:
    pool = getattr(loader, "_pool", None)
    if pool is None:
        return False
    try:
        return pool.activeThreadCount() > 0
    except RuntimeError:
        return False  # 底层 C++ 对象已销毁


def _note_shell_slow(elapsed: float) -> None:
    """记录一次慢查询；连续超阈值则打开熔断（主线程调用）"""
    global _BREAKER_SLOW_STREAK, _BREAKER_OPEN_UNTIL
    _BREAKER_SLOW_STREAK += 1
    if _BREAKER_SLOW_STREAK >= _BREAKER_THRESHOLD:
        _BREAKER_SLOW_STREAK = 0
        _BREAKER_OPEN_UNTIL = time.monotonic() + _BREAKER_COOLDOWN_S
        _get_icon_loader().reset_breaker()
        logger.warning(
            f"[FileTree] Shell 图标查询连续 {_BREAKER_THRESHOLD} 次超时"
            f"（本次 {elapsed:.1f}s），降级为按扩展名快速模式 {_BREAKER_COOLDOWN_S:.0f}s"
        )


def _reset_slow_streak() -> None:
    global _BREAKER_SLOW_STREAK
    _BREAKER_SLOW_STREAK = 0


def _placeholder_file_icon() -> QIcon:
    """通用文件占位图标（后台结果到达前的临时显示）"""
    global _PLACEHOLDER_FILE_ICON
    if _PLACEHOLDER_FILE_ICON is None:
        _PLACEHOLDER_FILE_ICON = QApplication.style().standardIcon(QStyle.SP_FileIcon)
    return _PLACEHOLDER_FILE_ICON


def _store_icon(path: str, icon: QIcon) -> None:
    if len(_ICON_CACHE) >= _ICON_CACHE_MAX:
        _ICON_CACHE.clear()  # 简单上限保护：全清重冷一次，避免无界增长
    _ICON_CACHE[path] = icon


def _query_icon_sync(file_path: str) -> QIcon:
    """非 Windows 回退：QFileIconProvider 同步查询（无 Shell 超时问题）"""
    global _FILE_ICON_PROVIDER
    if _FILE_ICON_PROVIDER is None:
        _FILE_ICON_PROVIDER = QFileIconProvider()
    icon = _FILE_ICON_PROVIDER.icon(QFileInfo(file_path))
    if icon and not icon.isNull():
        return icon
    return QApplication.style().standardIcon(QStyle.SP_FileIcon)


def _get_file_icon(file_path: str) -> QIcon:
    """主线程取文件图标：命中缓存即返，未命中返占位并排后台任务

    ★ 本函数被 data(DecorationRole) 高频调用（重绘 / hover / 滚动 /
      选中都会来），禁止任何同步 Shell / 磁盘查询 —— 那是主线程整窗
      冻结 45s 的直接根因。
    """
    cached = _ICON_CACHE.get(file_path)
    if cached is not None:
        return cached
    if not _IS_WIN:
        icon = _query_icon_sync(file_path)
        _store_icon(file_path, icon)
        return icon
    loader = _get_icon_loader()
    if loader.can_request(file_path):
        fast = time.monotonic() < _BREAKER_OPEN_UNTIL
        loader.submit(file_path, fast)
    return _placeholder_file_icon()


def _get_dir_icon() -> QIcon:
    """目录图标（首次查询后缓存；不针对具体文件，无 Shell 卡顿风险）"""
    global _DIR_ICON, _FILE_ICON_PROVIDER
    if _DIR_ICON is not None:
        return _DIR_ICON
    if _FILE_ICON_PROVIDER is None:
        _FILE_ICON_PROVIDER = QFileIconProvider()
    icon = _FILE_ICON_PROVIDER.icon(QFileIconProvider.Folder)
    if not icon or icon.isNull():
        icon = QApplication.style().standardIcon(QStyle.SP_DirIcon)
    _DIR_ICON = icon
    return icon


# ══════════════════════════════════════════════════════════
# 数据节点
# ══════════════════════════════════════════════════════════


@dataclass
class _FileNode:
    """文件树数据节点"""

    name: str
    path: str
    is_dir: bool
    children: Optional[List["_FileNode"]] = None
    loaded: bool = False
    parent_node: Optional["_FileNode"] = None


# ══════════════════════════════════════════════════════════
# FileTreeModel — 数据模型
# ══════════════════════════════════════════════════════════


class FileTreeModel(QAbstractItemModel):
    """自定义文件树 Model — 懒加载 + 排序（文件夹优先）"""

    need_scan = pyqtSignal(str)  # 请求异步扫描 dir_path

    COL_PATH = Qt.UserRole
    COL_IS_DIR = Qt.UserRole + 1

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._root_entries: List[_FileNode] = []
        self._project_root: str = ""
        self._loading: set = set()  # 正在扫描/排队中的目录（normpath 归一）
        self._failed: set = set()  # 扫描失败过的目录（熔断：不再自动重试）
        # 后台图标就绪 → 批量刷新装饰列（bound method：receiver=本模型，主线程执行）
        _get_icon_loader().icons_ready.connect(self._on_icons_ready)

    # ── 公开 API（cards.py 调用） ──

    def set_project(self, project_root: str, entries):
        """设置项目根目录并加载顶级条目（scanner 结果）

        ★ 整体 reset：QTreeView 会丢弃全部展开/选中/滚动状态。**只在项目根
        真的变化时调用**（见 cards.py ``_apply_root_entries``）；同项目刷新
        一律走 ``refresh_root`` 增量。
        """
        self.beginResetModel()
        self._project_root = os.path.normpath(project_root)
        self._root_entries = self._build_nodes(entries, None)
        self._loading.clear()
        self._failed.clear()  # 换了项目：旧根的标记全部失效
        self.endResetModel()

    def project_root(self) -> str:
        """当前项目根（已 normpath 归一，供卡片判定「根是否变化」）"""
        return self._project_root

    def populate_children(self, dir_path: str, entries) -> bool:
        """扫描完成后填充子节点"""
        node = self._find_node(dir_path)
        if not node:
            self._loading.discard(dir_path)
            return False
        parent_idx = self._index_for_node(node)
        if not parent_idx.isValid() and node.parent_node is not None:
            self._loading.discard(dir_path)
            return False
        if node.loaded:
            self._clear_children(node, parent_idx)
        self._insert_children(node, parent_idx, entries)
        node.loaded = True
        self._loading.discard(dir_path)
        return True

    def refresh_children(self, dir_path: str, entries):
        """外部变更后增量刷新子节点 — 只增删差异，保留未变节点（展开/选择/滚动状态不丢失）"""
        node = self._find_node(dir_path)
        if not node or not node.loaded:
            return
        if node.children is None:
            node.children = []
        parent_idx = self._index_for_node(node)
        self._sync_entries(node.children, parent_idx, entries, node)

    def refresh_root(self, entries):
        """外部变更后增量刷新根目录顶级条目（根目录自身无节点，单独处理）"""
        self._sync_entries(self._root_entries, QModelIndex(), entries, None)

    def rename_node(self, old_path: str, new_name: str) -> Optional[str]:
        """重命名节点，返回新路径；失败返回 None"""
        node = self._find_node(old_path)
        if not node:
            return None
        new_path = os.path.join(os.path.dirname(old_path), new_name)
        node.name = new_name
        node.path = new_path
        idx = self._index_for_node(node)
        if idx.isValid():
            self.dataChanged.emit(idx, idx, [Qt.DisplayRole, Qt.ToolTipRole])
        return new_path

    def remove_node(self, path: str):
        """从模型移除节点"""
        node = self._find_node(path)
        if not node:
            return
        parent = node.parent_node
        if parent is None:
            # 顶级
            row = self._find_row_in_list(self._root_entries, node)
            if row >= 0:
                self.beginRemoveRows(QModelIndex(), row, row)
                self._root_entries.pop(row)
                self.endRemoveRows()
        elif parent.children is not None:
            parent_idx = self._index_for_node(parent)
            row = self._find_row_in_list(parent.children, node)
            if row >= 0:
                self.beginRemoveRows(parent_idx, row, row)
                parent.children.pop(row)
                self.endRemoveRows()

    def add_node(self, parent_path: str, entry) -> Optional[QModelIndex]:
        """添加新子节点（按排序位置插入），返回 index"""
        parent_node = self._find_node(parent_path)
        if not parent_node:
            return None
        if parent_node.children is None:
            parent_node.children = []
            parent_node.loaded = True

        parent_idx = self._index_for_node(parent_node)
        insert_row = self._sorted_insert_pos(parent_node.children, entry)
        self.beginInsertRows(parent_idx, insert_row, insert_row)
        new_node = _FileNode(
            name=entry.name,
            path=entry.path,
            is_dir=entry.is_dir,
            parent_node=parent_node,
            loaded=False,
        )
        parent_node.children.insert(insert_row, new_node)
        self.endInsertRows()
        return self._index_for_node(new_node)

    def find_node(self, path: str) -> Optional[_FileNode]:
        return self._find_node(path)

    def node_is_loading(self, path: str) -> bool:
        """检查某路径是否正在异步扫描中"""
        return os.path.normpath(path) in self._loading

    def mark_loading(self, path: str):
        """标记某路径正在扫描（从入队起就置位，到扫描落定才清）

        ★ 置位窗口必须覆盖「入队 → 扫描完成」全程：Qt 会对「已展开且
        canFetchMore 为真」的节点每轮布局请求一次 fetchMore，若只在扫描执行
        期间置位，排队中的目录仍可 fetch，会与扫描请求互相踩踏（实测导致
        扫描风暴 + 主线程反复阻塞）。
        """
        self._loading.add(os.path.normpath(path))

    def clear_loading(self, path: str):
        """扫描落定时清除加载标记"""
        self._loading.discard(os.path.normpath(path))

    def clear_all_loading(self):
        """清空全部加载标记（换项目 / 重置调度时用）"""
        self._loading.clear()

    def mark_failed(self, path: str):
        """标记某目录扫描失败（熔断：canFetchMore 返回 False，不再自动重试）

        无此熔断时，持续失败的目录（无权限 / 已删除）会被 Qt 每轮布局重试，
        形成慢速请求风暴。用户点刷新（``clear_failed``）可手动重试。
        """
        self._failed.add(os.path.normpath(path))

    def clear_failed(self):
        """清除失败熔断（手动刷新时允许重试）"""
        self._failed.clear()

    def node_is_failed(self, path: str) -> bool:
        """该目录是否已熔断（扫描失败过，不再自动重试）"""
        return os.path.normpath(path) in self._failed

    def node_is_loaded(self, idx: QModelIndex) -> bool:
        if not idx.isValid():
            return False
        node: _FileNode = idx.internalPointer()
        return node.loaded

    # ── QAbstractItemModel 接口 ──

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        if not parent.isValid():
            if row < len(self._root_entries):
                return self.createIndex(row, column, self._root_entries[row])
        else:
            parent_node: _FileNode = parent.internalPointer()
            if parent_node.children is not None and row < len(parent_node.children):
                return self.createIndex(row, column, parent_node.children[row])
        return QModelIndex()

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        node: _FileNode = index.internalPointer()
        if node.parent_node is None:
            return QModelIndex()
        return self._index_for_node(node.parent_node)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if not parent.isValid():
            return len(self._root_entries)
        node: _FileNode = parent.internalPointer()
        if node.children is not None:
            return len(node.children)
        return 0

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 1

    def hasChildren(self, parent: QModelIndex = QModelIndex()) -> bool:
        """告知 Qt 该节点是否有子节点（未加载的目录假设有）"""
        if not parent.isValid():
            return len(self._root_entries) > 0
        node: _FileNode = parent.internalPointer()
        if not node.is_dir:
            return False
        if node.loaded:
            return bool(node.children)
        return True  # 未加载的目录假设有子节点

    def canFetchMore(self, parent: QModelIndex) -> bool:
        """Qt 在展开节点前调用 — 未加载、未在扫描/排队、且未失败过才返回 True"""
        if not parent.isValid():
            return False
        node: _FileNode = parent.internalPointer()
        if not node.is_dir:
            return False
        if node.loaded:
            return False
        key = os.path.normpath(node.path)
        if key in self._loading:
            return False
        if key in self._failed:
            return False  # 熔断：失败过的目录不自动重试，避免 Qt 每轮布局重试
        return True

    def fetchMore(self, parent: QModelIndex):
        """Qt 自动调用 — 触发异步扫描"""
        node: _FileNode = parent.internalPointer()
        if not node or not node.is_dir:
            return
        self._loading.add(os.path.normpath(node.path))  # 口径与 mark_loading 一致
        self.need_scan.emit(node.path)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        node: _FileNode = index.internalPointer()
        if role == Qt.DisplayRole:
            return node.name
        if role == Qt.DecorationRole:
            return _get_dir_icon() if node.is_dir else _get_file_icon(node.path)
        if role == self.COL_PATH:
            return node.path
        if role == self.COL_IS_DIR:
            return node.is_dir
        return None

    def _on_icons_ready(self):
        """一批后台图标入缓存（主线程槽，已防抖合并）

        ★ 发全量 dataChanged（无效索引对）而非逐行：data() 现在为纯缓存
          查表（微秒级），全量广播的成本落在视口可见行；逐行定位反而
          要做全树递归查找，大项目下更慢。
        """
        if not self._root_entries:
            return
        self.dataChanged.emit(QModelIndex(), QModelIndex(), [Qt.DecorationRole])

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.ItemIsDropEnabled
        node: _FileNode = index.internalPointer()
        # ★ ItemIsEditable 必须给：QAbstractItemView.edit() 在 flags 缺此项时
        # 直接返回 False（不改任何可见状态、不报错），F2 / 右键「重命名」全部
        # 静默失效。视图 editTriggers 设为 NoEditTriggers 只是禁「自动」触发，
        # 不拦显式 edit()。
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled | Qt.ItemIsEditable
        if node.is_dir:
            flags |= Qt.ItemIsDropEnabled
        return flags

    def supportedDropActions(self) -> Qt.DropActions:
        return Qt.MoveAction | Qt.CopyAction

    def mimeTypes(self) -> List[str]:
        return ["text/uri-list", "application/x-drifox-filelist"]

    def mimeData(self, indexes: List[QModelIndex]) -> QMimeData:
        mime_data = QMimeData()
        paths = [idx.data(self.COL_PATH) for idx in indexes if idx.isValid()]
        if paths:
            mime_data.setUrls([QUrl.fromLocalFile(p) for p in paths])
            mime_data.setData("application/x-drifox-filelist", "\n".join(paths).encode("utf-8"))
        return mime_data

    def canDropMimeData(self, data, action, row, column, parent):
        if not parent.isValid():
            return False
        node: _FileNode = parent.internalPointer()
        return node.is_dir

    def dropMimeData(self, data, action, row, column, parent):
        # 实际操作由 FileTreeView.dropEvent 处理
        return False

    # ── 内部辅助 ──

    def _build_nodes(self, entries, parent) -> List[_FileNode]:
        nodes = []
        for e in entries:
            nodes.append(
                _FileNode(
                    name=e.name,
                    path=e.path,
                    is_dir=e.is_dir,
                    parent_node=parent,
                    loaded=False,
                )
            )
        return nodes

    def _insert_children(self, node: _FileNode, parent_idx: QModelIndex, entries):
        children = self._build_nodes(entries, node)
        if not children:
            node.children = []
            return
        self.beginInsertRows(parent_idx, 0, len(children) - 1)
        node.children = children
        self.endInsertRows()

    def _clear_children(self, node: _FileNode, parent_idx: QModelIndex):
        if node.children:
            count = len(node.children)
            self.beginRemoveRows(parent_idx, 0, count - 1)
            node.children = None
            self.endRemoveRows()

    def _sync_entries(
        self,
        container: List[_FileNode],
        parent_idx: QModelIndex,
        entries,
        parent_node: Optional[_FileNode],
    ):
        """增量同步条目列表 — 只增删差异，未变节点保持原引用

        - 保留未变节点的展开/选择/滚动状态（不触发模型 reset）
        - 删除已消失条目、插入新增条目（按 scanner 排序规则定位）
        - 新增目录节点 loaded=False，展开时走懒加载
        """
        new_keys = {(e.name, e.is_dir) for e in entries}

        # 1) 删除已消失的条目（从后往前，保持索引有效）
        for row in range(len(container) - 1, -1, -1):
            child = container[row]
            if (child.name, child.is_dir) not in new_keys:
                self.beginRemoveRows(parent_idx, row, row)
                container.pop(row)
                self.endRemoveRows()

        # 2) 插入新增条目（按排序规则定位，保持目录在前、名称字母序）
        existing = {(c.name, c.is_dir) for c in container}
        for entry in entries:
            key = (entry.name, entry.is_dir)
            if key in existing:
                continue
            row = self._sorted_insert_pos(container, entry)
            self.beginInsertRows(parent_idx, row, row)
            container.insert(
                row,
                _FileNode(
                    name=entry.name,
                    path=entry.path,
                    is_dir=entry.is_dir,
                    parent_node=parent_node,
                    loaded=False,
                ),
            )
            self.endInsertRows()
            existing.add(key)

    def _find_node(self, norm_path: str) -> Optional[_FileNode]:
        target = os.path.normpath(norm_path)
        for entry in self._root_entries:
            result = self._find_node_recursive(entry, target)
            if result:
                return result
        return None

    def _find_node_recursive(self, node: _FileNode, target: str) -> Optional[_FileNode]:
        if os.path.normpath(node.path) == target:
            return node
        if node.children:
            for child in node.children:
                result = self._find_node_recursive(child, target)
                if result:
                    return result
        return None

    def _index_for_node(self, node: _FileNode) -> QModelIndex:
        if node is None:
            return QModelIndex()
        if node.parent_node is None:
            row = self._find_row_in_list(self._root_entries, node)
            return self.createIndex(row, 0, node) if row >= 0 else QModelIndex()
        parent_idx = self._index_for_node(node.parent_node)
        if not parent_idx.isValid():
            return QModelIndex()
        if node.parent_node.children is None:
            return QModelIndex()
        row = self._find_row_in_list(node.parent_node.children, node)
        return self.createIndex(row, 0, node) if row >= 0 else QModelIndex()

    @staticmethod
    def _find_row_in_list(lst: List[_FileNode], node: _FileNode) -> int:
        for i, item in enumerate(lst):
            if item is node:
                return i
        return -1

    @staticmethod
    def _sorted_insert_pos(children: List[_FileNode], entry) -> int:
        """找到排序插入位置：文件夹在前，按名称小写字母序"""
        for i, child in enumerate(children):
            if child.is_dir and not entry.is_dir:
                continue
            if not child.is_dir and entry.is_dir:
                return i
            if entry.name.lower() < child.name.lower():
                return i
        return len(children)


# ══════════════════════════════════════════════════════════
# FileTreeFilterProxy — 搜索过滤
# ══════════════════════════════════════════════════════════


class FileTreeFilterProxy(QSortFilterProxyModel):
    """文件树搜索过滤代理 — 仅根据文件名过滤"""

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.setRecursiveFilteringEnabled(True)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.setFilterRole(Qt.DisplayRole)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if not isinstance(model, FileTreeModel):
            return super().filterAcceptsRow(source_row, source_parent)

        pattern = self.filterRegularExpression().pattern()
        if not pattern:
            return True

        idx = model.index(source_row, 0, source_parent)
        name = model.data(idx, Qt.DisplayRole) or ""
        is_dir = model.data(idx, model.COL_IS_DIR)

        if pattern.lower() in name.lower():
            return True

        # 未加载的目录 — 乐观显示（可能子节点会匹配）
        if is_dir:
            node = model.find_node(model.data(idx, model.COL_PATH))
            if node and node.children is None:
                return True

        # 已加载的依赖 recursiveFilteringEnabled 检查子节点
        return False


# ══════════════════════════════════════════════════════════
# RenameDelegate — 原地重命名
# ══════════════════════════════════════════════════════════


class RenameDelegate(QStyledItemDelegate):
    """文件/文件夹原地重命名编辑器"""

    rename_requested = pyqtSignal(str, str)  # old_path, new_name

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._colors: dict = {}

    def set_colors(self, colors: dict):
        self._colors = colors

    def createEditor(self, parent_widget, option, index):
        editor = QLineEdit(parent_widget)
        accent = self._colors.get("accent", QColor("#62a0ea"))
        tc = self._colors.get("text", QColor(255, 255, 255))
        ff = self._colors.get("font_family", "Microsoft YaHei")
        fs = self._colors.get("font_size", 14)
        is_dark = self._colors.get("is_dark", True)

        # ★ 背景必须不透明：编辑器只「覆盖」在 item 之上，item 自己仍会绘制
        # 名称文字。半透明底（旧值 rgba(255,255,255,0.08)）会让两层文字叠加
        # 成重影，长文件名时尤其明显。
        # ★ 也不能用 card_bg 当基色：插件 ctx 的 colors 字典没有 card_bg 键
        # （主题只给 border/text_primary/text_secondary/accent），fallback 恒为
        # 深色 QColor(33,33,38)，浅色主题下会变成深底配深字。按 is_dark 直接定：
        # 深色取略亮的深灰，浅色取纯白。
        editor_bg = QColor(45, 45, 52) if is_dark else QColor(255, 255, 255)

        editor.setStyleSheet(
            f"QLineEdit {{"
            f"  background-color: {editor_bg.name()};"
            f"  border: 2px solid {accent.name()};"
            f"  border-radius: 3px;"
            f"  padding: 2px 6px;"
            f"  color: {tc.name()};"
            f"  font-family: '{ff}';"
            f"  font-size: {fs}px;"
            f"}}"
        )
        return editor

    def setEditorData(self, editor, index):
        name = index.data(Qt.DisplayRole) or ""
        editor.setText(name)
        is_dir = index.data(FileTreeModel.COL_IS_DIR)
        if not is_dir:
            dot = name.rfind(".")
            if dot > 0:
                editor.setSelection(0, dot)
            else:
                editor.selectAll()
        else:
            editor.selectAll()

    def setModelData(self, editor, model, index):
        new_name = editor.text().strip()
        old_name = index.data(Qt.DisplayRole)
        if not new_name or new_name == old_name:
            return
        if "/" in new_name or "\\" in new_name:
            logger.warning("[FileTree] 文件名含非法字符，取消重命名")
            return
        old_path = index.data(FileTreeModel.COL_PATH)
        self.rename_requested.emit(old_path, new_name)


# ══════════════════════════════════════════════════════════
# FileTreeView — 视图控件
# ══════════════════════════════════════════════════════════


class FileTreeView(QTreeView):
    """多选文件树视图 — 键盘/拖拽/右键"""

    # 请求卡片处理实际操作
    delete_requested = pyqtSignal(list)  # List[str] paths
    move_requested = pyqtSignal(list, str)  # paths, dest_dir
    copy_requested = pyqtSignal(list, str)  # paths, dest_dir
    context_menu_requested = pyqtSignal(QModelIndex, object)  # index, global_pos
    double_clicked_file = pyqtSignal(str)  # file_path
    enter_expand_requested = pyqtSignal(str)  # dir_path (Enter 键展开)
    clipboard_copy = pyqtSignal()  # Ctrl+C
    clipboard_cut = pyqtSignal()  # Ctrl+X
    clipboard_paste = pyqtSignal()  # Ctrl+V

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setAnimated(True)
        self.setIndentation(20)
        self.setHeaderHidden(True)
        self.setIconSize(QSize(18, 18))
        self.setFrameShape(QFrame.NoFrame)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setExpandsOnDoubleClick(False)
        self._drag_action: Optional[Qt.DropAction] = None
        self.project_root: str = ""

    # ── 键盘 ──

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Delete:
            self._emit_delete()
        elif event.key() == Qt.Key_F2:
            self._start_rename()
        elif event.key() == Qt.Key_Return or event.key() == Qt.Key_Enter:
            self._handle_enter()
        elif event.matches(QKeySequence.Copy):
            self.clipboard_copy.emit()
        elif event.matches(QKeySequence.Cut):
            self.clipboard_cut.emit()
        elif event.matches(QKeySequence.Paste):
            self.clipboard_paste.emit()
        elif event.matches(QKeySequence.SelectAll):
            self.selectAll()
        else:
            super().keyPressEvent(event)

    # ── 双击 ──

    def mouseDoubleClickEvent(self, event):
        idx = self.indexAt(event.pos())
        if not idx.isValid():
            super().mouseDoubleClickEvent(event)
            return
        src_idx = self._map_to_source(idx)
        is_dir = src_idx.data(FileTreeModel.COL_IS_DIR)
        if is_dir:
            # 双击目录：展开/折叠
            self.setExpanded(idx, not self.isExpanded(idx))
        else:
            path = src_idx.data(FileTreeModel.COL_PATH)
            if path:
                self.double_clicked_file.emit(path)
        # 不调用 super，避免 QTreeView 默认行为

    # ── 右键菜单 ──

    def contextMenuEvent(self, event):
        idx = self.indexAt(event.pos())
        src_idx = self._map_to_source(idx) if idx.isValid() else QModelIndex()
        self.context_menu_requested.emit(src_idx, event.globalPos())

    # ── 拖拽 ──

    def dragEnterEvent(self, event):
        if event.source() is self:
            event.acceptProposedAction()
            super().dragEnterEvent(event)
        elif event.mimeData().hasUrls():
            # 直接 accept，不走 super → super 会咨询 canDropMimeData，
            # 空白区域返回 False 导致事件被吞
            event.accept()
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        # ── 外部拖拽（系统文件管理器） ──
        if event.source() is not self:
            if not event.mimeData().hasUrls():
                event.ignore()
                return
            target_idx = self.indexAt(event.pos())
            if target_idx.isValid():
                # 有效节点（文件/目录）→ 让 QTreeView 显示高亮或行间指示器
                super().dragMoveEvent(event)
                return
            # 空白区域 → 有根目录才接受（不能走 super，理由同上）
            if not self.project_root or not os.path.isdir(self.project_root):
                event.ignore()
                return
            event.accept()
            return

        # 根据键盘修饰符切换移动/复制
        if event.keyboardModifiers() & Qt.ControlModifier:
            self._drag_action = Qt.CopyAction
            event.setDropAction(Qt.CopyAction)
        else:
            self._drag_action = Qt.MoveAction
            event.setDropAction(Qt.MoveAction)

        super().dragMoveEvent(event)

    def dropEvent(self, event):
        # ── 外部拖拽（系统文件管理器） ──
        if event.source() is not self:
            if not event.mimeData().hasUrls():
                event.ignore()
                return

            # 从 URLs 提取本地文件路径
            source_paths = []
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    file_path = url.toLocalFile()
                    if os.path.exists(file_path):
                        source_paths.append(file_path)

            if not source_paths:
                event.ignore()
                return

            # 确定目标目录
            target_idx = self.indexAt(event.pos())
            if target_idx.isValid():
                target_src = self._map_to_source(target_idx)
                target_path = target_src.data(FileTreeModel.COL_PATH)
                target_is_dir = target_src.data(FileTreeModel.COL_IS_DIR)
                if target_path:
                    dest_dir = target_path if target_is_dir else os.path.dirname(target_path)
                else:
                    dest_dir = ""
            else:
                # 空白区域 → 回退到项目根目录
                dest_dir = self.project_root

            if not dest_dir or not os.path.isdir(dest_dir):
                event.ignore()
                return

            # 防拖拽到自身
            for src in source_paths:
                if os.path.normpath(src) == os.path.normpath(dest_dir):
                    logger.warning(f"[FileTree] 禁止拖拽到自身: {src}")
                    event.ignore()
                    return

            action = event.dropAction()
            if action == Qt.CopyAction:
                self.copy_requested.emit(source_paths, dest_dir)
            else:
                self.move_requested.emit(source_paths, dest_dir)

            event.acceptProposedAction()
            return

        # ── 内部拖拽（树内节点移动/复制） ──
        action = self._drag_action or event.dropAction()
        self._drag_action = None

        target_idx = self.indexAt(event.pos())
        if not target_idx.isValid():
            event.ignore()
            return
        target_src = self._map_to_source(target_idx)
        target_path = target_src.data(FileTreeModel.COL_PATH)
        target_is_dir = target_src.data(FileTreeModel.COL_IS_DIR)
        if not target_path:
            event.ignore()
            return

        dest_dir = target_path if target_is_dir else os.path.dirname(target_path)
        if not dest_dir:
            event.ignore()
            return

        selected_idxs = self.selectionModel().selectedRows()
        source_paths: List[str] = []
        for idx in selected_idxs:
            src_idx = self._map_to_source(idx)
            p = src_idx.data(FileTreeModel.COL_PATH)
            if not p:
                continue
            norm_src = os.path.normpath(p)
            norm_dst = os.path.normpath(dest_dir)
            if norm_src == norm_dst:
                continue
            if norm_dst.startswith(norm_src + os.sep):
                logger.warning(f"[FileTree] 禁止循环拖拽: {p} → {dest_dir}")
                continue
            source_paths.append(p)

        if not source_paths:
            event.ignore()
            return

        if action == Qt.CopyAction:
            self.copy_requested.emit(source_paths, dest_dir)
        else:
            self.move_requested.emit(source_paths, dest_dir)

        event.acceptProposedAction()

    # ── 重命名 ──

    def start_rename_at_index(self, src_idx: QModelIndex):
        """外部触发重命名（右键菜单或 F2）"""
        if not src_idx.isValid():
            return
        proxy_idx = self._map_from_source(src_idx)
        if proxy_idx.isValid():
            self.edit(proxy_idx)

    def _start_rename(self):
        idxs = self.selectionModel().selectedRows()
        if not idxs:
            return
        self.edit(idxs[0])

    # ── Delete ──

    def _emit_delete(self):
        idxs = self.selectionModel().selectedRows()
        paths = []
        for idx in idxs:
            src_idx = self._map_to_source(idx)
            p = src_idx.data(FileTreeModel.COL_PATH)
            if p:
                paths.append(p)
        if paths:
            self.delete_requested.emit(paths)

    # ── Enter ──

    def _handle_enter(self):
        idxs = self.selectionModel().selectedRows()
        if not idxs:
            return
        src_idx = self._map_to_source(idxs[0])
        is_dir = src_idx.data(FileTreeModel.COL_IS_DIR)
        path = src_idx.data(FileTreeModel.COL_PATH)
        if not path:
            return
        if is_dir:
            self.enter_expand_requested.emit(path)
        else:
            self.double_clicked_file.emit(path)

    # ── 索引映射（source ↔ proxy） ──

    def _map_to_source(self, proxy_idx: QModelIndex) -> QModelIndex:
        p = self.model()
        if isinstance(p, QSortFilterProxyModel):
            return p.mapToSource(proxy_idx)
        return proxy_idx

    def _map_from_source(self, src_idx: QModelIndex) -> QModelIndex:
        p = self.model()
        if isinstance(p, QSortFilterProxyModel):
            return p.mapFromSource(src_idx)
        return src_idx

    def selected_source_paths(self) -> List[str]:
        """获取选中项路径（直接查 source model）"""
        paths = []
        for idx in self.selectionModel().selectedRows():
            src_idx = self._map_to_source(idx)
            p = src_idx.data(FileTreeModel.COL_PATH)
            if p:
                paths.append(p)
        return paths

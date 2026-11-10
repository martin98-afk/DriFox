# -*- coding: utf-8 -*-
"""数据层 — 缓存目录扫描 + 大小计算 + 异步 Workers

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
- 所有文件操作通过 os/shutil/stdlib 完成
"""

import csv
import ctypes
import locale
import os
import re
import shutil
import stat
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

from PyQt5.QtCore import QObject, pyqtSignal

# Windows 下从 GUI 进程调用控制台程序会弹黑框，需显式隐藏子进程控制台窗口
_CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# ── 路径常量 ──────────────────────────────────────────────


def _drifox_dir() -> Path:
    """获取应用数据目录（与 app.utils.utils.get_app_data_dir 保持一致）

    开发环境: 当前目录/.drifox
    PyInstaller打包: ~/.drifox（用户 home 目录，可写）
    macOS .app: ~/Library/Application Support/Drifox/.drifox
    """
    if not hasattr(sys, "_MEIPASS") and not getattr(sys, "frozen", False):
        return Path(".drifox")
    if sys.platform == "darwin":
        try:
            from AppKit import NSApplicationSupportDirectory, NSFileManager, NSUserDomainMask

            paths = NSFileManager.defaultManager().URLsForDirectory_inDomains_(
                NSApplicationSupportDirectory, NSUserDomainMask
            )
            if paths:
                app_support_path = paths[0].fileSystemRepresentation().decode("utf-8")
                app_support = Path(app_support_path) / "Drifox"
                app_support.mkdir(parents=True, exist_ok=True)
                return app_support / ".drifox"
        except Exception:
            pass
    return Path.home() / ".drifox"


# ── 缓存类型定义 ──────────────────────────────────────────


class CleanerItem(NamedTuple):
    """可清理项定义"""

    cid: str
    icon: str  # icons.py 里的 SVG 图标键（线性风格，替代 emoji）
    label: str
    rel_path: str
    dir_mode: bool  # 保留兼容：旧版二选一统计模式，现已统一递归，不再分支
    default_checked: bool = True
    tip: str = ""


CACHE_DEFS: List[CleanerItem] = [
    # 备份顶层两类：edits=编辑类（可安全清），deletes=删除类（误删恢复手段，默认保留）
    CleanerItem(
        "edits",
        "copy",
        "编辑类备份",
        "backups/edits",
        True,
        True,
        "AI 修改文件前的自动备份，可用于回滚；清掉后旧编辑无法撤销",
    ),
    CleanerItem(
        "deletes",
        "trash",
        "删除类备份",
        "backups/deletes",
        True,
        False,
        "AI 删除文件前的自动备份（垃圾箱），误删后唯一恢复手段，建议保留",
    ),
    CleanerItem("logs", "file", "日志文件", "logs", False, True, "运行日志，排查问题时有用"),
    CleanerItem("cache", "database", "应用缓存", "cache", True, False, "可重建的离线缓存"),
    CleanerItem("screenshots", "camera", "截图文件", "screenshots", False, True),
    CleanerItem("archived", "archive", "归档会话", "archived", False, False),
    CleanerItem("share_sessions", "upload", "分享会话文件", "share/sessions", False, False),
    # 工具执行结果缓存:app/core/tool_result_persister.py 写入
    # {app_data_dir}/projects/{session_id}/tool-results/,属于可重建的离线缓存。
    CleanerItem("projects", "wrench", "工具缓存结果", "projects", True, True),
]


# ── 实用函数 ──────────────────────────────────────────────


def _format_size(n: int) -> str:
    """格式化字节大小，如 1234567 → '1.2 MB'"""
    if n < 0:
        return "N/A"
    if n >= 1073741824:
        return f"{n / 1073741824:.1f} GB"
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _calc_dir_size(path: Path, dir_mode: bool) -> int:
    """计算目录大小（含全部直接文件与子目录，递归统计）

    dir_mode 保留兼容：旧实现按模式二选一（只统计子目录或只统计直接文件），
    会漏算另一半内容；现在统一递归统计全部内容。
    """
    if not path.exists():
        return 0
    return _walk_dir_size(path)


def _walk_dir_size(path: Path) -> int:
    """递归计算目录总大小（迭代式 scandir，栈替代递归）

    Windows 上 entry.stat() 的 size 信息由目录枚举直接携带，
    相比 os.walk + os.path.getsize（每文件额外一次系统调用）快数倍。
    """
    total = 0
    stack = [str(path)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


# macOS libproc 句柄与 rusage_info_v4 结构（仅 darwin 使用，懒加载缓存）
_LIBPROC: Optional[ctypes.CDLL] = None


class _RUsageInfoV4(ctypes.Structure):
    """macOS proc_pid_rusage(RUSAGE_INFO_V4) 返回结构（字段布局来自 libproc.h）"""

    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_user_time_max", ctypes.c_uint64),
        ("ri_system_time_max", ctypes.c_uint64),
        ("ri_pkg_idle_wkups_max", ctypes.c_uint64),
        ("ri_interrupt_wkups_max", ctypes.c_uint64),
        ("ri_pageins_max", ctypes.c_uint64),
        ("ri_wired_size_max", ctypes.c_uint64),
        ("ri_resident_size_max", ctypes.c_uint64),
        ("ri_phys_footprint_max", ctypes.c_uint64),
        ("ri_proc_flags", ctypes.c_uint64),
        ("ri_average_dram_purgeable", ctypes.c_uint64),
        ("ri_average_compressed", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
        ("ri_idle_wkups", ctypes.c_uint64),
        ("ri_idle_wkups_max", ctypes.c_uint64),
        ("ri_counters", ctypes.c_uint64 * 6),
        # 实测(2026-11-09)：现代 macOS 内核对 RUSAGE_INFO_V4 实际写入 >208 字节，
        # 按旧 libproc.h 布局给足 208B 会被越界写坏堆(调用即返回正常、之后
        # 任意 gc/free 段错误)。加尾垫到 512B，读取字段(off 72)不受影响。
        ("_tail_pad", ctypes.c_uint64 * 32),
    ]


def _get_process_memory() -> Optional[int]:
    """获取当前进程内存占用（字节）。

    macOS 返回 phys_footprint（与活动监视器「内存」列同口径），
    Windows/Linux 返回 RSS。

    为什么 macOS 不能用 RSS（2026-11-09 实测）：libmalloc 释放内存走
    madvise(MADV_FREE_REUSABLE) 惰性回收，归还的页面进入内核 FREE 池后
    仍计入 resident_size——分配 200MB 再全部释放，footprint 从 212.9MB
    落回 9.8MB，而 RSS 恒为 224.0MB 纹丝不动。「释放内存」按钮的前后
    数字都基于本函数，读 RSS 用户永远看不到下降，误以为清理无效。
    """
    if sys.platform == "darwin":
        mem = _mac_phys_footprint()
        if mem is not None:
            return mem
    try:
        import psutil

        return psutil.Process(os.getpid()).memory_info().rss
    except Exception:
        return None


def _mac_phys_footprint() -> Optional[int]:
    """macOS：proc_pid_rusage 读 phys_footprint（真实物理占用，字节）。失败返 None。"""
    global _LIBPROC
    try:
        if _LIBPROC is None:
            _LIBPROC = ctypes.CDLL("/usr/lib/libproc.dylib")
            _LIBPROC.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            _LIBPROC.proc_pid_rusage.restype = ctypes.c_int
        ri = _RUsageInfoV4()
        if _LIBPROC.proc_pid_rusage(os.getpid(), 4, ctypes.byref(ri)) != 0:
            return None
        return int(ri.ri_phys_footprint)
    except Exception:
        return None


def _rmtree_force(path: Path) -> bool:
    """强制删除目录树（含只读文件）

    git clone 生成的 .git/objects/pack/* 默认带只读属性，Windows 上裸
    shutil.rmtree 会因 WinError 5 静默失败（ignore_errors 吞掉异常，目录
    原样残留——install_tmp 里 170+ 个历史安装残留的根因）。onexc 回调先
    chmod 清除只读位再重试删除。

    Returns:
        True 全部删除成功；False 有残留（如 .pyd/.dll 被进程占用）
    """
    if not path.exists():
        return True

    def _onerror(func, failed_path, excinfo):
        try:
            os.chmod(failed_path, stat.S_IWRITE)
            func(failed_path)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onexc=_onerror)
        return True
    except Exception:
        return False


def _delete_cache(path: Path, dir_mode: bool):
    """清空缓存目录全部内容（直接文件删除 + 子目录递归删除），目录本身保留

    dir_mode 保留兼容：旧实现按模式二选一（只删子目录或只删直接文件），
    导致另一半内容（cache 下直接文件 / logs 下子目录）永远清不掉。
    """
    if not path.exists():
        return
    try:
        for entry in path.iterdir():
            try:
                if entry.is_dir():
                    _rmtree_force(entry)
                else:
                    entry.unlink()
            except (OSError, PermissionError):
                pass
    except (OSError, PermissionError):
        pass


def _kill_qwebengine_processes() -> int:
    """终止残留的 QtWebEngine(Chromium) 孤儿子进程，归还其占用的内存。

    只清理「父进程已消失」的孤儿进程（DriFox 退出/崩溃后残留的"僵尸"
    Chromium 进程）。**绝不杀当前进程的后代**（2026-11-06）：主进程存活时，
    其 WebEngine renderer/GPU/network 子进程是正在渲染消息页面的活动资源，
    一并杀掉会把在途渲染内容直接清空。正常退出由 Qt 生命周期回收子进程；
    异常残留会变孤儿，由下方兜底捕获。误杀在途渲染的代价远大于漏杀几个
    孤儿的收益，取舍明确。

    先发 terminate 优雅退出，超时未退的再强杀（kill）。

    Returns:
        成功终止的进程数
    """
    try:
        import psutil
    except Exception:
        return 0

    pattern = re.compile(r"qtwebengine", re.IGNORECASE)
    self_pid = os.getpid()
    procs = []
    seen = set()

    # ── 全系统孤儿兜底（父进程已不存在的残留；父存活的进程一律不碰） ──
    def _fallback_iter_orphans():
        """tasklist/pgrep 失败或超时的回落：process_iter 全表迭代（慢，保底不劣化）。"""
        try:
            for p in psutil.process_iter(["pid", "name", "ppid"]):
                try:
                    if p.info["pid"] in seen:
                        continue
                    if not pattern.search(p.info["name"] or ""):
                        continue
                    ppid = p.info["ppid"]
                    if ppid in (0, 1) or not psutil.pid_exists(ppid):
                        procs.append(p)
                        seen.add(p.info["pid"])
                except Exception:
                    pass
        except Exception:
            pass

    # Windows：tasklist 子进程枚举（psutil.process_iter 全表迭代长持 GIL，
    # 打包版 watchdog 543ms / 源码版 9025ms 实证）。CSV 只取列 0（映像名）/
    # 列 1（PID），不依赖列数；候选（通常几十个）再 psutil.Process(pid)
    # 定点校验孤儿身份与 kill，不再全表迭代。
    # macOS：tasklist 是 Windows 命令必抛 FileNotFoundError，原实现每次
    # 回落 process_iter 全表（实测 9 秒）。pgrep -if 毫秒级出候选 pid。
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["pgrep", "-if", "qtwebengine"],
                capture_output=True,
                timeout=10,
            ).stdout
            for line in out.decode(errors="replace").splitlines():
                try:
                    pid = int(line.strip())
                except ValueError:
                    continue
                if pid in seen or pid == self_pid:
                    continue
                try:
                    p = psutil.Process(pid)
                    ppid = p.ppid()
                except Exception:
                    continue
                if ppid in (0, 1) or not psutil.pid_exists(ppid):
                    procs.append(p)
                    seen.add(pid)
        except Exception:
            _fallback_iter_orphans()  # pgrep 失败 → 回落全表路径
    else:
        try:
            raw = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                timeout=10,
                creationflags=_CREATE_NO_WINDOW,
            ).stdout
            text = raw.decode(locale.getpreferredencoding(False), errors="replace")
            for row in csv.reader(text.splitlines()):
                if len(row) < 2:
                    continue
                if not pattern.search(row[0] or ""):
                    continue
                try:
                    pid = int(row[1].strip())
                except ValueError:
                    continue
                if pid in seen or pid == self_pid:
                    continue
                try:
                    p = psutil.Process(pid)
                    ppid = p.ppid()
                except Exception:
                    continue
                if ppid in (0, 1) or not psutil.pid_exists(ppid):
                    procs.append(p)
                    seen.add(pid)
        except Exception:
            # tasklist 失败/超时 → 回落原 process_iter 全表路径（保底不劣化）
            _fallback_iter_orphans()

    if not procs:
        return 0

    killed = 0
    for p in procs:
        try:
            p.terminate()
            killed += 1
        except Exception:
            pass
    # 等待最多 2 秒优雅退出，未退出的强杀
    try:
        _gone, alive = psutil.wait_procs(procs, timeout=2)
    except Exception:
        alive = []
    for p in alive:
        try:
            p.kill()
            killed += 1
        except Exception:
            pass
    return killed


# ── 内存释放 ──────────────────────────────────────────


def _release_memory_light():
    """第一阶段：清理各类进程内缓存（无原生风险）。

    链路：
    1. 清理 DriFox 全局 LRU 缓存（HTML 渲染/token 估算/grep 编译等）
    2. 清理 Qt QPixmapCache
    3. 清理 importlib 缓存
    4. 清理 re 正则编译缓存 + linecache 行缓存
    """
    # ── 1. DriFox 内部 LRU 缓存 ──
    try:
        from app.plugins.sdk import caps_for

        purge = caps_for("cache.purge")
        if purge is not None:
            purge()
    except Exception:
        pass

    # ── 2. Qt 像素图缓存 ──
    try:
        from PyQt5.QtGui import QPixmapCache

        QPixmapCache.clear()
    except Exception:
        pass

    # ── 3. importlib 缓存（模块查找缓存） ──
    try:
        import importlib

        importlib.invalidate_caches()
    except Exception:
        pass

    # ── 4. re 正则 + linecache 行缓存 ──
    try:
        import re

        re.purge()
    except Exception:
        pass
    try:
        import linecache

        linecache.clearcache()
    except Exception:
        pass


def _release_memory_deep():
    """第二阶段：全代 GC + 工作集归还 OS + 清理孤儿 WebEngine 子进程（高危原生操作）。

    注意：调用前必须让主线程事件循环排干在途回调（见 cards._on_memory_release 的
    分拍设计）：本阶段的 gc.collect(2) 可能连带析构 Python 独占的 QObject，
    与 Chromium 后台线程正在派发的回调相撞会 access violation。

    Returns:
        (collected_objects, killed_procs)
    """
    import ctypes
    import gc
    import sys as _sys

    # ── 5. Python GC 全代回收 ──
    # gc.collect() 默认只收第 0 代；gc.collect(2) 收全部三代
    collected = gc.collect(2)

    # ── 6. 激进工作集归还 OS ──
    # 崩溃修复（2026-09-11）：删除原步骤 6a 的 HeapCompact(GetProcessHeap(), 0)。
    # 该调用在 UI 线程遍历并压缩进程默认堆，与 Chromium 十几个后台线程
    # （IO/GPU/worker/audio）的堆分配/释放直接竞态，是崩溃日志 crash_*.log 中
    # scanner.py:328 处 4 次 access violation 的直接崩点；且它只合并空闲块、
    # 几乎不归还内存给 OS，收益远小于下方工作集修剪。另外 GetProcessHeap 返回的
    # HANDLE 未声明 restype，64 位下存在被 c_int 截断成伪句柄的风险（P008 同款坑）。
    try:
        if _sys.platform == "win32":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            # 激进工作集修剪：SetProcessWorkingSetSize(-1, -1)
            #     移除进程最小工作集限制，让 Windows 积极换出未使用页面
            #     比 EmptyWorkingSet 更彻底——不再保留任何"锁住"的页面
            h = kernel32.GetCurrentProcess()
            kernel32.SetProcessWorkingSetSize(h, ctypes.c_size_t(-1).value, ctypes.c_size_t(-1).value)
        elif _sys.platform == "linux":
            libc = ctypes.CDLL("libc.so.6", use_last_error=True)
            libc.malloc_trim(0)
        elif _sys.platform == "darwin":
            # macOS：把滞留在 malloc zone free list 的空闲页推入内核 FREE 池。
            # libmalloc 在 free() 时已自动 madvise(MADV_FREE_REUSABLE)，footprint
            # 随对象释放自动下降（实测 relief 前后一致），本调用是 free list 兜底，
            # 与 win/linux 分支保持语义对齐。
            libc = ctypes.CDLL(None)
            libc.malloc_default_zone.restype = ctypes.c_void_p
            libc.malloc_zone_pressure_relief.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            libc.malloc_zone_pressure_relief(libc.malloc_default_zone(), 0)
    except Exception:
        pass

    # ── 7. 终止残留 QtWebEngine(Chromium) 孤儿子进程，归还其内存 ──
    killed_procs = _kill_qwebengine_processes()

    return collected, killed_procs

def _release_memory_deep_core() -> int:
    """第二阶段的可主线程部分：GS 全代回收 + 工作集归还 OS。

    从 ``_release_memory_deep`` 拆出来，供「GC 留主线程、psutil 进后台」的分拍
    调用（见 cards._on_memory_release）。

    为什么 GC 不放后台：``gc.collect(2)`` 回收期间会暂停**全部**线程（含主线程）
    直到回收完成，换到后台线程只是把停顿换个地方发生，主线程照样卡。真正的
    收益来自把 psutil 的全系统进程遍历（逐个 OpenProcess 取 pid/name/ppid，
    2026-10-31 实测主线程阻塞 501ms）挪到后台——那部分是真实的阻塞式 IO。

    Returns:
        gc.collect(2) 回收的对象数
    """
    import ctypes
    import gc
    import sys as _sys

    collected = gc.collect(2)

    # 工作集修剪（崩溃修复见 _release_memory_deep 的步骤 6 注释）
    try:
        if _sys.platform == "win32":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = kernel32.GetCurrentProcess()
            kernel32.SetProcessWorkingSetSize(h, ctypes.c_size_t(-1).value, ctypes.c_size_t(-1).value)
        elif _sys.platform == "linux":
            libc = ctypes.CDLL("libc.so.6", use_last_error=True)
            libc.malloc_trim(0)
        elif _sys.platform == "darwin":
            # macOS free list 兜底（原理见 _release_memory_deep 步骤 6 注释）
            libc = ctypes.CDLL(None)
            libc.malloc_default_zone.restype = ctypes.c_void_p
            libc.malloc_zone_pressure_relief.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            libc.malloc_zone_pressure_relief(libc.malloc_default_zone(), 0)
    except Exception:
        pass

    return collected


def _release_memory():
    """兼容入口：不分拍一次性执行两阶段（轻清理 → 深度释放）。

    推荐走 cards._on_memory_release 的分拍调用（light → 事件循环排干 → deep），
    单拍同步执行时全代 GC 与 Chromium 在途回调撞车的概率显著更高。

    Returns:
        (before_rss, after_rss, collected_objects, killed_procs)
    """
    import ctypes

    before = _get_process_memory()

    _release_memory_light()
    collected, killed_procs = _release_memory_deep()

    after = _get_process_memory()
    return before, after, collected, killed_procs


# ── 扫描工作器 ──────────────────────────────────────────


class _ScanWorker(QObject):
    """后台扫描所有缓存目录大小"""

    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, drifox_dir: Path):
        super().__init__()
        self._drifox_dir = drifox_dir
        self._cancelled = False

    def cancel(self):
        """协作式取消：置位后 run() 在下一个目录边界快速退出，不发 finished"""
        self._cancelled = True

    def run(self):
        try:
            sizes: Dict[str, int] = {}
            for item in CACHE_DEFS:
                if self._cancelled:
                    return
                full = self._drifox_dir / item.rel_path
                sizes[item.cid] = _calc_dir_size(full, item.dir_mode)
            if self._cancelled:
                return
            self.finished.emit(sizes)
        except Exception as e:
            self.error.emit(f"{e}\n{traceback.format_exc()}")


# ── 清理工作器 ──────────────────────────────────────────


class _CleanWorker(QObject):
    """后台执行文件删除"""

    progress = pyqtSignal(str)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, drifox_dir: Path, targets: List[CleanerItem]):
        super().__init__()
        self._drifox_dir = drifox_dir
        self._targets = targets

    def run(self):
        try:
            freed: Dict[str, int] = {}
            for item in self._targets:
                self.progress.emit(item.label)
                full = self._drifox_dir / item.rel_path
                before = _calc_dir_size(full, item.dir_mode)
                _delete_cache(full, item.dir_mode)
                after = _calc_dir_size(full, item.dir_mode)
                freed[item.cid] = before - after
            self.finished.emit(freed)
        except Exception as e:
            self.error.emit(f"{e}\n{traceback.format_exc()}")

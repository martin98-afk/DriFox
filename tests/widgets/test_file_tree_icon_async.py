# -*- coding: utf-8 -*-
"""file-tree 插件：图标后台加载 + 超时熔断回归测试

问题（2026-10-15 实锤）：``FileTreeModel.data(DecorationRole)`` 在主线程同步调
``QFileIconProvider.icon()`` → Win32 ``SHGetFileInfo()``。Windows Shell 对带文件
关联（本机 .csv→WPS 表格）的图标查询单次阻塞 5s 且超时结果不缓存；目录首次
展开 / 展开状态恢复时整批重查 —— qt_messages 日志 9 条
``SHGetFileInfo() timed out``（每条间隔精确 5s）与 ``[FileTree] expand 45226ms``
吻合，主线程整窗冻结 45s。历史同类两次：25092ms / 81297ms（均为 5s 整数倍）。

修复（方案 A+C）：
- **A 后台化**：``data()`` 只读缓存，未命中返占位图标 + QThreadPool 后台
  ctypes 直调 ``SHGetFileInfoW`` 取 HICON 转 QImage，信号回主线程转 QIcon
  入缓存 → dataChanged 刷新。
- **C 熔断**：真实查询连续 3 次超时 → 降级为 ``SHGFI_USEFILEATTRIBUTES``
  快速模式（按扩展名查注册表，实测 <1ms），冷却 60s 后放行恢复。
- **热重载安全**：旧 loader 退役保活（不强拆 QThreadPool —— 池子析构会同步
  等待在跑任务，实测把主线程冻结 >120s）。
"""

import importlib.util
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

import pytest

# FileTreeModel 是 QObject、且惰性起 QThreadPool：构造前必须有 QApplication
_QAPP = QApplication.instance() or QApplication(sys.argv)

_PLUGIN_UI = Path(__file__).resolve().parent.parent.parent / "plugins" / "file-tree" / "ui"


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tree_widget_mod = _load_module("file_tree_icon_async_ui.tree_widget", _PLUGIN_UI / "tree_widget.py")


# ── 测试替身 ──────────────────────────────────────────────


class _Entry:
    """scanner._DirEntry 的最小替身"""

    __slots__ = ("name", "path", "is_dir")

    def __init__(self, name, path, is_dir):
        self.name = name
        self.path = path
        self.is_dir = is_dir


def _slow_query(seconds):
    """模拟 Shell 病态：真实路径查询阻塞 seconds，快速模式立即返回"""

    def _query(path, fast):
        time.sleep(0.0 if fast else seconds)
        return None

    return _query


def _fake_query(image):
    def _query(path, fast):
        return image

    return _query


def _pump(ms):
    """泵事件循环 ms 毫秒（等后台任务 + 防抖广播落地）"""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


@pytest.fixture(autouse=True)
def _reset_icon_state(monkeypatch):
    """每个测试重置模块级单例与熔断状态，避免跨测试串扰

    ★ 收尾纪律：不能带「在跑的后台任务」退出。QThreadPool 析构会同步
      等待在跑任务（生产正是因此把主线程冻死），测试里同样会把进程挂住。
      故先 shutdown + waitForDone，再丢引用；退役名单里的实例一并排水。
    """
    monkeypatch.setattr(tree_widget_mod, "_ICON_LOADER", None)
    monkeypatch.setattr(tree_widget_mod, "_ICON_CACHE", {})
    monkeypatch.setattr(tree_widget_mod, "_BREAKER_SLOW_STREAK", 0)
    monkeypatch.setattr(tree_widget_mod, "_BREAKER_OPEN_UNTIL", 0.0)
    app = QApplication.instance()
    if app is not None and hasattr(app, "_drifox_file_tree_retired_loaders"):
        delattr(app, "_drifox_file_tree_retired_loaders")
    yield

    def _drain(obj):
        try:
            obj.shutdown()
            obj._pool.waitForDone(3000)
        except RuntimeError:
            pass

    loader = tree_widget_mod._ICON_LOADER
    if loader is not None:
        _drain(loader)
    tree_widget_mod._ICON_LOADER = None
    if app is not None:
        for old in getattr(app, "_drifox_file_tree_retired_loaders", []):
            _drain(old)
        if hasattr(app, "_drifox_file_tree_retired_loaders"):
            delattr(app, "_drifox_file_tree_retired_loaders")


def _make_model(tmp_path, n_files=3):
    model = tree_widget_mod.FileTreeModel()
    entries = []
    for i in range(n_files):
        f = tmp_path / f"f{i}.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        entries.append(_Entry(f.name, str(f), False))
    model.set_project(str(tmp_path), entries)
    return model


# ── 主线程零阻塞（核心不变量） ─────────────────────────────


class TestMainThreadNeverBlocks:
    """data(DecorationRole) 的耗时与 Shell 查询时长解耦"""

    def test_data_call_cost_independent_of_shell_slowness(self, monkeypatch, tmp_path):
        """Shell 每次卡 0.6s，主线程取 N 个图标仍须毫秒级

        修复前：本测试会退化为 N × 0.6s 的同步阻塞（生产实测 5s/文件）。
        """
        monkeypatch.setattr(tree_widget_mod, "_query_shell_icon", _slow_query(0.6))
        model = _make_model(tmp_path, n_files=3)

        t0 = time.perf_counter()
        for row in range(model.rowCount()):
            icon = model.data(model.index(row, 0), Qt.DecorationRole)
            assert icon is not None
        dt = time.perf_counter() - t0

        assert dt < 0.2, f"主线程被 Shell 查询拖慢: {dt * 1000:.0f}ms（应 <200ms）"

    def test_miss_returns_placeholder_not_none(self, monkeypatch, tmp_path):
        """缓存未命中必须同步返回可用图标（占位），不能返回 None 空行"""
        monkeypatch.setattr(tree_widget_mod, "_query_shell_icon", _slow_query(0.6))
        model = _make_model(tmp_path, n_files=1)
        icon = model.data(model.index(0, 0), Qt.DecorationRole)
        assert icon is not None and not icon.isNull()

    def test_same_path_reuses_cache_after_async_landing(self, monkeypatch, tmp_path):
        """后台结果落地后，同路径取值命中缓存（不再排任务）"""
        img = QImage(16, 16, QImage.Format_ARGB32)
        img.fill(QColor(10, 20, 30))
        calls = []

        def counting_query(path, fast):
            calls.append(path)
            return img

        monkeypatch.setattr(tree_widget_mod, "_query_shell_icon", counting_query)
        model = _make_model(tmp_path, n_files=1)
        path = model.index(0, 0).data(tree_widget_mod.FileTreeModel.COL_PATH)

        model.data(model.index(0, 0), Qt.DecorationRole)  # 首次：排任务
        _pump(600)  # 等任务 + 防抖广播
        assert path in tree_widget_mod._ICON_CACHE, "后台图标未落地"
        first_n = len(calls)

        icon = model.data(model.index(0, 0), Qt.DecorationRole)
        assert icon is not None and not icon.isNull()
        _pump(200)
        assert len(calls) == first_n, "命中缓存后不应再排查询任务"


# ── 熔断降级（方案 C） ────────────────────────────────────


class TestBreakerDegrades:
    """连续超时 → 全量转快速模式"""

    def test_breaker_opens_after_consecutive_slow_results(self, monkeypatch):
        monkeypatch.setattr(tree_widget_mod, "_SHELL_SLOW_SEC", 0.05)
        monkeypatch.setattr(tree_widget_mod, "_BREAKER_COOLDOWN_S", 30.0)
        loader = tree_widget_mod._get_icon_loader()

        for i in range(2):
            loader._on_task_done(f"p{i}", None, 0.2, False)
        assert tree_widget_mod._BREAKER_OPEN_UNTIL == 0.0, "未达阈值不应提前熔断"

        loader._on_task_done("p2", None, 0.2, False)
        assert tree_widget_mod._BREAKER_OPEN_UNTIL > time.monotonic(), "达阈值未打开熔断"

    def test_fast_result_resets_streak(self, monkeypatch):
        monkeypatch.setattr(tree_widget_mod, "_SHELL_SLOW_SEC", 0.05)
        loader = tree_widget_mod._get_icon_loader()
        loader._on_task_done("a", None, 0.2, False)  # 慢
        loader._on_task_done("b", None, 0.2, False)  # 慢
        loader._on_task_done("c", None, 0.01, False)  # 正常 → 计数清零
        loader._on_task_done("d", None, 0.2, False)  # 慢
        assert tree_widget_mod._BREAKER_OPEN_UNTIL == 0.0, "中间一次正常应打断连续计数"

    def test_breaker_routes_new_requests_to_fast(self, monkeypatch, tmp_path):
        """熔断打开期间，新的查询请求直接走快速模式"""
        monkeypatch.setattr(tree_widget_mod, "_BREAKER_OPEN_UNTIL", time.monotonic() + 30)
        modes = []

        def spy(path, fast):
            modes.append(fast)
            return None

        monkeypatch.setattr(tree_widget_mod, "_query_shell_icon", spy)
        model = _make_model(tmp_path, n_files=1)
        model.data(model.index(0, 0), Qt.DecorationRole)
        _pump(400)
        assert modes and all(modes), f"熔断期应全走 fast，实际: {modes}"

    def test_watchdog_flags_stuck_task(self, monkeypatch):
        """任务超时未回（后台被挂死）→ 看门狗摘除并按慢计

        ★ 直接操纵 _pending 时间戳模拟「在途且陈旧」，不真提交后台任务：
          真任务会调用 Shell，测试退出时与池子析构交互会挂住进程。
        """
        monkeypatch.setattr(tree_widget_mod, "_ICON_TASK_STALE_SEC", 0.01)
        monkeypatch.setattr(tree_widget_mod, "_SHELL_SLOW_SEC", 0.001)
        loader = tree_widget_mod._get_icon_loader()
        loader._pending["stuck_path"] = time.monotonic() - 1.0
        loader._check_stale()
        assert "stuck_path" in loader._stale_paths
        assert loader.can_request("stuck_path") is False, "摘除后不应重复提交"


# ── 异步落地与刷新 ────────────────────────────────────────


class TestAsyncLanding:
    def test_real_icon_replaces_placeholder(self, monkeypatch, tmp_path):
        img = QImage(16, 16, QImage.Format_ARGB32)
        img.fill(QColor(200, 100, 50))
        monkeypatch.setattr(tree_widget_mod, "_query_shell_icon", _fake_query(img))
        model = _make_model(tmp_path, n_files=1)
        path = model.index(0, 0).data(tree_widget_mod.FileTreeModel.COL_PATH)

        first = model.data(model.index(0, 0), Qt.DecorationRole)
        _pump(600)
        cached = tree_widget_mod._ICON_CACHE.get(path)
        assert cached is not None
        assert cached is not first, "后台真图应替换占位图标"

    def test_data_changed_emitted_for_decoration(self, monkeypatch, tmp_path):
        img = QImage(16, 16, QImage.Format_ARGB32)
        img.fill(QColor(1, 2, 3))
        monkeypatch.setattr(tree_widget_mod, "_query_shell_icon", _fake_query(img))
        model = _make_model(tmp_path, n_files=1)

        events = []
        model.dataChanged.connect(lambda tl, br, roles: events.append(list(roles or [])))
        model.data(model.index(0, 0), Qt.DecorationRole)
        _pump(600)

        assert events, "图标落地须发 dataChanged（否则视图不重绘）"
        assert any(Qt.DecorationRole in roles for roles in events)


# ── 热重载安全 ────────────────────────────────────────────


class TestHotReloadSafety:
    """热重载不得阻塞主线程

    ★ 用 threading.Event 闸门而非固定 sleep：任务必定在测试内被放行结束，
      避免把在跑任务留给下游（池子析构时会同步等待，挂住整个进程）。
    """

    def test_retire_does_not_wait_for_running_tasks(self, monkeypatch):
        """退役旧 loader 不得同步等待在跑任务

        修复前直接丢引用触发 QThreadPool 析构 → 主线程冻结（实测 >120s）。
        """
        gate = threading.Event()

        def gated(path, fast):
            gate.wait(10.0)
            return None

        monkeypatch.setattr(tree_widget_mod, "_query_shell_icon", gated)
        loader = tree_widget_mod._get_icon_loader()
        loader.submit("hot_reload_probe", False)
        time.sleep(0.1)  # 让任务真正进入 gated()

        app = QApplication.instance()
        t0 = time.perf_counter()
        tree_widget_mod._retire_loader(app, loader)
        dt = time.perf_counter() - t0

        gate.set()  # 放行任务
        try:
            loader._pool.waitForDone(3000)
        except RuntimeError:
            pass

        assert dt < 0.5, f"退役阻塞主线程 {dt * 1000:.0f}ms"
        assert getattr(app, "_drifox_file_tree_retired_loaders", None), "在跑实例须保活"

    def test_new_loader_does_not_block_on_stale_pool(self, monkeypatch):
        """新 loader 创建不被旧池子牵连"""
        gate = threading.Event()

        def gated(path, fast):
            gate.wait(10.0)
            return None

        monkeypatch.setattr(tree_widget_mod, "_query_shell_icon", gated)
        old = tree_widget_mod._get_icon_loader()
        old.submit("probe_a", False)
        old.submit("probe_b", False)
        time.sleep(0.1)

        tree_widget_mod._ICON_LOADER = None  # 模拟模块重载（全局归零）
        t0 = time.perf_counter()
        new_loader = tree_widget_mod._get_icon_loader()
        dt = time.perf_counter() - t0

        gate.set()
        try:
            old._pool.waitForDone(3000)
        except RuntimeError:
            pass

        assert dt < 0.5, f"新建 loader 阻塞 {dt * 1000:.0f}ms"
        assert new_loader is not old


# ── 非 Windows 回退路径 ───────────────────────────────────


class TestNonWindowsFallback:
    def test_sync_path_when_not_windows(self, monkeypatch, tmp_path):
        """非 Windows 保持同步 QFileIconProvider（无 Shell 超时问题）"""
        monkeypatch.setattr(tree_widget_mod, "_IS_WIN", False)
        monkeypatch.setattr(tree_widget_mod, "_ICON_CACHE", {})
        f = tmp_path / "a.py"
        f.write_text("x = 1", encoding="utf-8")

        t0 = time.perf_counter()
        icon = tree_widget_mod._get_file_icon(str(f))
        dt = time.perf_counter() - t0

        assert icon is not None and not icon.isNull()
        assert str(f) in tree_widget_mod._ICON_CACHE, "非 Windows 应同步入缓存"
        assert dt < 1.0

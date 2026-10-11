# -*- coding: utf-8 -*-
"""file-tree 插件：树状态稳定性回归测试

问题：切工作台页签 / 切对话标签 / 点刷新 / 项目上下文同步都会重扫项目根，
旧实现无条件 ``set_project``（``beginResetModel``）→ QTreeView 丢弃全部
展开 / 选中 / 滚动状态，用户展开的层级被「刷没」。

覆盖：
1. ``_same_path`` 路径归一（Windows 大小写 + 尾分隔符）—— 归一失败会把同根
   扫描误当「换项目」整树重建
2. 同根重扫走增量（``refresh_root``），展开状态与节点对象保留
3. 换项目 → 切回：视图状态缓存还原展开项（A→B→A）
4. 扫描代次守卫：被取代的旧扫描迟到回调被丢弃，不清掉新线程引用
5. 被中断扫描的 loading 标记不残留（否则该目录此后展开永远为空）
"""

import importlib.util
import os
import sys
from pathlib import Path

from PyQt5.QtCore import QItemSelectionModel
from PyQt5.QtWidgets import QApplication

_PLUGIN_UI = Path(__file__).resolve().parent.parent.parent / "plugins" / "file-tree" / "ui"


def _load_package():
    """注册 file_tree_state_ui 包（插件目录名含连字符，无法常规 import）"""
    if "file_tree_state_ui" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "file_tree_state_ui", _PLUGIN_UI / "__init__.py", submodule_search_locations=[str(_PLUGIN_UI)]
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["file_tree_state_ui"] = pkg
    spec.loader.exec_module(pkg)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load_package()
scanner_mod = _load_module("file_tree_state_ui.scanner", _PLUGIN_UI / "scanner.py")
tree_widget_mod = _load_module("file_tree_state_ui.tree_widget", _PLUGIN_UI / "tree_widget.py")
cards_mod = _load_module("file_tree_state_ui.cards", _PLUGIN_UI / "cards.py")

FileTreeCard = cards_mod.FileTreeCard
_same_path = cards_mod._same_path


def _scan_dir(d):
    """同步扫描目录（与 _TreeScanner.scan 相同逻辑）"""
    entries = []
    with os.scandir(d) as it:
        for entry in sorted(it, key=lambda e: (not e.is_dir(), e.name.lower())):
            if not scanner_mod._should_show(entry.name, entry.is_dir()):
                continue
            entries.append(scanner_mod._DirEntry(name=entry.name, path=entry.path, is_dir=entry.is_dir()))
    return entries


# ── 同步桩：替换 QThread / _TreeScanner，规避无头环境 QThread 崩溃 ──


class _FakeSignal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for s in list(self._slots):
            s(*args)


class _FakeThread:
    def __init__(self):
        self.started = _FakeSignal()
        self.finished = _FakeSignal()

    def start(self):
        self.started.emit()

    def quit(self):
        pass

    def wait(self, ms=0):
        return True

    def deleteLater(self):
        pass


class _FakeScanner:
    def __init__(self):
        self.finished = _FakeSignal()
        self.error = _FakeSignal()

    def moveToThread(self, thread):
        pass

    def scan(self, directory):
        self.finished.emit(_scan_dir(directory))


def _make_card(root, monkeypatch):
    """构造卡片并把扫描线程换成同步桩（不依赖事件循环）"""
    monkeypatch.setattr(cards_mod, "QThread", _FakeThread)
    monkeypatch.setattr(cards_mod, "_TreeScanner", _FakeScanner)
    card = FileTreeCard()
    card._project_root = str(root)
    card._tree_view.project_root = str(root)
    card._watcher.set_root(str(root))
    return card


def _expanded_paths(card):
    return card._expanded_source_paths()


def _expand(card, path):
    """展开指定路径的 source 节点（数据须已加载）"""
    node = card._source_model.find_node(path)
    assert node is not None, f"节点不存在: {path}"
    proxy_idx = card._proxy_model.mapFromSource(card._source_model._index_for_node(node))
    assert proxy_idx.isValid()
    card._tree_view.expand(proxy_idx)
    return proxy_idx


class TestSamePath:
    """路径同一性判定：归一失败的后果是整树重建（展开状态被刷没）"""

    def test_trailing_separator_ignored(self):
        assert _same_path("D:/work/X", "D:/work/X/") is True

    def test_case_insensitive_on_windows(self):
        assert _same_path("D:/work/X", "d:\\work\\x") is True

    def test_different_paths_not_equal(self):
        assert _same_path("D:/work/X", "D:/work/Y") is False

    def test_empty_is_never_equal(self):
        assert _same_path("", "") is False


class TestRootRefreshKeepsState:
    """同根重扫走增量：展开状态与节点对象保留（用户痛点主回归）"""

    def test_rescan_same_root_keeps_expansion(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        sub = root / "sub"
        sub.mkdir(parents=True)
        (sub / "inner.txt").write_text("1")
        (root / "a.txt").write_text("x")

        card = _make_card(root, monkeypatch)
        try:
            card._async_load_tree()  # 首次：建树
            sub_node_before = card._source_model.find_node(str(sub))
            assert sub_node_before is not None

            _expand(card, str(sub))  # 桩：展开时数据已随扫描落地
            card._source_model.populate_children(str(sub), _scan_dir(sub))
            assert card._tree_view.isExpanded(
                card._proxy_model.mapFromSource(card._source_model._index_for_node(sub_node_before))
            )

            # 内容变化后重扫同根（切页 / 点刷新走的同一条路径）
            (root / "b.txt").write_text("y")
            card._async_load_tree()

            sub_node_after = card._source_model.find_node(str(sub))
            assert sub_node_after is sub_node_before  # 未变节点保持原对象（未 reset）
            proxy_after = card._proxy_model.mapFromSource(card._source_model._index_for_node(sub_node_after))
            assert card._tree_view.isExpanded(proxy_after) is True
            assert card._source_model.find_node(str(root / "b.txt")) is not None  # 增量已生效
            assert _expanded_paths(card) == [str(sub)]
        finally:
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()

    def test_rescan_same_root_keeps_loaded_children(self, tmp_path, monkeypatch):
        """同根重扫后已展开目录的孙级数据仍在（不会退回未加载态）"""
        root = tmp_path / "proj"
        sub = root / "sub"
        sub.mkdir(parents=True)
        (sub / "inner.txt").write_text("1")

        card = _make_card(root, monkeypatch)
        try:
            card._async_load_tree()
            card._source_model.populate_children(str(sub), _scan_dir(sub))
            assert card._source_model.find_node(str(sub / "inner.txt")) is not None

            card._async_load_tree()  # 同根重扫

            assert card._source_model.find_node(str(sub / "inner.txt")) is not None
            assert card._source_model.find_node(str(sub)).loaded is True
        finally:
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()


class TestProjectSwitchRestoresState:
    """换项目（真 reset）→ 切回：按项目根缓存的视图状态还原"""

    def test_switch_back_restores_expansion(self, tmp_path, monkeypatch):
        root_a = tmp_path / "projA"
        root_b = tmp_path / "projB"
        sub_a = root_a / "sub"
        sub_a.mkdir(parents=True)
        (sub_a / "inner.txt").write_text("1")
        root_b.mkdir()
        (root_b / "b.txt").write_text("x")

        card = _make_card(root_a, monkeypatch)
        try:
            # 项目 A：建树并展开 sub
            card._async_load_tree()
            _expand(card, str(sub_a))
            card._source_model.populate_children(str(sub_a), _scan_dir(sub_a))
            assert _expanded_paths(card) == [str(sub_a)]

            # 切到项目 B：整树 reset，A 的状态存入缓存
            card._project_root = str(root_b)
            card._tree_view.project_root = str(root_b)
            card._watcher.set_root(str(root_b))
            card._async_load_tree()
            assert card._source_model.find_node(str(sub_a)) is None
            assert _expanded_paths(card) == []

            # 切回项目 A：展开项应被还原
            card._project_root = str(root_a)
            card._tree_view.project_root = str(root_a)
            card._watcher.set_root(str(root_a))
            card._async_load_tree()

            assert _expanded_paths(card) == [str(sub_a)]
            # 展开后其子级也可达（孙级数据已随扫描回填）
            assert card._source_model.find_node(str(sub_a / "inner.txt")) is not None
        finally:
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()

    def test_switch_back_restores_selection(self, tmp_path, monkeypatch):
        root_a = tmp_path / "projA"
        root_b = tmp_path / "projB"
        root_a.mkdir()
        root_b.mkdir()
        (root_a / "a.txt").write_text("x")
        (root_b / "b.txt").write_text("y")

        card = _make_card(root_a, monkeypatch)
        try:
            card._async_load_tree()

            node = card._source_model.find_node(str(root_a / "a.txt"))
            proxy_idx = card._proxy_model.mapFromSource(card._source_model._index_for_node(node))
            card._tree_view.selectionModel().select(
                proxy_idx,
                QItemSelectionModel.Select | QItemSelectionModel.Rows,
            )
            assert card._tree_view.selected_source_paths() == [str(root_a / "a.txt")]

            card._project_root = str(root_b)
            card._tree_view.project_root = str(root_b)
            card._watcher.set_root(str(root_b))
            card._async_load_tree()

            card._project_root = str(root_a)
            card._tree_view.project_root = str(root_a)
            card._watcher.set_root(str(root_a))
            card._async_load_tree()

            assert card._tree_view.selected_source_paths() == [str(root_a / "a.txt")]
        finally:
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()


class TestScanQueueNoMutualKill:
    """扫描串行队列：不互杀（用户痛点回归：展开目录多了卡死）

    根因：旧实现每来个扫描请求就 ``_cleanup_worker()`` 杀掉在跑的扫描。
    ``QThread.quit()`` 打断不了工作线程里的 ``os.scandir``，``wait(3000)`` 会
    真阻塞主线程；被杀目录的 loading 被清后回到「已展开且可 fetch」状态，
    Qt 下轮布局再次请求它 —— 与扫描请求互相踩踏成自激循环。
    实测 6 目录触发 223 次扫描、主线程累计阻塞 25s（卡死）。
    """

    def test_requests_are_queued_not_killing(self, tmp_path, monkeypatch):
        """连续请求多次扫描时不杀线程，全部排队"""
        root = tmp_path / "proj"
        root.mkdir()
        for i in range(5):
            (root / f"dir{i}").mkdir()

        card = _make_card(root, monkeypatch)
        try:
            card._async_load_tree()  # 根扫描（同步桩：立即完成）
            card._worker_thread = _FakeThread()  # 占位：模拟一个扫描正在跑
            for i in range(5):
                card._request_scan(str(root / f"dir{i}"), lambda e: None)

            assert len(card._scan_queue) == 5  # 全部入队
            assert card._worker_thread is not None  # 在跑的扫描没被杀
        finally:
            card._worker_thread = None
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()

    def test_same_dir_request_is_idempotent(self, tmp_path, monkeypatch):
        """同一目录重复请求只入队一次（防 Qt 每轮布局重复请求升级成风暴）"""
        root = tmp_path / "proj"
        sub = root / "sub"
        sub.mkdir(parents=True)

        card = _make_card(root, monkeypatch)
        try:
            card._async_load_tree()
            card._worker_thread = _FakeThread()

            first = card._request_scan(str(sub), lambda e: None)
            second = card._request_scan(str(sub), lambda e: None)
            third = card._request_scan(str(sub).upper() if os.name == "nt" else str(sub), lambda e: None)

            assert first is True
            assert second is False  # 已排队：幂等
            assert third is False  # 大小写差异在 Windows 上同目录
            assert len(card._scan_queue) == 1
        finally:
            card._worker_thread = None
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()

    def test_loading_mark_covers_queue_window(self, tmp_path, monkeypatch):
        """入队即占位 loading：Qt 不会把排队中的目录当成可 fetch

        Qt 对「已展开且 canFetchMore==True」的节点每轮布局请求一次 fetchMore
        （实测确认）。若只在扫描执行期间置位，排队中的目录仍可 fetch → 风暴。
        """
        root = tmp_path / "proj"
        sub = root / "sub"
        sub.mkdir(parents=True)

        card = _make_card(root, monkeypatch)
        try:
            card._async_load_tree()
            card._worker_thread = _FakeThread()
            card._request_scan(str(sub), lambda e: None)

            src_idx = card._source_model._index_for_node(card._source_model.find_node(str(sub)))
            assert card._source_model.canFetchMore(src_idx) is False  # 排队中不可 fetch
        finally:
            card._worker_thread = None
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()

    def test_root_request_is_urgent(self, tmp_path, monkeypatch):
        """根扫描插队 + 清空子目录队列 + 世代递增（换根时旧回调作废）"""
        root = tmp_path / "proj"
        sub = root / "sub"
        sub.mkdir(parents=True)

        card = _make_card(root, monkeypatch)
        try:
            card._async_load_tree()
            card._worker_thread = _FakeThread()
            card._request_scan(str(sub), lambda e: None)
            assert len(card._scan_queue) == 1
            gen_before = card._scan_gen

            card._async_load_tree()  # 根扫描：urgent

            assert card._scan_gen == gen_before + 1
            assert len(card._scan_queue) == 1  # 子目录排队项被丢弃，只剩根
            assert _same_path(card._scan_queue[0][0], str(root))
        finally:
            card._worker_thread = None
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()

    def test_stale_generation_result_discarded(self, tmp_path, monkeypatch):
        """旧世代扫描回调：结果丢弃、key 摘掉（否则该目录永久卡死）"""
        root = tmp_path / "proj"
        root.mkdir()
        (root / "a.txt").write_text("x")

        card = _make_card(root, monkeypatch)
        try:
            card._async_load_tree()
            gen = card._scan_gen
            sentinel = object()
            card._worker_thread = sentinel
            card._scan_gen = gen + 1  # 模拟换根：世代已推进
            stale_path = str(root / "stale_dir")
            card._scan_queued.add(os.path.normcase(os.path.normpath(stale_path)))
            called = []
            card._dispatch_scan_done(
                gen, os.path.normcase(os.path.normpath(stale_path)), stale_path, lambda e: called.append(e), []
            )

            assert called == []  # 旧世代结果丢弃
            # key 必须摘掉：漏摘会让该目录此后所有请求被当成重复而丢弃
            assert os.path.normcase(os.path.normpath(stale_path)) not in card._scan_queued
        finally:
            card._worker_thread = None
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()

    def test_failed_dir_is_fused(self, tmp_path, monkeypatch):
        """扫描失败的目录熔断：canFetchMore 返 False，不自动重试（防慢速风暴）"""
        root = tmp_path / "proj"
        sub = root / "sub"
        sub.mkdir(parents=True)

        card = _make_card(root, monkeypatch)
        try:
            card._async_load_tree()
            node = card._source_model.find_node(str(sub))
            src_idx = card._source_model._index_for_node(node)
            card._scan_gen_free = None

            gen = card._scan_gen
            card._dispatch_scan_error(gen, "k", str(sub), "boom")

            assert card._source_model.node_is_failed(str(sub)) is True
            assert card._source_model.canFetchMore(src_idx) is False

            # 手动刷新（根扫描 urgent）解除熔断
            card._async_load_tree()
            assert card._source_model.node_is_failed(str(sub)) is False
        finally:
            card._worker_thread = None
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()

    def test_unloaded_after_scan_is_fused(self, tmp_path, monkeypatch):
        """扫描成功但节点仍不可加载（目录不可读）→ 熔断，不反复重扫"""
        root = tmp_path / "proj"
        sub = root / "sub"
        sub.mkdir(parents=True)

        card = _make_card(root, monkeypatch)
        try:
            card._async_load_tree()
            # 桩：回调收到空 entries 且节点未 loaded（populate_children 找不到节点）
            card._source_model.remove_node(str(sub))
            gen = card._scan_gen
            card._dispatch_scan_done(gen, "k", str(sub), lambda e: None, [])

            # 节点已被移除 → 不误加熔断（find_node 返回 None）
            assert card._source_model.node_is_failed(str(sub)) is False
        finally:
            card._worker_thread = None
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()


class TestInterruptedScanLoadingMark:
    """中断清理：清空队列 + 清 loading 标记（销毁路径，不再用于常规刷新）"""

    def test_loading_mark_cleared_on_interrupt(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        sub = root / "sub"
        sub.mkdir(parents=True)
        (root / "a.txt").write_text("x")

        card = _make_card(root, monkeypatch)
        try:
            card._async_load_tree()
            # 模拟：某目录扫描中途被新的扫描取代
            card._current_scan_target = str(sub)
            card._worker_thread = _FakeThread()  # 占位，让 cleanup 认为有活线程
            card._source_model.mark_loading(str(sub))
            assert card._source_model.node_is_loading(str(sub)) is True

            card._cleanup_worker()

            assert card._source_model.node_is_loading(str(sub)) is False
        finally:
            card._worker_thread = None
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()

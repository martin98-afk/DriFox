# -*- coding: utf-8 -*-
"""file-tree 插件：根目录文件监听回归测试

问题：文件树对根目录没有监控（根目录新增/删除文件不刷新）
根因：
1. _on_dir_changed_externally 依赖 find_node 找对应节点，
   而根目录自身在 FileTreeModel 中没有节点（_root_entries 只含根下条目）
   → 根目录变更时直接 return，永不刷新
2. _DirWatcher 超过 _MAX_WATCH_PATHS 上限时淘汰最早添加的路径
   （即根目录），且无保护机制
"""

import importlib.util
import os
import sys
import time
from pathlib import Path

from PyQt5.QtWidgets import QApplication, QTreeView

_PLUGIN_UI = Path(__file__).resolve().parent.parent.parent / "plugins" / "file-tree" / "ui"


def _load_package():
    """注册 file_tree_ui 包（插件目录名含连字符，无法常规 import）"""
    if "file_tree_ui" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "file_tree_ui", _PLUGIN_UI / "__init__.py", submodule_search_locations=[str(_PLUGIN_UI)]
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["file_tree_ui"] = pkg
    spec.loader.exec_module(pkg)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load_package()
watcher_mod = _load_module("file_tree_ui.watcher", _PLUGIN_UI / "watcher.py")
scanner_mod = _load_module("file_tree_ui.scanner", _PLUGIN_UI / "scanner.py")
tree_widget_mod = _load_module("file_tree_ui.tree_widget", _PLUGIN_UI / "tree_widget.py")
cards_mod = _load_module("file_tree_ui.cards", _PLUGIN_UI / "cards.py")

_DirWatcher = watcher_mod._DirWatcher
FileTreeModel = tree_widget_mod.FileTreeModel
FileTreeFilterProxy = tree_widget_mod.FileTreeFilterProxy
FileTreeCard = cards_mod.FileTreeCard
_MAX_WATCH_PATHS = watcher_mod._MAX_WATCH_PATHS


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
# 说明：真实 QThread 异步扫描在 Python3.14 + PyQt5 无头环境会 STATUS_STACK_BUFFER_OVERRUN，
# 此处用同步桩验证回调链路逻辑。


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


class TestWatcherRootProtection:
    """_DirWatcher 监听上限淘汰时保护根目录"""

    def test_root_not_evicted_when_over_limit(self, tmp_path):
        w = _DirWatcher()
        root = tmp_path / "root"
        root.mkdir()
        w.set_root(str(root))
        w.add_path(str(root))

        # 添加超过上限数量的目录，触发淘汰
        for i in range(_MAX_WATCH_PATHS + 5):
            d = tmp_path / f"d{i:03d}"
            d.mkdir()
            w.add_path(str(d))

        assert len(w._watcher.directories()) <= _MAX_WATCH_PATHS
        assert str(root) in w._watcher.directories()

    def test_remove_path_clears_debounce_timer(self, tmp_path):
        w = _DirWatcher()
        d = tmp_path / "d"
        d.mkdir()
        w.add_path(str(d))
        w._on_dir_changed(str(d))
        assert str(d) in w._debounce_timers
        w.remove_path(str(d))
        assert str(d) not in w._debounce_timers


class TestModelRefreshRoot:
    """FileTreeModel.refresh_root 刷新根目录顶级条目"""

    def test_refresh_root_updates_top_level(self, tmp_path):
        model = FileTreeModel()
        root = tmp_path / "root"
        root.mkdir()
        (root / "a.txt").write_text("x")
        (root / "b.txt").write_text("y")
        model.set_project(str(root), _scan_dir(root))
        assert model.rowCount() == 2

        # 根目录新增文件
        (root / "c.txt").write_text("z")
        model.refresh_root(_scan_dir(root))
        assert model.rowCount() == 3
        assert model.find_node(str(root / "c.txt")) is not None

        # 根目录删除文件
        (root / "a.txt").unlink()
        model.refresh_root(_scan_dir(root))
        assert model.rowCount() == 2
        assert model.find_node(str(root / "a.txt")) is None


class TestCardRootChange:
    """FileTreeCard 根目录外部变更 → 重扫 → 模型更新（同步桩）"""

    def _make_card(self, root):
        card = FileTreeCard()
        card._project_root = str(root)
        card._tree_view.project_root = str(root)
        card._watcher.set_root(str(root))
        card._source_model.set_project(str(root), _scan_dir(root))
        return card

    def test_root_change_triggers_rescan(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cards_mod, "QThread", _FakeThread)
        monkeypatch.setattr(cards_mod, "_TreeScanner", _FakeScanner)
        root = tmp_path / "root"
        root.mkdir()
        (root / "a.txt").write_text("x")
        card = self._make_card(root)
        try:
            assert card._source_model.find_node(str(root / "a.txt")) is not None

            # 根目录新增文件 → 外部变更回调（同步桩直接完成扫描）
            (root / "b.txt").write_text("y")
            card._on_dir_changed_externally(str(root))
            assert card._source_model.find_node(str(root / "b.txt")) is not None
        finally:
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()

    def test_root_deleted_file_removed_from_model(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cards_mod, "QThread", _FakeThread)
        monkeypatch.setattr(cards_mod, "_TreeScanner", _FakeScanner)
        root = tmp_path / "root"
        root.mkdir()
        (root / "a.txt").write_text("x")
        (root / "b.txt").write_text("y")
        card = self._make_card(root)
        try:
            # 根目录删除文件 → 外部变更回调
            (root / "a.txt").unlink()
            card._on_dir_changed_externally(str(root))
            assert card._source_model.find_node(str(root / "a.txt")) is None
            assert card._source_model.find_node(str(root / "b.txt")) is not None
        finally:
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()

    def test_unloaded_child_dir_change_ignored(self, tmp_path, monkeypatch):
        """子目录未加载时外部变更不触发重扫（原逻辑不回退）"""
        monkeypatch.setattr(cards_mod, "QThread", _FakeThread)
        monkeypatch.setattr(cards_mod, "_TreeScanner", _FakeScanner)
        root = tmp_path / "root"
        root.mkdir()
        sub = root / "sub"
        sub.mkdir()
        (sub / "inner.txt").write_text("x")
        card = self._make_card(root)
        try:
            scans = []

            class _RecordingScanner(_FakeScanner):
                def scan(self, directory):
                    scans.append(directory)
                    super().scan(directory)

            monkeypatch.setattr(cards_mod, "_TreeScanner", _RecordingScanner)
            # sub 未展开（未加载），外部变更应被忽略
            card._on_dir_changed_externally(str(sub))
            assert scans == []
        finally:
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()


class TestModelIncrementalRefresh:
    """FileTreeModel 增量刷新 — 未变节点保持原引用（展开状态保留的前提）"""

    def test_refresh_root_keeps_unchanged_nodes(self, tmp_path):
        model = FileTreeModel()
        root = tmp_path / "root"
        root.mkdir()
        (root / "sub").mkdir()
        (root / "a.txt").write_text("x")
        (root / "b.txt").write_text("y")
        model.set_project(str(root), _scan_dir(root))

        sub_before = model.find_node(str(root / "sub"))
        a_before = model.find_node(str(root / "a.txt"))

        # 根目录新增 c.txt + 删除 b.txt → 增量刷新
        (root / "c.txt").write_text("z")
        (root / "b.txt").unlink()
        model.refresh_root(_scan_dir(root))

        assert model.find_node(str(root / "sub")) is sub_before  # 未变目录保留原对象
        assert model.find_node(str(root / "a.txt")) is a_before  # 未变文件保留原对象
        assert model.find_node(str(root / "b.txt")) is None
        assert model.find_node(str(root / "c.txt")) is not None

    def test_refresh_root_keeps_loaded_subdir_state(self, tmp_path):
        """根刷新后已展开子目录保持原节点 + loaded 状态（孙级数据不丢）"""
        model = FileTreeModel()
        root = tmp_path / "root"
        root.mkdir()
        sub = root / "sub"
        sub.mkdir()
        (sub / "inner.txt").write_text("1")
        model.set_project(str(root), _scan_dir(root))
        model.populate_children(str(sub), _scan_dir(sub))
        sub_node = model.find_node(str(sub))
        assert sub_node.loaded

        # 根目录新增文件 → 增量刷新根
        (root / "new.txt").write_text("n")
        model.refresh_root(_scan_dir(root))

        sub_after = model.find_node(str(sub))
        assert sub_after is sub_node
        assert sub_after.loaded is True
        # 孙级数据仍可通过原节点访问（未触发重扫）
        assert model.find_node(str(sub / "inner.txt")) is not None

    def test_refresh_children_keeps_unchanged_nodes(self, tmp_path):
        model = FileTreeModel()
        root = tmp_path / "root"
        root.mkdir()
        sub = root / "sub"
        sub.mkdir()
        (sub / "x.txt").write_text("1")
        (sub / "y.txt").write_text("2")
        model.set_project(str(root), _scan_dir(root))
        model.populate_children(str(sub), _scan_dir(sub))

        x_before = model.find_node(str(sub / "x.txt"))
        y_before = model.find_node(str(sub / "y.txt"))

        # sub 内新增 z.txt + 删除 y.txt → 增量刷新
        (sub / "z.txt").write_text("3")
        (sub / "y.txt").unlink()
        model.refresh_children(str(sub), _scan_dir(sub))

        assert model.find_node(str(sub / "x.txt")) is x_before
        assert model.find_node(str(sub / "y.txt")) is None
        assert model.find_node(str(sub / "z.txt")) is not None

    def test_refresh_children_when_empty(self, tmp_path):
        """刷新为空的目录：全部子节点被移除"""
        model = FileTreeModel()
        root = tmp_path / "root"
        root.mkdir()
        sub = root / "sub"
        sub.mkdir()
        (sub / "x.txt").write_text("1")
        model.set_project(str(root), _scan_dir(root))
        model.populate_children(str(sub), _scan_dir(sub))
        assert model.rowCount(model._index_for_node(model.find_node(str(sub)))) == 1

        (sub / "x.txt").unlink()
        model.refresh_children(str(sub), _scan_dir(sub))
        assert model.rowCount(model._index_for_node(model.find_node(str(sub)))) == 0


class TestViewExpandedStatePreserved:
    """刷新后展开状态保留（用户痛点回归：文件变化刷新导致整树折叠）"""

    def test_expanded_dir_survives_root_refresh(self, tmp_path):
        model = FileTreeModel()
        root = tmp_path / "root"
        root.mkdir()
        sub = root / "sub"
        sub.mkdir()
        (sub / "inner.txt").write_text("1")
        (root / "a.txt").write_text("x")

        proxy = FileTreeFilterProxy()
        proxy.setSourceModel(model)
        view = QTreeView()
        view.setModel(proxy)
        try:
            model.set_project(str(root), _scan_dir(root))
            sub_proxy = proxy.mapFromSource(model._index_for_node(model.find_node(str(sub))))
            view.expand(sub_proxy)  # 展开（触发 fetchMore，数据随后填充）
            model.populate_children(str(sub), _scan_dir(sub))
            assert view.isExpanded(sub_proxy) is True

            # 根目录新增文件 → 增量刷新根（旧实现 beginResetModel 会折叠整树）
            (root / "b.txt").write_text("y")
            model.refresh_root(_scan_dir(root))

            sub_after = model.find_node(str(sub))
            sub_proxy_after = proxy.mapFromSource(model._index_for_node(sub_after))
            assert view.isExpanded(sub_proxy_after) is True
        finally:
            view.deleteLater()
            QApplication.processEvents()

    def test_expanded_subdir_survives_child_refresh(self, tmp_path):
        """父目录刷新后，已展开的孙目录保持展开（旧实现清空重建会丢失）"""
        model = FileTreeModel()
        root = tmp_path / "root"
        root.mkdir()
        parent = root / "parent"
        parent.mkdir()
        sub = parent / "sub"
        sub.mkdir()
        (sub / "inner.txt").write_text("1")
        (parent / "a.txt").write_text("x")

        proxy = FileTreeFilterProxy()
        proxy.setSourceModel(model)
        view = QTreeView()
        view.setModel(proxy)
        try:
            model.set_project(str(root), _scan_dir(root))
            model.populate_children(str(parent), _scan_dir(parent))
            sub_proxy = proxy.mapFromSource(model._index_for_node(model.find_node(str(sub))))
            view.expand(sub_proxy)
            model.populate_children(str(sub), _scan_dir(sub))
            assert view.isExpanded(sub_proxy) is True

            # parent 内新增文件 → 增量刷新 parent
            (parent / "b.txt").write_text("y")
            model.refresh_children(str(parent), _scan_dir(parent))

            sub_after = model.find_node(str(sub))
            assert sub_after is not None
            sub_proxy_after = proxy.mapFromSource(model._index_for_node(sub_after))
            assert view.isExpanded(sub_proxy_after) is True
        finally:
            view.deleteLater()
            QApplication.processEvents()

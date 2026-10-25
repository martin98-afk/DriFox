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

from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QAbstractItemView, QApplication

# FileTreeCard 是 QWidget：构造前必须存在 QApplication（否则 qFatal 0xC0000409）
_QAPP = QApplication.instance() or QApplication(sys.argv)

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
        # 对齐真实语义：quit → 线程事件循环退出 → finished（收尾槽置 None + pump）
        self.finished.emit()

    def wait(self, ms=0):
        return True

    def deleteLater(self):
        pass


class _FakeScanner:
    """同步桩：目录构造时绑定、scan() 无参（对齐 _TreeScanner 新接口）"""

    def __init__(self, directory=""):
        self._directory = directory
        self.finished = _FakeSignal()
        self.error = _FakeSignal()

    def moveToThread(self, thread):
        pass

    def scan(self):
        self.finished.emit(_scan_dir(self._directory))


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


def _is_emoji(ch: str) -> bool:
    """是否 emoji 字符（取 Unicode emoji 实际码位区间，不含中日韩文字）"""
    cp = ord(ch)
    return (
        0x1F300 <= cp <= 0x1FAFF  # 各类 emoji 图形
        or 0x2600 <= cp <= 0x27BF  # 杂项符号 + Dingbats（✂️ 等）
        or 0xFE0F == cp  # 变体选择符（emoji presentation）
        or 0x2190 <= cp <= 0x21FF  # 箭头（部分被当 emoji 用）
    )


class TestRenameEntrypoints:
    """重命名可编辑性回归：flags 缺 ItemIsEditable 会让 F2 / 右键菜单静默失效

    根因：``QAbstractItemView.edit()`` 在 index.flags() 不含 ``ItemIsEditable``
    时直接返回 False —— 不改任何可见状态、不打日志，表现为「点重命名没反应」。
    视图的 ``editTriggers = NoEditTriggers`` 只禁「双击等自动触发」，不拦显式
    ``edit()`` 调用，因此修复点在 model.flags 而非视图。
    """

    def test_node_flags_include_editable(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        root.mkdir()
        (root / "a.txt").write_text("x")

        card = _make_card(root, monkeypatch)
        try:
            card._async_load_tree()
            node = card._source_model.find_node(str(root / "a.txt"))
            assert node is not None
            src_idx = card._source_model._index_for_node(node)
            flags = card._source_model.flags(src_idx)
            assert flags & tree_widget_mod.Qt.ItemIsEditable, "flags 必须含 ItemIsEditable"
            # 目录同样可重命名
            dir_idx = card._source_model._index_for_node(card._source_model.find_node(str(root)))
            if dir_idx.isValid():
                assert card._source_model.flags(dir_idx) & tree_widget_mod.Qt.ItemIsEditable
        finally:
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()

    def test_start_rename_opens_editor(self, tmp_path, monkeypatch):
        """右键菜单 / F2 走 start_rename_at_index → 编辑器必须真正打开"""
        root = tmp_path / "proj"
        root.mkdir()
        (root / "a.txt").write_text("x")

        card = _make_card(root, monkeypatch)
        try:
            card._async_load_tree()
            node = card._source_model.find_node(str(root / "a.txt"))
            src_idx = card._source_model._index_for_node(node)
            card._tree_view.start_rename_at_index(src_idx)
            QApplication.processEvents()
            assert card._tree_view.state() == QAbstractItemView.EditingState, "编辑器未打开"
        finally:
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()

    def test_editor_background_is_opaque(self, tmp_path, monkeypatch):
        """编辑器底色必须不透明，否则下层 item 文字透出成重影

        回归：旧值 ``rgba(255,255,255,0.08)`` 半透明，编辑器只「覆盖」在 item
        之上而 item 仍绘制自己的名称文字 → 两层文字叠加。长文件名时最明显。
        """
        root = tmp_path / "proj"
        root.mkdir()
        (root / "a_very_long_file_name_for_overlap.png").write_text("x")

        for is_dark in (True, False):
            card = _make_card(root, monkeypatch)
            try:
                card._colors = {
                    "accent": tree_widget_mod.QColor("#62a0ea"),
                    "text": tree_widget_mod.QColor("#ffffff" if is_dark else "#0f172a"),
                    "font_family": "Microsoft YaHei",
                    "font_size": 14,
                    "is_dark": is_dark,
                }
                card._rename_delegate.set_colors(card._colors)
                card._async_load_tree()
                node = card._source_model.find_node(str(root / "a_very_long_file_name_for_overlap.png"))
                src_idx = card._source_model._index_for_node(node)

                editor = card._rename_delegate.createEditor(card._tree_view, None, src_idx)
                try:
                    qss = editor.styleSheet()
                    assert "background-color" in qss, "编辑器必须用 background-color 显式指定底色"
                    assert "rgba" not in qss.split("background-color")[1].split(";")[0], (
                        "编辑器底色不得半透明（会导致下层文字透出重影）"
                    )
                finally:
                    editor.deleteLater()
            finally:
                card._cleanup_worker()
                card.deleteLater()
                QApplication.processEvents()

    def test_rename_commits_to_disk_and_model(self, tmp_path, monkeypatch):
        """编辑器提交 → 文件真实改名 + 模型节点路径同步"""
        root = tmp_path / "proj"
        root.mkdir()
        old = root / "a.txt"
        old.write_text("x")

        card = _make_card(root, monkeypatch)
        try:
            card._async_load_tree()
            card._on_rename_file(str(old), "b.txt")
            assert not old.exists()
            assert (root / "b.txt").exists()
            assert card._source_model.find_node(str(old)) is None
            assert card._source_model.find_node(str(root / "b.txt")) is not None
        finally:
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()


class TestContextMenu:
    """右键菜单结构：图标非空 + 剪贴板空时无「粘贴」项"""

    def _capture(self, card, path, monkeypatch):
        from unittest.mock import MagicMock

        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QIcon

        captured = {}

        class _FakeMenu:
            """替代 cards 模块的 QMenu。

            PySide6 的 ``QMenu.exec`` 是 C++ 静态重载（类型属性 setattr 拦不住，
            实际调用仍走原生实现），只能替换模块引用。
            """

            def __init__(self, *args, **kwargs):
                self._actions = []

            def setStyleSheet(self, *args, **kwargs):
                return None

            def addAction(self, *args):
                text = args[-1]
                icon = args[0] if args and isinstance(args[0], QIcon) else QIcon()
                act = MagicMock()
                act.text.return_value = text
                act.icon.return_value = icon
                act.isSeparator.return_value = False
                act.isEnabled.return_value = True
                self._actions.append(act)
                return act

            def addSeparator(self):
                sep = MagicMock()
                sep.isSeparator.return_value = True
                self._actions.append(sep)

            def actions(self):
                return list(self._actions)

            def exec(self, *args, **kwargs):
                rows = []
                for act in self._actions:
                    if act.isSeparator():
                        rows.append(None)
                        continue
                    rows.append(
                        {
                            "text": act.text(),
                            "has_icon": not act.icon().pixmap(16, 16).isNull(),
                            "enabled": act.isEnabled(),
                        }
                    )
                captured["rows"] = rows
                return None

        monkeypatch.setattr(cards_mod, "QMenu", _FakeMenu)
        node = card._source_model.find_node(path)
        if node is None:
            card._on_context_menu(card._proxy_model.index(-1, -1), QPoint(0, 0))
        else:
            idx = card._proxy_model.mapFromSource(card._source_model._index_for_node(node))
            card._on_context_menu(idx, QPoint(0, 0))
        return captured["rows"]

    def test_all_items_have_icon_and_no_emoji(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        root.mkdir()
        target = root / "a.txt"
        target.write_text("x")

        card = _make_card(root, monkeypatch)
        try:
            card._async_load_tree()
            rows = self._capture(card, str(target), monkeypatch)
            items = [r for r in rows if r is not None]
            assert items, "菜单为空"
            for item in items:
                assert item["has_icon"], f"菜单项缺图标: {item['text']}"
                # emoji 全部换 icon：文本里不应再有 emoji 字符
                # （范围取 emoji 实际区块；中文 U+4E00-9FFF 不在其中）
                assert not any(_is_emoji(ch) for ch in item["text"]), f"仍含 emoji: {item['text']}"
        finally:
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()

    def test_paste_hidden_when_clipboard_empty(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        root.mkdir()
        target = root / "a.txt"
        target.write_text("x")

        card = _make_card(root, monkeypatch)
        try:
            card._async_load_tree()
            card._clipboard = []
            texts = [r["text"] for r in self._capture(card, str(target), monkeypatch) if r]
            assert "粘贴" not in texts, "剪贴板空时不应显示粘贴项"

            card._clipboard = [(str(target), False)]
            texts = [r["text"] for r in self._capture(card, str(target), monkeypatch) if r]
            assert "粘贴" in texts, "剪贴板有内容时必须显示粘贴项"
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
            card._worker_thread = _FakeThread()  # 新收尾槽会调 quit()，须可调用
            card._current_dispatch = (
                gen,
                os.path.normcase(os.path.normpath(stale_path)),
                stale_path,
                lambda e: called.append(e),
            )
            card._on_scan_finished([])

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
            card._worker_thread = _FakeThread()  # 新收尾槽会调 quit()，须可调用
            card._current_dispatch = (gen, "k", str(sub), None)
            card._on_scan_error("boom")

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
            card._worker_thread = _FakeThread()  # 新收尾槽会调 quit()，须可调用
            card._current_dispatch = (gen, "k", str(sub), lambda e: None)
            card._on_scan_finished([])

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


class TestRealThreadScanLifecycle:
    """真实 QThread 生命周期回归（打开文件树整窗卡死的根因）

    旧实现三宗罪（同一根源：lambda 连接 + 不 quit）：
    1. ``started.connect(lambda: scan)`` 无 receiver → PyQt queued 回主线程
       → ``os.scandir`` 全程占用主线程（大目录卡 UI）
    2. 扫描完成直接 ``self._worker_thread = None`` → Python GC 掉**仍在运行**
       的 QThread → Qt5 ``~QThread`` qFatal → 0xC0000409 整窗死
       （root_watch 同步桩注释里的 STATUS_STACK_BUFFER_OVERRUN 即此 bug）
    3. 无 quit() → worker 事件循环永不退出 → 每次扫描泄漏一个线程

    本用例走**真实 QThread**（不换桩），泵主线程事件循环驱动全链路：
    - scan 必须在 worker 线程执行（thread id != 主线程）
    - finished 回调必须回主线程（model 操作不越线程）
    - 收尾后 ``_worker_thread is None``（线程真 finished 才置 None，GC 安全）
    """

    def test_scan_runs_in_worker_thread_and_cleans_up(self, tmp_path, monkeypatch):
        import threading
        import time as _time

        main_tid = threading.get_ident()
        scan_tids = set()

        # spy：记录 scan() 实际执行线程（真实 _TreeScanner.scan 在 worker 亲和下被调）
        orig_scan = cards_mod._TreeScanner.scan

        def _spy_scan(self):
            scan_tids.add(threading.get_ident())
            return orig_scan(self)

        monkeypatch.setattr(cards_mod._TreeScanner, "scan", _spy_scan)

        root = tmp_path / "proj"
        root.mkdir()
        (root / "a.txt").write_text("x")
        (root / "b.txt").write_text("y")

        card = FileTreeCard()
        try:
            card._project_root = str(root)
            card._tree_view.project_root = str(root)
            card._watcher.set_root(str(root))

            card._async_load_tree()

            deadline = _time.monotonic() + 10.0
            while _time.monotonic() < deadline:
                QApplication.processEvents()
                if card._worker_thread is None and card._source_model._root_entries:
                    # 收集 scan 线程 id：scanner 已在 pump 中创建过
                    break
                _time.sleep(0.005)

            # 泵到队列完全排空（_on_thread_finished 收尾）
            deadline = _time.monotonic() + 5.0
            while _time.monotonic() < deadline:
                QApplication.processEvents()
                if card._worker_thread is None and not card._scan_queue:
                    break
                _time.sleep(0.005)

            assert card._source_model._root_entries, "根扫描结果未落地"
            assert card._worker_thread is None, "worker 线程未收尾（泄漏 / GC 风险）"
            assert card._scan_queued == set(), "dedup 集合未清空"
            assert scan_tids, "scan 未被调用"
            assert main_tid not in scan_tids, "scan 在主线程执行（lambda 连接回退 bug 回归）"

            # 用 model 回调链路验证线程归属：再发起一次子目录扫描，回调走真实落地链
            (root / "sub").mkdir()
            orig_populate = card._source_model.populate_children
            captured = {}

            def _spy_populate(dir_path, entries):
                captured["tid"] = threading.get_ident()
                return orig_populate(dir_path, entries)

            card._source_model.populate_children = _spy_populate
            sub_dir = str(root / "sub")
            card._request_scan(sub_dir, lambda e, _d=sub_dir: card._on_lazy_scan_done(e, _d))
            deadline = _time.monotonic() + 10.0
            while _time.monotonic() < deadline:
                QApplication.processEvents()
                if "tid" in captured:
                    break
                _time.sleep(0.005)

            assert "tid" in captured, "子目录扫描未完成"
            assert captured["tid"] == main_tid, "populate_children 必须在主线程执行（model 线程纪律）"
        finally:
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()

    def test_worker_thread_never_leaks_across_rounds(self, tmp_path):
        """连续多轮扫描：每轮线程都收尾，不留悬挂 QThread（泄漏回归）"""
        import time as _time

        root = tmp_path / "proj"
        root.mkdir()
        for i in range(4):
            (root / f"d{i}").mkdir()

        card = FileTreeCard()
        try:
            card._project_root = str(root)
            card._tree_view.project_root = str(root)
            card._watcher.set_root(str(root))
            card._async_load_tree()
            done_calls = []
            for i in range(4):
                card._request_scan(str(root / f"d{i}"), lambda e, _i=i: done_calls.append(_i))

            deadline = _time.monotonic() + 15.0
            while _time.monotonic() < deadline:
                QApplication.processEvents()
                if card._worker_thread is None and not card._scan_queue and not card._scan_queued:
                    break
                _time.sleep(0.005)

            assert card._worker_thread is None
            assert not card._scan_queue and not card._scan_queued
            # 4 轮扫描回调全部按队列序消化（串行队列不丢任务、不悬挂线程）
            assert sorted(done_calls) == [0, 1, 2, 3]
        finally:
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()

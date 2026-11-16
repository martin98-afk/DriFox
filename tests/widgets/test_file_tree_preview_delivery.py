# -*- coding: utf-8 -*-
"""file-tree 插件：外部投递预览（preview_path）回归测试

覆盖「工具卡文件地址点击 → 文件树页预览」这条接管链的插件侧：
- 可预览类型（代码 / md / json / 图片）→ 进预览页，返回 True
- 不可预览类型（未知后缀 / 二进制）→ 返回 False，交宿主降级，且不切页
- 文件不存在 → 返回 False
- 目标在已加载树内 → 展开祖先 + 选中，返回树页时用户能看到自己的位置
- 目标在树外（项目外临时文件）→ 只进预览，不动树的展开状态

卡片构造用同步扫描桩（不依赖 QThread 与事件循环），与
``test_file_tree_state_stability.py`` 同一套做法。
"""

import importlib.util
import os
import sys
from pathlib import Path

from PyQt5.QtGui import QImage
from PyQt5.QtWidgets import QApplication

# FileTreeCard 是 QWidget：构造前必须存在 QApplication（否则 qFatal 0xC0000409）
_QAPP = QApplication.instance() or QApplication(sys.argv)

_PLUGIN_UI = Path(__file__).resolve().parent.parent.parent / "plugins" / "file-tree" / "ui"


def _load_package():
    if "file_tree_deliver_ui" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "file_tree_deliver_ui", _PLUGIN_UI / "__init__.py", submodule_search_locations=[str(_PLUGIN_UI)]
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["file_tree_deliver_ui"] = pkg
    spec.loader.exec_module(pkg)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load_package()
scanner_mod = _load_module("file_tree_deliver_ui.scanner", _PLUGIN_UI / "scanner.py")
_load_module("file_tree_deliver_ui.tree_widget", _PLUGIN_UI / "tree_widget.py")
cards_mod = _load_module("file_tree_deliver_ui.cards", _PLUGIN_UI / "cards.py")

FileTreeCard = cards_mod.FileTreeCard


def _scan_dir(d):
    entries = []
    with os.scandir(d) as it:
        for entry in sorted(it, key=lambda e: (not e.is_dir(), e.name.lower())):
            if not scanner_mod._should_show(entry.name, entry.is_dir()):
                continue
            entries.append(scanner_mod._DirEntry(name=entry.name, path=entry.path, is_dir=entry.is_dir()))
    return entries


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
        self.finished.emit()

    def wait(self, ms=0):
        return True

    def deleteLater(self):
        pass


class _FakeScanner:
    def __init__(self, directory=""):
        self._directory = directory
        self.finished = _FakeSignal()
        self.error = _FakeSignal()

    def moveToThread(self, thread):
        pass

    def scan(self):
        self.finished.emit(_scan_dir(self._directory))


def _make_card(root, monkeypatch):
    monkeypatch.setattr(cards_mod, "QThread", _FakeThread)
    monkeypatch.setattr(cards_mod, "_TreeScanner", _FakeScanner)
    card = FileTreeCard()
    card._project_root = str(root)
    card._tree_view.project_root = str(root)
    card._watcher.set_root(str(root))
    return card


def _cleanup(card):
    card._cleanup_worker()
    card.deleteLater()
    QApplication.processEvents()


class TestPreviewPathKinds:
    def test_text_file_opens_preview(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        root.mkdir()
        target = root / "a.py"
        target.write_text("print(1)\n", encoding="utf-8")
        card = _make_card(root, monkeypatch)
        try:
            assert card.preview_path(str(target)) is True
            assert card._stack.currentIndex() == cards_mod._PAGE_PREVIEW
            assert card._preview_page.current_path() == str(target)
        finally:
            _cleanup(card)

    def test_markdown_file_opens_preview(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        root.mkdir()
        target = root / "README.md"
        target.write_text("# 标题\n正文\n", encoding="utf-8")
        card = _make_card(root, monkeypatch)
        try:
            assert card.preview_path(str(target)) is True
            assert card._preview_page.current_kind() == "markdown"
        finally:
            _cleanup(card)

    def test_json_file_opens_preview(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        root.mkdir()
        target = root / "x.json"
        target.write_text('{"a": 1}', encoding="utf-8")
        card = _make_card(root, monkeypatch)
        try:
            assert card.preview_path(str(target)) is True
            assert card._preview_page.current_kind() == "text"
        finally:
            _cleanup(card)

    def test_image_file_opens_preview(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        root.mkdir()
        target = root / "shot.png"
        QImage(8, 8, QImage.Format_RGB32).save(str(target))
        card = _make_card(root, monkeypatch)
        try:
            assert card.preview_path(str(target)) is True
            assert card._preview_page.current_kind() == "image"
        finally:
            _cleanup(card)

    def test_unknown_kind_declines(self, tmp_path, monkeypatch):
        """未知后缀 → 不接管（返回 False，宿主继续交系统程序），且不切页"""
        root = tmp_path / "proj"
        root.mkdir()
        target = root / "archive.zzz"
        target.write_bytes(b"\x00\x01")
        card = _make_card(root, monkeypatch)
        try:
            before = card._stack.currentIndex()
            assert card.preview_path(str(target)) is False
            assert card._stack.currentIndex() == before
        finally:
            _cleanup(card)

    def test_binary_text_kind_declines(self, tmp_path, monkeypatch):
        """后缀在白名单但内容是二进制 → 预览失败返回 False（交宿主降级）"""
        root = tmp_path / "proj"
        root.mkdir()
        target = root / "fake.json"
        target.write_bytes(b"\x00\x00\x00\x00binary")
        card = _make_card(root, monkeypatch)
        try:
            assert card.preview_path(str(target)) is False
            assert card._stack.currentIndex() != cards_mod._PAGE_PREVIEW
        finally:
            _cleanup(card)

    def test_missing_file_declines(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        root.mkdir()
        card = _make_card(root, monkeypatch)
        try:
            assert card.preview_path(str(root / "nope.py")) is False
            assert card.preview_path("") is False
        finally:
            _cleanup(card)


class TestRevealInTree:
    def test_reveals_loaded_node_and_expands_ancestors(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        sub = root / "pkg"
        sub.mkdir(parents=True)
        target = sub / "mod.py"
        target.write_text("x = 1\n", encoding="utf-8")
        card = _make_card(root, monkeypatch)
        try:
            card._async_load_tree()  # 建树（桩：同步落地根条目）
            card._source_model.populate_children(str(sub), _scan_dir(sub))
            assert card.preview_path(str(target)) is True
            # 祖先已展开 + 目标被选中
            sub_idx = card._proxy_model.mapFromSource(card._source_model._index_for_node(
                card._source_model.find_node(str(sub))
            ))
            assert sub_idx.isValid() and card._tree_view.isExpanded(sub_idx) is True
            assert card._tree_view.selected_source_paths() == [str(target)]
        finally:
            _cleanup(card)

    def test_outside_tree_still_previews_without_touching_expansion(self, tmp_path, monkeypatch):
        """项目外文件：仍能预览，但不动树的展开状态"""
        root = tmp_path / "proj"
        sub = root / "pkg"
        sub.mkdir(parents=True)
        (sub / "mod.py").write_text("x = 1\n", encoding="utf-8")
        outside = tmp_path / "outside.py"
        outside.write_text("y = 2\n", encoding="utf-8")
        card = _make_card(root, monkeypatch)
        try:
            card._async_load_tree()
            expanded_before = card._expanded_source_paths()
            assert card.preview_path(str(outside)) is True
            assert card._stack.currentIndex() == cards_mod._PAGE_PREVIEW
            assert card._expanded_source_paths() == expanded_before
        finally:
            _cleanup(card)


class TestInitialLoadKeepsPreview:
    """首次根扫描结果晚于外部投递预览到达时，不得把预览页抢回树页"""

    def test_preview_survives_first_tree_load(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        root.mkdir()
        target = root / "a.py"
        target.write_text("print(1)\n", encoding="utf-8")
        card = _make_card(root, monkeypatch)
        try:
            # 先投递预览（树尚未建：model_root 为空 → 属于「初始装载」）
            assert card.preview_path(str(target)) is True
            assert card._stack.currentIndex() == cards_mod._PAGE_PREVIEW
            # 根扫描随后落地
            card._async_load_tree()
            assert card._stack.currentIndex() == cards_mod._PAGE_PREVIEW
            assert card._preview_page.current_path() == str(target)
        finally:
            _cleanup(card)

    def test_reveal_lands_after_first_tree_load(self, tmp_path, monkeypatch):
        """首点预览时树还没建：根扫描落地后必须补上定位，返回树页能看到选中"""
        root = tmp_path / "proj"
        root.mkdir()
        target = root / "a.py"
        target.write_text("print(1)\n", encoding="utf-8")
        card = _make_card(root, monkeypatch)
        try:
            assert card.preview_path(str(target)) is True
            assert card._tree_view.selected_source_paths() == []  # 此刻树是空的
            card._async_load_tree()
            assert card._tree_view.selected_source_paths() == [str(target)]
            card._close_preview()
            assert card._stack.currentIndex() == cards_mod._PAGE_TREE
            assert card._tree_view.selected_source_paths() == [str(target)]
        finally:
            _cleanup(card)

    def test_reveal_deep_path_scans_levels(self, tmp_path, monkeypatch):
        """深层文件：逐层扫描祖先目录，每层落地后再往前推一步，最终定位成功"""
        root = tmp_path / "proj"
        deep = root / "app" / "core" / "store"
        deep.mkdir(parents=True)
        target = deep / "repo.py"
        target.write_text("x = 1\n", encoding="utf-8")
        card = _make_card(root, monkeypatch)
        try:
            card._async_load_tree()  # 只加载根层：app 节点已建但未展开
            assert card._source_model.find_node(str(root / "app")) is not None
            assert card.preview_path(str(target)) is True
            # 桩扫描是同步的：每层请求立刻落地并递归推进，最终应已定位
            assert card._tree_view.selected_source_paths() == [str(target)]
        finally:
            _cleanup(card)

    def test_outside_path_clears_pending(self, tmp_path, monkeypatch):
        """项目外文件：定位无意义，待定位状态必须清掉，不能留着反复推进"""
        root = tmp_path / "proj"
        root.mkdir()
        outside = tmp_path / "outside.py"
        outside.write_text("y = 2\n", encoding="utf-8")
        card = _make_card(root, monkeypatch)
        try:
            card._async_load_tree()
            assert card.preview_path(str(outside)) is True
            assert card._pending_reveal_path == ""
        finally:
            _cleanup(card)

    def test_real_project_switch_still_leaves_preview(self, tmp_path, monkeypatch):
        """真换项目（model 已有根）仍退出旧预览，避免跨项目串台"""
        root_a = tmp_path / "projA"
        root_b = tmp_path / "projB"
        root_a.mkdir()
        root_b.mkdir()
        (root_a / "a.py").write_text("a = 1\n", encoding="utf-8")
        card = _make_card(root_a, monkeypatch)
        try:
            card._async_load_tree()
            assert card.preview_path(str(root_a / "a.py")) is True
            assert card._stack.currentIndex() == cards_mod._PAGE_PREVIEW
            # 换成另一个项目根
            card._project_root = str(root_b)
            card._tree_view.project_root = str(root_b)
            card._watcher.set_root(str(root_b))
            card._async_load_tree()
            assert card._stack.currentIndex() != cards_mod._PAGE_PREVIEW
        finally:
            _cleanup(card)


class TestPluginOpenHook:
    """插件注册的 opener 闭包：页面缺失 / 异常都降级返回 False"""

    def test_can_open_matches_previewable_kinds(self, tmp_path):
        ui_mod = sys.modules["file_tree_deliver_ui"]
        py = tmp_path / "a.py"
        py.write_text("x", encoding="utf-8")
        zz = tmp_path / "a.zzz"
        zz.write_bytes(b"\x00")
        assert ui_mod._can_open(str(py)) is True
        assert ui_mod._can_open(str(zz)) is False
        assert ui_mod._can_open(str(tmp_path / "missing.py")) is False

    def test_open_file_degrades_when_page_missing(self, monkeypatch):
        ui_mod = sys.modules["file_tree_deliver_ui"]
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry()
        monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
        monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
        monkeypatch.setattr(reg, "open_workbench_page", lambda page_id: None)
        assert ui_mod._open_file("whatever.py") is False

    def test_open_file_degrades_on_exception(self, monkeypatch):
        ui_mod = sys.modules["file_tree_deliver_ui"]
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry()
        monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
        monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))

        def _boom(page_id):
            raise RuntimeError("宿主没就绪")

        monkeypatch.setattr(reg, "open_workbench_page", _boom)
        assert ui_mod._open_file("whatever.py") is False

    def test_open_file_calls_page_preview(self, monkeypatch):
        ui_mod = sys.modules["file_tree_deliver_ui"]
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        seen = []

        class _Page:
            def preview_path(self, path):
                seen.append(path)
                return True

        reg = UIPluginRegistry()
        monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
        monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
        monkeypatch.setattr(reg, "open_workbench_page", lambda page_id: _Page())
        assert ui_mod._open_file("D:/x/a.py") is True
        assert seen == ["D:/x/a.py"]

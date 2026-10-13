# -*- coding: utf-8 -*-
"""file-tree 插件：主题签名缓存 + 图标缓存回归测试

问题：切工作台页签 / 项目联动每次都走 ``refresh_data → _apply_latest_theme``，
无条件对整棵 QTreeView ``setStyleSheet`` + ``setFont`` —— 触发整树
unpolish/polish + 全量重绘，树 2000+ 行时单次 40-130ms（实测），表现为
「打开 / 切回文件树页经常卡一下」。

修复：``_apply_latest_theme`` 产出（样式串 + 字体）签名缓存，与上次全等则
整体跳过 set；``_get_file_icon`` 按路径缓存（重绘高频复用，冷类别文件
Shell 取图 1.9ms/次）。
"""

import importlib.util
import sys
from pathlib import Path

from PyQt5.QtWidgets import QApplication

# FileTreeCard 是 QWidget：构造前必须存在 QApplication（否则 qFatal 0xC0000409）
_QAPP = QApplication.instance() or QApplication(sys.argv)

_PLUGIN_UI = Path(__file__).resolve().parent.parent.parent / "plugins" / "file-tree" / "ui"


def _load_package():
    if "file_tree_theme_ui" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "file_tree_theme_ui", _PLUGIN_UI / "__init__.py", submodule_search_locations=[str(_PLUGIN_UI)]
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["file_tree_theme_ui"] = pkg
    spec.loader.exec_module(pkg)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load_package()
scanner_mod = _load_module("file_tree_theme_ui.scanner", _PLUGIN_UI / "scanner.py")
tree_widget_mod = _load_module("file_tree_theme_ui.tree_widget", _PLUGIN_UI / "tree_widget.py")
cards_mod = _load_module("file_tree_theme_ui.cards", _PLUGIN_UI / "cards.py")

FileTreeCard = cards_mod.FileTreeCard

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
        self.finished.emit([])

# stub 全局滚动条样式（真实 get_unified_scrollbar_style 首调 ~1.7s，测试不承受）
import types  # noqa: E402

if "app.utils.design_tokens" not in sys.modules or not hasattr(
    sys.modules.get("app.utils.design_tokens"), "_ft_stubbed"
):
    stub = types.ModuleType("app.utils.design_tokens")
    stub._ft_stubbed = True
    stub.get_unified_scrollbar_style = lambda width=6: f"QScrollBar:vertical {{ width: {width}px; }}"
    sys.modules["app.utils.design_tokens"] = stub


def _make_ctx(**overrides):
    ctx = {
        "project_root": str(Path(__file__).parent.parent.parent),
        "font_family": "Microsoft YaHei",
        "font_size": 14,
        "is_dark": True,
        "plugin_icon": {},
        "colors": {},
    }
    ctx.update(overrides)
    return ctx


def _make_card(monkeypatch):
    card = FileTreeCard()
    card.set_context(_make_ctx())

    # 计数 setStyleSheet / setFont（挂在真实对象上）
    calls = {"style": 0, "font": 0}
    orig_style = card._tree_view.setStyleSheet
    orig_font = card._tree_view.setFont

    def counting_style(ss):
        calls["style"] += 1
        return orig_style(ss)

    def counting_font(f):
        calls["font"] += 1
        return orig_font(f)

    monkeypatch.setattr(card._tree_view, "setStyleSheet", counting_style)
    monkeypatch.setattr(card._tree_view, "setFont", counting_font)
    return card, calls


class TestThemeSignature:
    """主题签名缓存：同 ctx 重复应用不再触碰整树样式"""

    def test_same_ctx_skips_all_sets(self, monkeypatch):
        card, calls = _make_card(monkeypatch)
        try:
            card._apply_latest_theme()  # 首次：必须真正 set
            assert calls["style"] >= 1
            first_style = calls["style"]
            first_font = calls["font"]
            assert first_font >= 1

            card._apply_latest_theme()  # 同 ctx：全跳过
            card._apply_latest_theme()  # 再来一次
            assert calls["style"] == first_style, "同 ctx 二次应用不应再 setStyleSheet"
            assert calls["font"] == first_font, "同 ctx 二次应用不应再 setFont"
        finally:
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()

    def test_font_change_reapplies(self, monkeypatch):
        card, calls = _make_card(monkeypatch)
        try:
            card._apply_latest_theme()
            first_style = calls["style"]

            card.set_context(_make_ctx(font_family="Consolas", font_size=15))
            card._apply_latest_theme()
            assert calls["style"] > first_style, "字体变化必须重新应用样式"

            # 换回同 ctx 后再次稳定
            stable = calls["style"]
            card._apply_latest_theme()
            assert calls["style"] == stable
        finally:
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()

    def test_dark_mode_change_reapplies(self, monkeypatch):
        card, calls = _make_card(monkeypatch)
        try:
            card._apply_latest_theme()
            first = calls["style"]
            card.set_context(_make_ctx(is_dark=False))
            card._apply_latest_theme()
            assert calls["style"] > first, "明暗切换必须重新应用样式"
        finally:
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()

    def test_refresh_data_stable_after_first(self, monkeypatch):
        """切页协议入口：第二次 refresh_data 起不再触碰树样式（高频路径零成本）"""
        card, calls = _make_card(monkeypatch)
        try:
            card._async_load_tree()
            card._apply_latest_theme()
            after_first = calls["style"]
            card.refresh_data()
            assert calls["style"] == after_first, "refresh_data 不应重复应用未变化的主题"
        finally:
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()


class TestExpandQueueTimeout:
    """恢复队列超时跳过：队首目录扫描迟迟不回时不堵死整队"""

    def test_timeout_skips_stuck_head(self, monkeypatch, tmp_path):
        root = tmp_path / "proj"
        sub = root / "sub"
        sub.mkdir(parents=True)

        monkeypatch.setattr(cards_mod, "QThread", _FakeThread)
        monkeypatch.setattr(cards_mod, "_TreeScanner", _FakeScanner)
        card = FileTreeCard()
        card._project_root = str(root)
        card._tree_view.project_root = str(root)
        card._watcher.set_root(str(root))
        try:
            card._async_load_tree()
            QApplication.processEvents()  # 桩同步落地根扫描

            # 挂一个未加载目录在队首，模拟扫描永不回调
            stuck = str(sub)
            card._source_model.mark_loading(stuck)  # 占位 loading：node.loaded 保持 False
            card._pending_expand = [stuck, str(root)]
            monkeypatch.setattr(cards_mod, "_EXPAND_WAIT_TIMEOUT_S", 0.0)  # 立即超时
            card._pending_expand_since = 1.0  # 已等待很久
            card._process_expand_queue()

            assert stuck not in card._pending_expand, "超时队首应被跳过"
            assert str(root) in card._pending_expand or not card._pending_expand, "后续目录应继续推进"
        finally:
            card._worker_thread = None
            card._cleanup_worker()
            card.deleteLater()
            QApplication.processEvents()


class TestFileIconCache:
    """图标缓存：同路径重绘复用同一 QIcon，不重复走 QFileInfo + Shell"""

    def test_same_path_same_instance(self, monkeypatch, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1", encoding="utf-8")
        p = str(f)
        ic1 = tree_widget_mod._get_file_icon(p)
        ic2 = tree_widget_mod._get_file_icon(p)
        assert ic1 is ic2, "同路径必须命中缓存返回同一 QIcon 实例"

    def test_cache_bounded(self, monkeypatch, tmp_path):
        # 超限清空后仍能正常取图（不崩溃、返回非空图标）
        tree_widget_mod._ICON_CACHE.clear()
        for i in range(tree_widget_mod._ICON_CACHE_MAX + 1):
            tree_widget_mod._get_file_icon(str(tmp_path / f"f{i}.py"))
        assert len(tree_widget_mod._ICON_CACHE) <= tree_widget_mod._ICON_CACHE_MAX
        ic = tree_widget_mod._get_file_icon(str(tmp_path / "f0.py"))
        assert ic is not None and not ic.isNull()

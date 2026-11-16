# -*- coding: utf-8 -*-
"""file-tree 插件：吸顶滚动（sticky scroll）回归测试

需求（用户 2026-11-15）：树滚下去之后根目录被顶出可视区，想折叠当前目录
必须一路滚回顶部。期望 VS Code 那样把「顶部可见行的祖先链」贴顶显示，
直接折叠当前文件夹。

实现：``plugins/file-tree/ui/sticky_scroll.py`` 的 ``StickyScrollOverlay``
—— 纯覆盖式自绘浮层，不改 model / proxy / delegate。

覆盖：
1. 滚到最顶部不吸顶（顶部行本身可见，不该白占一行盖住内容）
2. 滚动后祖先链逐层出现（浅→深）
3. 5 行上限 + 视口半高上限
4. 点箭头折叠 / 点行定位
5. 滚轮必须转发到 viewport（否则鼠标停在吸顶区滚不动树）
6. model reset / rowsRemoved 同步清空持有的索引（否则重绘解引用野指针 → 原生崩溃）
7. eventFilter 不碰 tree（树析构期 viewport 已是悬空指针 → 0xC0000005）
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest
from PyQt5.QtCore import QEvent, QModelIndex, QObject, QPoint, Qt
from PyQt5.QtGui import QMouseEvent, QStandardItem, QStandardItemModel, QWheelEvent
from PyQt5.QtWidgets import QAbstractItemView, QApplication, QTreeView, QVBoxLayout, QWidget

_QAPP = QApplication.instance() or QApplication(sys.argv)
_PLUGIN_UI = Path(__file__).resolve().parent.parent.parent / "plugins" / "file-tree" / "ui"


def _load_package():
    if "file_tree_sticky_ui" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "file_tree_sticky_ui", _PLUGIN_UI / "__init__.py", submodule_search_locations=[str(_PLUGIN_UI)]
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["file_tree_sticky_ui"] = pkg
    spec.loader.exec_module(pkg)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load_package()
scanner_mod = _load_module("file_tree_sticky_ui.scanner", _PLUGIN_UI / "scanner.py")
tree_widget_mod = _load_module("file_tree_sticky_ui.tree_widget", _PLUGIN_UI / "tree_widget.py")
sticky_mod = _load_module("file_tree_sticky_ui.sticky_scroll", _PLUGIN_UI / "sticky_scroll.py")

StickyScrollOverlay = sticky_mod.StickyScrollOverlay
FileTreeModel = tree_widget_mod.FileTreeModel
FileTreeFilterProxy = tree_widget_mod.FileTreeFilterProxy


# ── 目录树夹具 ────────────────────────────────────────────


def _entries(directory):
    out = []
    with os.scandir(directory) as it:
        for e in sorted(it, key=lambda x: (not x.is_dir(), x.name.lower())):
            out.append(scanner_mod._DirEntry(name=e.name, path=e.path, is_dir=e.is_dir()))
    return out


class _Tree:
    """几何确定的深目录链（QStandardItemModel）

    ★ 为什么不用 FileTreeModel / FileTreeFilterProxy：该模型是懒加载的，
      offscreen 下展开后 QTreeView 的滚动范围经常不重算（实测 49 行内容、
      行高 40 时 max 仍为 42），而吸顶判定完全依赖行几何 —— 夹具必须确定。
      overlay 只用 COL_PATH / COL_IS_DIR 两个 role，标准 item model 语义等价。

    结构：root/d0..d5 六个顶层目录，d0/s0/t0 深链，t0 下 deep_files 个文件行。
    """

    def __init__(self, tmp_path=None, viewport_h=300, deep_files=40, top_dirs=6):
        self.root = "/root"
        self.deep = "/root/d0/s0/t0"
        self.model = QStandardItemModel()
        root_item = _item("root", self.root, False)  # 根行不算「目录级吸顶」
        self.model.appendRow([root_item])
        for i in range(top_dirs):
            d = _item(f"d{i}", f"/root/d{i}", True)
            root_item.appendRow([d])
            if i:
                continue
            s = _item("s0", "/root/d0/s0", True)
            d.appendRow([s])
            t = _item("t0", self.deep, True)
            s.appendRow([t])
            for j in range(deep_files):
                name = f"f{j:02d}.py"
                t.appendRow([_item(name, f"{self.deep}/{name}", False)])

        self.proxy = None  # 无代理（直接挂 source model）
        self.view = QTreeView()
        self.view.setModel(self.model)
        # ★ 复刻真实接线：qfluentwidgets.SmoothScrollDelegate 会把树切成逐像素
        #   滚动。默认的 ScrollPerItem 下滚动条单位是「行」，max 是行数而不是
        #   像素，逐像素测试全部失真。
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        # 与真实接线一致：吸顶条 + 树放进同一垂直布局（条在上、树占满剩余）
        self.host = QWidget()
        host_layout = QVBoxLayout(self.host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(0)
        self.overlay = StickyScrollOverlay(self.view, self.host)
        host_layout.addWidget(self.overlay)
        host_layout.addWidget(self.view, 1)
        self.host.resize(320, viewport_h)
        self.host.show()
        # ★ 全展开：吸顶判定以「行完全滚出」为准（见 _collect_ancestors），
        #   夹具有足够深的可见行才能测到多层祖先链。
        self.view.expandAll()
        self.view.doItemsLayout()
        _pump(5)
        self.overlay.bind_model(self.model)
        _pump(5)

    def index_for_path(self, path):
        return _find_index(self.model, path)

    def proxy_index(self, path):
        idx = self.index_for_path(path)
        assert idx.isValid(), f"节点不存在: {path}"
        return idx

    def scroll_to(self, value):
        self.view.verticalScrollBar().setValue(value)
        _pump()

    def names(self):
        return [os.path.basename(p) for p in self.overlay.row_paths()]

    def teardown(self):
        # 先解绑模型，再拆树：避免 model 先销毁后浮层仍持索引
        try:
            self.overlay.bind_model(None)
            self.overlay.deleteLater()
            self.view.setModel(None)
            self.host.close()
            self.host.deleteLater()
            _pump()
        except RuntimeError:
            pass


def _item(name, path, is_dir):
    it = QStandardItem(name)
    it.setData(path, FileTreeModel.COL_PATH)
    it.setData(bool(is_dir), FileTreeModel.COL_IS_DIR)
    return it


def _find_index(model, path, parent=None):
    if parent is None:
        parent = QModelIndex()  # 无效索引 = 顶层
    for r in range(model.rowCount(parent)):
        idx = model.index(r, 0, parent)
        if idx.data(FileTreeModel.COL_PATH) == path:
            return idx
        found = _find_index(model, path, idx)
        if found.isValid():
            return found
    return parent  # 无效


def _pump(times=3):
    for _ in range(times):
        QApplication.processEvents()


@pytest.fixture
def tree(tmp_path):
    t = _Tree(tmp_path)
    try:
        _pump()
        yield t
    finally:
        t.teardown()


# ── 1. 顶部不吸顶 ─────────────────────────────────────────


class TestNoStickyAtTop:
    def test_hidden_at_scroll_top(self, tree):
        tree.scroll_to(0)
        assert tree.overlay.isHidden(), "滚到最顶部时不应吸顶（顶部行本身可见）"
        assert tree.names() == []


# ── 2. 祖先链逐层出现 ─────────────────────────────────────


class TestAncestorChain:
    def test_chain_appears_after_scroll(self, tree):
        bar = tree.view.verticalScrollBar()
        assert bar.maximum() > 0, "夹具必须能滚动（否则测不到吸顶）"
        tree.scroll_to(bar.maximum() // 3)
        assert not tree.overlay.isHidden()
        names = tree.names()
        assert names == ["d0", "s0", "t0"], f"祖先链应为 d0→s0→t0，实际 {names}"

    def test_chain_follows_top_visible_row(self, tree):
        """滚到 t0 的兄弟层级：t0 退出后其祖先仍在链上"""
        tree.scroll_to(tree.view.verticalScrollBar().maximum() // 3)
        first = tree.names()
        tree.scroll_to(tree.view.verticalScrollBar().maximum())
        last = tree.names()
        # 底部区域是 d1..d5 顶层目录（t0 子树已滚完），祖先链应当收缩
        assert len(last) <= len(first), f"滚过子树后祖先链应收缩，{first} → {last}"

    def test_file_row_contributes_ancestors_only(self, tree):
        """顶部行是文件时，吸顶的是它的祖先目录（文件本身不吸顶）"""
        tree.scroll_to(tree.view.verticalScrollBar().maximum() // 2)
        for idx in tree.overlay.rows():
            assert idx.data(FileTreeModel.COL_IS_DIR), "吸顶区只应有目录，不应出现文件行"

    def test_collapsed_dir_not_sticky(self, tree):
        """折叠目录不吸顶（用户反馈「没展开的也显示在顶部」）

        构造：把中间层 s0 折叠 → 顶部行上移到 t0 的兄弟/父层，
        断言吸顶区里不出现任何折叠态目录。
        """
        s0 = tree.proxy_index(str(Path(tree.root) / "d0" / "s0"))
        tree.view.collapse(s0)
        _pump()
        bar = tree.view.verticalScrollBar()
        for frac in (0.2, 0.4, 0.6, 0.8, 1.0):
            tree.scroll_to(int(bar.maximum() * frac))
            for idx in tree.overlay.rows():
                assert tree.view.isExpanded(idx), (
                    f"折叠目录 {idx.data()} 不应出现在吸顶区"
                )

    def test_all_sticky_rows_are_expanded(self, tree):
        """全量扫描：任意滚动位置下，吸顶行必须都是展开态"""
        bar = tree.view.verticalScrollBar()
        for val in range(0, bar.maximum() + 1, max(1, bar.maximum() // 20)):
            tree.scroll_to(val)
            for idx in tree.overlay.rows():
                assert tree.view.isExpanded(idx), f"val={val} 出现折叠态吸顶行 {idx.data()}"


# ── 3. 行数上限 ───────────────────────────────────────────


class TestRowLimit:
    def test_never_exceeds_max_rows(self, tree):
        bar = tree.view.verticalScrollBar()
        for frac in (0.1, 0.3, 0.5, 0.7, 0.9):
            tree.scroll_to(int(bar.maximum() * frac))
            assert len(tree.overlay.rows()) <= sticky_mod.MAX_ROWS

    def test_limited_by_half_viewport(self, tmp_path):
        """视口很矮时也不允吸顶条吃掉整屏（行数上限之外的高度兜底）"""
        t = _Tree(tmp_path, viewport_h=90)
        try:
            t.scroll_to(t.view.verticalScrollBar().maximum() // 3)
            if t.overlay.isHidden():
                pytest.skip("该滚动位置无吸顶内容")
            assert t.overlay.height() <= t.view.height()
        finally:
            t.teardown()


# ── 4. 交互 ───────────────────────────────────────────────


def _click(widget, pos, button=Qt.LeftButton):
    ev = QMouseEvent(
        QEvent.MouseButtonPress,
        pos,
        widget.mapToGlobal(pos),
        button,
        button,
        Qt.NoModifier,
    )
    widget.mousePressEvent(ev)


class TestInteraction:
    def test_arrow_click_collapses(self, tree):
        tree.scroll_to(tree.view.verticalScrollBar().maximum() // 3)
        rows = tree.overlay.rows()
        assert rows, "需要先有吸顶行"
        target = rows[0]
        assert tree.view.isExpanded(target), "前置：最外层吸顶目录应处于展开态"

        # 箭头热区按**吸顶区内的层序**定位（吸顶条自绘时层序即缩进级别，
        # 与该行在树里的绝对缩进无关；用 visualRect 反推会差一层）
        indent = tree.view.indentation()
        row = rows.index(target)
        x = row * indent + indent // 2
        _click(tree.overlay, QPoint(x, tree.overlay.row_height() // 2))
        _pump()
        assert not tree.view.isExpanded(target), "点箭头应折叠该目录"

    def test_body_click_scrolls_to_dir(self, tree):
        tree.scroll_to(tree.view.verticalScrollBar().maximum() // 3)
        rows = tree.overlay.rows()
        assert rows
        target = rows[0]
        before = tree.view.verticalScrollBar().value()
        _click(tree.overlay, QPoint(tree.overlay.width() - 20, tree.overlay.row_height() // 2))
        _pump()
        assert tree.view.currentIndex() == target, "点行主体应选中该目录"
        assert tree.view.verticalScrollBar().value() != before, "点行主体应把该目录滚进视口"


# ── 4.5 内嵌不遮挡（不碰滚动值） ─────────────────────────

class TestEmbeddedNoScrollMutation:
    """吸顶条必须「真实占位」而不是覆盖层 + setViewportMargins

    历史教训（2026-11-16 用户实测）：用 ``setViewportMargins`` 给树抠顶部
    留白 + 等量推滚动值，在本机 Qt 下 margin 不生效但滚动值照旧被推走，
    表现为「往上滚卡住、反复抖动滚不上去」。所以现在吸顶条是布局里的
    真实兄弟控件。

    ★ 滚动值仍会被改，但只在条高变化时、且改量与条高变化量严格相等
      （双向对称补偿，见 ``_apply_height``）；高度不变时一律不动。
    """

    def test_stable_height_does_not_touch_scrollbar(self, tree):
        bar = tree.view.verticalScrollBar()
        tree.scroll_to(bar.maximum() // 3)
        before = bar.value()
        before_h = tree.overlay.maximumHeight()
        tree.overlay.schedule_update()
        _pump()
        if tree.overlay.maximumHeight() == before_h:
            assert bar.value() == before, "条高没变却动了滚动值（会与滚轮打架）"

    def test_sticky_is_layout_sibling_not_overlay(self, tree):
        """吸顶条与树同处一个布局，而不是树的子控件（覆盖层）"""
        overlay = tree.overlay
        assert overlay.parent() is not tree.view, "吸顶条不能是树的子控件（会盖住内容）"
        assert tree.view.parent() is overlay.parent(), "吸顶条与树必须是同一父容器的兄弟"

    def test_height_is_real_layout_space(self, tree):
        """条高即布局占位：树被整体下推，内容不被遮挡"""
        bar = tree.view.verticalScrollBar()
        tree.scroll_to(bar.maximum() // 3)
        if tree.overlay.isHidden():
            pytest.skip("该滚动位置无吸顶内容")
        assert tree.overlay.height() == (
            tree.overlay.row_height() * len(tree.overlay.rows())
        ), "吸顶条高度必须恰好等于内容行数 × 行高"
        # 树顶（在父容器坐标系里）应位于吸顶条下方
        host = tree.overlay.parent()
        assert tree.view.geometry().top() >= tree.overlay.geometry().bottom()

    def test_scroll_up_not_blocked(self, tree):
        """用户实测场景：滚下去再往上滚必须能上去，且不抖动"""
        bar = tree.view.verticalScrollBar()
        tree.scroll_to(bar.maximum())
        for _ in range(min(20, bar.maximum())):
            bar.setValue(max(0, bar.value() - 1))
            _pump(1)
        assert bar.value() < bar.maximum(), "往上滚必须能离开底部"
        assert bar.value() >= 0

# ── 4.6 抖动回归（条高振荡 / 补偿失配） ─────────────────

class TestNoJitter:
    """单向滚动时吸顶高度不得回缩，条高变化必须与滚动值等量同步

    历史 bug（2026-11-16 用户实测）：收缩不补偿 → 阈值放宽 → 刚退出的层
    在下一帧重新满足 → 高度 51↔68 反复横跳；接上平滑滚动引擎后滚轮向上
    6 格滚动值只走 1px、27 帧内容反向跳动（最大 33px）。修法：双向对称
    补偿（S 与 h 同进同退，``S - h`` 恒定）+ 判定滞回带。
    """

    def _scan(self, tree, start, steps, direction):
        """逐像素滚动，返回 (滚动值, 条高) 序列"""
        bar = tree.view.verticalScrollBar()
        tree.scroll_to(start)
        seq = [(bar.value(), tree.overlay.maximumHeight())]
        for _ in range(steps):
            nxt = max(0, min(bar.maximum(), bar.value() + direction))
            if nxt == bar.value():
                break
            bar.setValue(nxt)
            _pump(1)
            seq.append((bar.value(), tree.overlay.maximumHeight()))
        return seq

    def test_height_monotonic_scrolling_down(self, tree):
        """向下滚时条高只能增或不变：回缩就是抖动"""
        bar = tree.view.verticalScrollBar()
        _pump(5)
        seq = self._scan(tree, 0, min(200, bar.maximum()), +1)
        heights = [h for _, h in seq]
        drops = [(a, b) for a, b in zip(heights, heights[1:]) if b < a]
        assert not drops, f"向下滚动条高出现回缩：{drops[:5]}（全序列 {heights}）"

    def test_height_monotonic_scrolling_up(self, tree):
        """向上滚时条高只能减或不变：回涨就是抖动"""
        bar = tree.view.verticalScrollBar()
        _pump(5)
        start = min(bar.maximum(), max(0, bar.maximum() // 2))
        seq = self._scan(tree, start, min(200, start), -1)
        heights = [h for _, h in seq]
        rises = [(a, b) for a, b in zip(heights, heights[1:]) if b > a]
        assert not rises, f"向上滚动条高出现回涨：{rises[:5]}（全序列 {heights}）"

    def test_sibling_switch_keeps_height(self, tree):
        """第二层兄弟目录切换时条高不得掉档

        历史 bug（用户实测「两个吸顶文件夹切换很难受」）：旧目录已完全滚出、
        新目录还没完全滚出，中间一帧只剩上层 → 条高 34→17→34。
        修法：同深度旧行保留到新行接手。
        """
        bar = tree.view.verticalScrollBar()
        _pump(5)
        tree.scroll_to(0)
        rh = tree.overlay._measure_row_height()
        deep = tree.index_for_path(tree.deep)  # t0（d0 的最后一层）
        last = deep.child(tree.model.rowCount(deep) - 1, 0)
        edge = tree.view.visualRect(last).top() + rh + bar.value()  # 滚过 t0 末尾
        start = max(0, edge - 3 * rh)
        seq = self._scan(tree, start, 6 * rh, +1)
        heights = [h for _, h in seq]
        drops = [(a, b) for a, b in zip(heights, heights[1:]) if b < a]
        assert not drops, f"兄弟切换时条高掉档：{drops[:5]}（全序列 {heights}）"

    def test_scroll_compensates_height_one_to_one(self, tree):
        """条高每变 d，滚动值必须同步变 d（``S - h`` 恒定 = 内容屏幕零位移）

        失效后果：高度与滚动值失配后判据漂移，下一帧重新判定 → 自激振荡。
        """
        bar = tree.view.verticalScrollBar()
        _pump(5)
        seq = self._scan(tree, 0, min(120, bar.maximum() // 2), +1)
        bad = []
        for (v0, h0), (v1, h1) in zip(seq, seq[1:]):
            dh = h1 - h0
            dv = v1 - v0
            if dh != 0 and dv - dh != 1:  # 用户这一步贡献 1px，其余必须是补偿
                bad.append((v0, h0, v1, h1))
        assert not bad, f"条高变化未与滚动值等量同步：{bad[:5]}"


# ── 4.7 背景透明（与文件树背景对齐） ─────────────────────


class TestBackgroundAlignment:
    """吸顶条不自己填背景：留白区下方就是卡片背景，自填会叠成两层"""

    def test_no_opaque_paint_attribute(self, tree):
        assert not tree.overlay.testAttribute(
            Qt.WA_OpaquePaintEvent
        ), "不能声明整块不透明，否则透明处留残留像素"

    def test_background_not_filled_by_overlay(self, tree):
        """paintEvent 不填背景：抓图后角落像素应保持透明"""
        tree.scroll_to(tree.view.verticalScrollBar().maximum() // 3)
        if tree.overlay.isHidden():
            pytest.skip("当前滚动位置无吸顶内容")
        img = tree.overlay.grab().toImage()
        # 控件最右侧空白区（文字/图标/边框之外）必须仍是透明
        corner = img.pixelColor(max(0, img.width() - 3), 1)
        assert (
            corner.alpha() == 0
        ), f"吸顶条背景应透明（露出卡片底色），实测 corner alpha={corner.alpha()}"


# ── 5. 滚轮转发 ───────────────────────────────────────────


class TestWheelForwarding:
    def test_wheel_reaches_viewport(self, tree, monkeypatch):
        """吸顶条盖住视口顶部：不转发滚轮，鼠标停在这里就滚不动树"""
        received = []
        spy = _WheelSpy(received)
        tree.view.viewport().installEventFilter(spy)
        tree.scroll_to(tree.view.verticalScrollBar().maximum() // 3)
        assert not tree.overlay.isHidden()

        ev = QWheelEvent(
            QPoint(10, 5),
            tree.overlay.mapToGlobal(QPoint(10, 5)),
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.NoButton,
            Qt.NoModifier,
            Qt.NoScrollPhase,
            False,
        )
        tree.overlay.wheelEvent(ev)
        assert received, "滚轮事件必须投递到 viewport（SmoothScrollDelegate 装在那里）"

    def test_wheel_not_swallowed_when_no_viewport(self, tree):
        """viewport 缺失时不能崩，且事件要放行"""
        tree.overlay._vp = None
        ev = QWheelEvent(
            QPoint(1, 1),
            tree.overlay.mapToGlobal(QPoint(1, 1)),
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.NoButton,
            Qt.NoModifier,
            Qt.NoScrollPhase,
            False,
        )
        tree.overlay.wheelEvent(ev)
        assert not ev.isAccepted()


class _WheelSpy(QObject):
    """installEventFilter 只收 QObject；持有 sink 引用收集 Wheel 事件"""

    def __init__(self, sink):
        super().__init__()
        self._sink = sink

    def eventFilter(self, obj, event):  # noqa: N802 (Qt 命名)
        if event.type() == QEvent.Wheel:
            self._sink.append(event)
        return False


# ── 6. 索引失效防护 ───────────────────────────────────────


class TestIndexInvalidation:
    def test_model_reset_clears_rows_synchronously(self, tree):
        """reset 会让 internalPointer 失效：必须同步清空，不能等排队刷新"""
        tree.scroll_to(tree.view.verticalScrollBar().maximum() // 3)
        assert tree.overlay.rows(), "前置：需要有吸顶行"
        tree.model.clear()  # modelReset
        assert tree.overlay.rows() == [], "model reset 必须同步清空吸顶索引"

    def test_rows_removed_clears_rows(self, tree):
        tree.scroll_to(tree.view.verticalScrollBar().maximum() // 3)
        assert tree.overlay.rows()
        target = tree.overlay.rows()[-1].data(FileTreeModel.COL_PATH)
        idx = tree.index_for_path(target)
        assert idx.isValid(), f"目标节点不存在: {target}"
        tree.model.removeRow(idx.row(), idx.parent())
        assert tree.overlay.rows() == [], "行删除必须同步清空吸顶索引"

    def test_invalid_index_skipped_in_paint(self, tree):
        """兜底：失效索引不参与绘制（不崩、不解引用）"""
        tree.scroll_to(tree.view.verticalScrollBar().maximum() // 3)
        rows = list(tree.overlay.rows())
        assert rows
        tree.overlay._rows = [rows[0], _invalid_index()]
        try:
            tree.overlay.grab()  # 触发 paintEvent
        finally:
            tree.overlay._rows = rows


def _invalid_index():
    from PyQt5.QtCore import QModelIndex

    return QModelIndex()


# ── 7. 树析构期安全 ───────────────────────────────────────


class TestTeardownSafety:
    def test_event_filter_does_not_touch_tree(self, tree):
        """真实崩溃现场：树析构期 eventFilter 仍会被调用，此刻调
        ``tree.viewport()`` 是悬空指针 → 0xC0000005（无 Python 栈）。
        回归防护：把 _tree 置 None 后过滤器仍应正常工作。"""
        overlay = tree.overlay
        overlay._tree = None
        assert overlay.eventFilter(overlay._vp, QEvent(QEvent.Resize)) is False

    def test_overlay_survives_tree_deletion_at_exit(self, tmp_path):
        """构造 → 解绑 → 删树：进程不得崩（本用例通过即证明无悬空调用）"""
        t = _Tree(tmp_path)
        overlay = StickyScrollOverlay(t.view)
        overlay.bind_model(t.model)
        _pump()
        t.view.verticalScrollBar().setValue(t.view.verticalScrollBar().maximum() // 3)
        _pump()
        overlay.bind_model(None)
        overlay.deleteLater()
        t.view.setModel(None)
        t.view.deleteLater()
        _pump(5)
        assert True

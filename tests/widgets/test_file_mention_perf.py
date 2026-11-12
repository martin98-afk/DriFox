# -*- coding: utf-8 -*-
"""FileMentionCard 性能优化的回归测试

覆盖三处优化的正确性（性能数字不作断言，只锁行为）：

1. gitignore 匹配改用 IgnoreRules 预编译规则后，必须与旧的逐条 fnmatch
   实现判定完全一致——这里把旧实现作为 oracle 逐字保留，用于对照。
2. 初始渲染量由 RENDER_CHUNK(200) 降为 INITIAL_RENDER_COUNT 后，
   _filtered_items 仍必须是全量匹配结果，不能因少建 widget 而丢结果。
3. 懒加载：滚动/键盘向下时能继续追加，直到渲染完 _filtered_items。
"""

import fnmatch
import os
import sys

from PyQt5.QtWidgets import QApplication

from app.widgets.cards.floating.file_mention_card import (
    INITIAL_RENDER_COUNT,
    MAX_RENDERED_ITEMS,
    _scan_tree,
    FileMentionCard,
    FileMentionItemWidget,
    IgnoreRules,
)


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


def _ref_is_ignored(rel_path: str, is_dir: bool, patterns) -> bool:
    """旧实现的逐字复刻，作为 gitignore 判定的正确性基准（oracle）

    优化前的实现对每个 entry 逐条调用 fnmatch.fnmatch 并记录最后一条命中，
    本函数完整保留该语义，用于验证 IgnoreRules 未改变任何判定结果。
    """
    path_parts = rel_path.replace("\\", "/").split("/")
    name = path_parts[-1]
    name_lower = name.lower()

    for part in path_parts:
        if part.lower() in FileMentionCard._IGNORED_DIRS:
            return True

    for part in path_parts:
        part_lower = part.lower()
        for pattern in FileMentionCard._IGNORED_DIR_PATTERNS:
            if fnmatch.fnmatch(part_lower, pattern):
                return True

    if not is_dir:
        for ext in FileMentionCard._IGNORED_EXT:
            if name_lower.endswith(ext):
                return True

    if not patterns or not is_dir:
        # 文件级 gitignore 规则不再过滤文件（与 _scan_tree 内联判定同步，
        # config.json 等被 gitignore 的文件在 @ 卡中可见）；目录才判 gitignore
        return False

    ignored = False
    for pattern in patterns:
        negate = False
        p = pattern
        if pattern.startswith("!"):
            negate = True
            p = pattern[1:]
        p_trail = p.rstrip("/")
        match_path = rel_path.replace("\\", "/")

        if fnmatch.fnmatch(match_path, p_trail):
            ignored = not negate
            continue
        if "/" not in p and fnmatch.fnmatch(name, p_trail):
            ignored = not negate
            continue
        if p.endswith("/") and (match_path.startswith(p) or match_path == p.rstrip("/")):
            ignored = not negate
    return ignored


class TestIgnoreRulesEquivalence:
    """IgnoreRules 必须与旧 fnmatch 实现判定完全一致"""

    # 刻意避开 build / dist / logs 等已存在于 _IGNORED_DIRS 的名字：
    # 那些名字会命中「总是忽略目录名」规则，而 IgnoreRules 只负责 gitignore 部分
    # （_scan_tree 把「总是忽略」内联在前置快速路径中），两者混测不可比。
    CASES = [
        ("mybuild/", "mybuild", True, True),
        ("mybuild/", "a/mybuild", True, False),
        ("out/x.txt", "out/x.txt", False, True),
        ("out/x.txt", "out/y.txt", False, False),
        ("out/x.txt", "sub/out/x.txt", False, False),  # 含 / → 锚定到根
        ("*.log", "app.log", False, True),
        ("*.log", "sub/app.log", False, True),  # 无 / → 任意层级文件名
        ("!keep.log", "keep.log", False, False),
        ("secret*.txt", "secret_a.txt", False, True),
        ("[abc].txt", "b.txt", False, True),
        ("temp/", "temp", True, True),
        ("*.tmp", "cache.tmp", False, True),
        ("a?c.txt", "abc.txt", False, True),
        ("docs/", "docs", True, True),
    ]

    def test_matches_reference_implementation(self):
        for pattern, rel, is_dir, expected in self.CASES:
            rules = IgnoreRules.compile([pattern])
            parts = [p.lower() for p in rel.split("/")]
            got = rules.matches(rel.split("/")[-1], rel, parts, is_dir)
            assert got == expected, f"pattern={pattern!r} rel={rel!r}: 期望 {expected}, 实际 {got}"
            if is_dir:
                # 目录级规则仍与旧 fnmatch 引擎逐条一致；文件级规则的「不再应用」
                # 门控在调用层（_scan_tree/_is_ignored），不在 IgnoreRules 引擎内
                ref = _ref_is_ignored(rel, is_dir, [pattern])
                assert got == ref, f"pattern={pattern!r} rel={rel!r}: 与旧实现不一致（旧={ref}, 新={got}）"

    def test_negate_ordering_semantics(self):
        """gitignore 语义：按序匹配，最后一条生效"""
        parts = ["keep.log"]
        # 取反在后 → 不被忽略
        assert IgnoreRules.compile(["*.log", "!keep.log"]).matches("keep.log", "keep.log", parts, False) is False
        # 取反在前、通配在后 → 取反被覆盖
        assert IgnoreRules.compile(["!keep.log", "*.log"]).matches("keep.log", "keep.log", parts, False) is True

    def test_empty_patterns_never_ignored(self):
        rules = IgnoreRules.compile([])
        assert rules.matches("a.py", "a/b/a.py", ["a", "b", "a.py"], False) is False

    def test_compile_cache_returns_equivalent_object(self):
        """编译缓存必须返回判定等价的对象（增量路径依赖该缓存）"""
        pats = ["*.log", "out/"]
        assert IgnoreRules.compile(pats)._rules == IgnoreRules.compile(list(pats))._rules

    def test_static_is_ignored_matches_reference(self):
        """_is_ignored 委托路径（增量扫描使用）同样与旧实现一致"""
        patterns = ["*.log", "out/x.txt", "!keep.log"]
        samples = [
            ("app/main_widget.py", False),
            ("app/widgets/cards", True),
            ("out/x.txt", False),
            ("sub/out/x.txt", False),
            ("sub/app.log", False),
            ("keep.log", False),
            ("tests/data/sample.csv", False),
            ("myvenv/x.py", False),
        ]
        for rel, is_dir in samples:
            got = FileMentionCard._is_ignored(rel, is_dir, patterns)
            ref = _ref_is_ignored(rel, is_dir, patterns)
            assert got == ref, f"rel={rel!r}: 旧={ref}, 新={got}"


class TestScanTreeEquivalence:
    """_scan_tree 的扫描结果必须与旧实现逐条一致"""

    @staticmethod
    def _ref_scan(root_dir, patterns):
        items = []

        def walk(dirpath, prefix):
            try:
                with os.scandir(dirpath) as it:
                    for entry in it:
                        rel = f"{prefix}/{entry.name}" if prefix else entry.name
                        is_dir = entry.is_dir(follow_symlinks=False)
                        if _ref_is_ignored(rel, is_dir, patterns):
                            continue
                        items.append(rel + ("/" if is_dir else ""))
                        if is_dir:
                            walk(entry.path, rel)
            except OSError:
                pass

        walk(root_dir, "")
        return set(items)

    def test_scan_tree_matches_reference_on_temp_tree(self, tmp_path):
        """在临时目录树上比对新旧扫描结果（含忽略目录、gitignore、嵌套结构）"""
        root = tmp_path / "proj"
        (root / "src" / "pkg").mkdir(parents=True)
        (root / "out").mkdir()
        (root / "__pycache__").mkdir()
        (root / "a" / "b" / "c").mkdir(parents=True)
        (root / ".gitignore").write_text("*.log\nout/x.txt\n!keep.log\n", encoding="utf-8")
        for p in [
            "src/main.py",
            "src/pkg/util.py",
            "src/app.log",
            "src/keep.log",
            "out/x.txt",
            "out/y.txt",
            "a/b/c/deep.py",
            "__pycache__/mod.pyc",
            "README.md",
        ]:
            (root / p).write_text("x", encoding="utf-8")

        patterns = FileMentionCard._parse_gitignore(root)
        rules = IgnoreRules.compile(patterns)

        items, snapshots, watched, truncated = _scan_tree(str(root), rules, 100000)
        got = {i["relative_path"] + ("/" if i["type"] == "dir" else "") for i in items}
        expected = self._ref_scan(str(root), patterns)

        assert not truncated
        assert got == expected, f"差异: 仅新={got - expected}, 仅旧={expected - got}"
        # 目录快照与监视集合必须与扫描到的目录一致
        assert snapshots, "应记录目录快照"
        assert str(root) in watched

    def test_scan_tree_respects_max_items(self, tmp_path):
        root = tmp_path / "big"
        root.mkdir()
        for i in range(30):
            (root / f"f{i}.py").write_text("x", encoding="utf-8")

        items, _snapshots, _watched, truncated = _scan_tree(str(root), IgnoreRules.compile([]), 5)
        assert len(items) == 5
        assert truncated is True


class TestLazyRender:
    """初始渲染量下调后，搜索结果与懒加载行为不受影响

    注：无 query 时卡片为「目录浏览模式」（只列当前层直接子项 + 分组标题行），
    全量列表断言改用搜索模式（query 命中全部条目，含 1 条分组标题）。
    """

    @staticmethod
    def _make_card(n, prefix="pkg/mod"):
        card = FileMentionCard()
        card._file_cache = [
            {
                "name": f"f{i}.py",
                "path": f"/tmp/f{i}.py",
                "relative_path": f"{prefix}{i % 20}/f{i}.py",
                "type": "file",
            }
            for i in range(n)
        ]
        card._cache_dirty = False
        return card

    @staticmethod
    def _search_all(card, n):
        """搜索模式加载全量（"f" 命中所有 f*.py 名称，额外 +1 分组标题行）"""
        card.load_items("f")
        card._filter_timer.stop()
        card._do_filter_and_render()
        return n + 1

    def test_initial_render_count_is_visible_rows(self):
        """初始只渲染 INITIAL_RENDER_COUNT 个 widget（旧值为 200）"""
        _ensure_qapp()
        card = self._make_card(1000)
        self._search_all(card, 1000)
        assert len(card._item_widgets) == INITIAL_RENDER_COUNT

    def test_filtered_items_keep_full_result_set(self):
        """少建 widget 不得减少匹配结果：_filtered_items 仍是全量"""
        _ensure_qapp()
        card = self._make_card(1000)
        expected = self._search_all(card, 1000)
        assert len(card._filtered_items) == expected
        assert card._rendered_count == INITIAL_RENDER_COUNT

    def test_keyboard_navigation_extends_render(self):
        """键盘一路向下时可持续追加，最终能浏览到全量（旧行为保持）"""
        _ensure_qapp()
        card = self._make_card(300)
        expected = self._search_all(card, 300)

        steps = 0
        while card._rendered_count < len(card._filtered_items) and steps < 100:
            card._selected_index = len(card._item_widgets) - 1
            if not card.select_next():
                card._extend_render()
            steps += 1

        assert len(card._item_widgets) == expected
        assert len(card._item_widgets) <= MAX_RENDERED_ITEMS

    def test_precise_search_renders_hit(self):
        """精确搜索命中唯一项时，该项必须进入渲染（不受渲染上限影响）"""
        _ensure_qapp()
        card = self._make_card(3000, prefix="deep/a/b/c/")
        card.load_items("f2999")
        card._filter_timer.stop()
        card._do_filter_and_render()
        # 全局搜索仍递归命中深层文件；「文件」分组标题行 + 命中项 = 2
        assert len(card._filtered_items) == 2
        assert len(card._item_widgets) == 2

    def test_no_match_clears_widgets(self):
        """无匹配 → 只剩 empty_hint 提示行（新语义：不隐藏整卡，保留退出路径）"""
        _ensure_qapp()
        card = self._make_card(50)
        card.load_items("definitely_not_exists_xyz")
        card._filter_timer.stop()
        card._do_filter_and_render()
        assert [i["type"] for i in card._filtered_items] == ["empty_hint"]
        assert len(card._item_widgets) == 1


class TestBrowseMode:
    """目录浏览模式（DSH 式层级导航）：无 query 只列当前层直接子项"""

    @staticmethod
    def _make_card():
        card = FileMentionCard()
        card._file_cache = [
            {"name": "src", "path": "/tmp/src", "relative_path": "src", "type": "dir"},
            {"name": "README.md", "path": "/tmp/README.md", "relative_path": "README.md", "type": "file"},
            {"name": "a.py", "path": "/tmp/src/a.py", "relative_path": "src/a.py", "type": "file"},
            {"name": "sub", "path": "/tmp/src/sub", "relative_path": "src/sub", "type": "dir"},
            {"name": "b.py", "path": "/tmp/src/sub/b.py", "relative_path": "src/sub/b.py", "type": "file"},
        ]
        card._cache_dirty = False
        # 隔离环境依赖：会话/插件提及走真实后端（SQLite/注册表），
        # 本机数据非空会污染「浏览模式条目集」断言
        card._collect_session_items = lambda: []
        card._collect_mention_items = lambda: []
        return card

    def test_root_lists_only_direct_children(self):
        """根层只列直接子项，不递归铺开"""
        _ensure_qapp()
        card = self._make_card()
        card.load_items("")
        card._filter_timer.stop()
        card._do_filter_and_render()
        # 「文件」标题 + src + README.md（孙辈不出现）
        entries = [i for i in card._filtered_items if i.get("type") not in ("section_title",)]
        assert sorted(i["relative_path"] for i in entries) == ["README.md", "src"]
        # 目录项带进入标记（右侧「进入目录 Tab ›」提示）
        src = next(i for i in entries if i["relative_path"] == "src")
        assert src.get("enterable") is True

    def test_enter_dir_lists_next_level(self):
        """Tab 进入目录后列出该层直接子项"""
        _ensure_qapp()
        card = self._make_card()
        card.load_items("")
        card._filter_timer.stop()
        card._do_filter_and_render()
        assert card.enter_selected_dir() is True  # 首个可选条目是 src 目录
        card._filter_timer.stop()
        card._do_filter_and_render()
        entries = [i for i in card._filtered_items if i.get("type") not in ("section_title",)]
        # relative_path 保持缓存中的全路径（显示层用 display_name 只显示本级名）
        assert sorted(i["relative_path"] for i in entries) == ["src/a.py", "src/sub"]
        assert sorted(i["display_name"] for i in entries) == ["a.py", "sub"]
        # 面包屑出现 src 段
        crumbs = [l.text() for l in card._crumb_labels if isinstance(l.text(), str) and l.text()]
        assert "src" in crumbs

    def test_search_is_global_across_levels(self):
        """搜索时仍为全局递归搜索，不受当前浏览层限制"""
        _ensure_qapp()
        card = self._make_card()
        card.load_items("")
        card.enter_selected_dir()
        card.load_items("b.py")
        card._filter_timer.stop()
        card._do_filter_and_render()
        entries = [i for i in card._filtered_items if i.get("type") not in ("section_title",)]
        assert [i["relative_path"] for i in entries] == ["src/sub/b.py"]


class TestScrollAndFlashRegression:
    """滚动卡顿与闪现修复的回归测试

    1. 懒加载渲染到深处后，FS 增量更新重入 _render_incremental：
       不得销毁重建已渲染 widget（闪现+跳顶+卡顿），不得重置 _rendered_count。
    2. 滚动置 hover 抑制窗：窗内 hovered 不同步选中（滚动↔hover 反馈循环）；
       hover 同步不调 ensureWidgetVisible，键盘导航仍调。
    3. fuzzy 收紧：路径子序列档删除；文件名子序列档 query≥3 才启用。
    4. 会话条目名高亮搜索词。
    """

    @staticmethod
    def _make_card(n_items=200):
        card = FileMentionCard()
        card._file_cache = [
            {"name": f"f{i}.py", "path": f"/tmp/f{i}.py", "relative_path": f"f{i}.py", "type": "file"}
            for i in range(n_items)
        ]
        card._cache_dirty = False
        card._collect_mention_items = lambda: []
        card._collect_session_items = lambda: []
        return card

    def _load_and_render(self, card, query="f"):
        card.load_items(query)
        card._filter_timer.stop()
        card._do_filter_and_render()

    def test_incremental_render_preserves_lazy_count(self):
        _ensure_qapp()
        card = self._make_card()
        self._load_and_render(card)
        assert len(card._item_widgets) == INITIAL_RENDER_COUNT

        # 模拟滚动懒加载两批
        card._extend_render()
        card._extend_render()
        deep = len(card._item_widgets)
        assert deep > INITIAL_RENDER_COUNT

        # FS 增量更新重入：同 query 再次过滤渲染（条目集不变）
        self._load_and_render(card, "f")

        assert len(card._item_widgets) == deep, "已渲染 widget 不应被销毁重建"
        assert card._rendered_count == deep, "懒加载渲染量不应被重置回初始值"

    def test_scroll_suppresses_hover_selection(self):
        _ensure_qapp()
        card = self._make_card(50)
        self._load_and_render(card)

        calls = []
        card._scroll_area.ensureWidgetVisible = lambda *a, **k: calls.append(1)

        # 正常 hover：同步选中，但不拖动视口
        card._on_item_hovered(card._item_widgets[3])
        assert card._selected_index == 3
        assert not calls, "hover 同步不应触发 ensureWidgetVisible"

        # 滚动后的抑制窗内：hover 不改选中
        card._on_scroll(0)
        card._on_item_hovered(card._item_widgets[6])
        assert card._selected_index == 3, "滚动抑制窗内 hover 不应同步选中"

        # 键盘导航仍滚动到可见
        card.select_next()
        assert card._selected_index == 4
        assert calls, "键盘导航应触发 ensureWidgetVisible"

    def test_fuzzy_gating(self):
        score = FileMentionCard._score_item
        item = {"name": "requirements.txt", "relative_path": "docs/requirements.txt"}
        # ≤2 字符 query：即使子序列命中也不给 fuzzy 分
        assert score(item, "rq") == 0
        # ≥3 字符：文件名子序列命中 → 40
        assert score(item, "rqrmnts") == 40
        # ≥3 字符但仅路径子序列、文件名不命中 → 路径 fuzzy 档已删，不给分
        item2 = {"name": "readme.md", "relative_path": "x/require/readme.md"}
        assert score(item2, "rqirm") == 0
        # 子串匹配不受影响
        assert score(item, "quire") == 60
        assert score(item, "docs") == 55

    def test_session_name_highlight(self):
        _ensure_qapp()
        data = {"type": "session", "name": "修复滚动卡顿的会话", "path": "session://1", "relative_path": "修复滚动卡顿的会话"}
        w = FileMentionItemWidget(dict(data), "滚动")
        assert w._path_label._hl_queries, "会话名应高亮搜索词"
        # 无 query：不高亮
        w2 = FileMentionItemWidget(dict(data), "")
        assert not w2._path_label._hl_queries

    def test_session_items_filtered_by_project(self, monkeypatch):
        """会话搜索只显示当前项目：project 透传存储层，切换项目缓存立即失效"""
        _ensure_qapp()
        calls = []

        class FakeStorage:
            def get_sessions_lightweight(self, limit=100, offset=0, project=None):
                calls.append(project)
                assert limit >= 1000, "搜索池应覆盖历史全量而非最近 30 条"
                return [
                    {"session_id": "s1", "title": "项目内会话", "project": project or "", "updated_at": "t"},
                    {"session_id": "s2", "title": "他项目会话", "project": "Other", "updated_at": "t"},
                ]

            def get_all(self, limit=100):
                return []

        import app.core.conversation.backend as backend_mod

        monkeypatch.setattr(backend_mod, "get_session_storage", lambda: FakeStorage())

        card = self._make_card(10)
        del card._collect_session_items  # 摘除 _make_card 的 mock，走真实方法验证 project 透传
        card.show_card("/tmp", "", project="ProjA")
        # show_card 对新 root_dir 走异步扫描分支（QTimer 延迟加载），
        # 测试环境无事件循环，手动置缓存就绪后同步加载
        card._cache_dirty = False
        card.load_items("")
        card._filter_timer.stop()
        card._do_filter_and_render()

        assert calls and calls[-1] == "ProjA", "project 应透传到存储层"
        entries = [i for i in card._filtered_items if i.get("type") == "session"]
        assert [i["name"] for i in entries] == ["项目内会话"], "应只保留当前项目会话"

        # 切换项目：TTL 内也必须重拉且传新值
        card.show_card("/tmp", "", project="ProjB")
        card._filter_timer.stop()
        card._do_filter_and_render()
        assert calls[-1] == "ProjB"
        assert len(calls) >= 2, "项目切换后不得命中旧缓存"

        # 未注入项目（空串）→ None → 全局，旧行为兼容
        card.show_card("/tmp", "", project="")
        card._filter_timer.stop()
        card._do_filter_and_render()
        assert calls[-1] is None


class TestBrowseUX:
    """目录浏览 UX：空态保留卡片、浏览↔输入框联动、文件级 gitignore 不再过滤"""

    @staticmethod
    def _make_card():
        card = FileMentionCard()
        card._file_cache = [
            {"name": "src", "path": "/tmp/src", "relative_path": "src", "type": "dir"},
            {"name": "config", "path": "/tmp/config", "relative_path": "config", "type": "dir"},
            {"name": "config.json", "path": "/tmp/config/config.json", "relative_path": "config/config.json", "type": "file"},
            {"name": "empty_dir", "path": "/tmp/empty_dir", "relative_path": "empty_dir", "type": "dir"},
            {"name": "README.md", "path": "/tmp/README.md", "relative_path": "README.md", "type": "file"},
        ]
        card._cache_dirty = False
        card._collect_mention_items = lambda: []
        card._collect_session_items = lambda: []
        return card

    def _load(self, card, query=""):
        card.load_items(query)
        card._filter_timer.stop()
        card._do_filter_and_render()

    def test_empty_dir_keeps_hint_row(self):
        """进入空目录 → empty_hint 提示行渲染，卡片不再消失，Enter 不误发"""
        _ensure_qapp()
        card = self._make_card()
        card._browse_rel = "empty_dir"
        self._load(card)
        types = [i["type"] for i in card._filtered_items]
        assert types == ["empty_hint"], f"空目录应只剩提示行，实际 {types}"
        assert len(card._item_widgets) == 1
        # 卡片可见性口径：filtered 含提示行恒非空 → show_card 不会隐藏
        assert len(card._filtered_items) > 0
        # Enter/点击不发射
        fired = []
        card.fileSelected.connect(fired.append)
        card.select_current()
        assert not fired
        # 可见性：模拟卡片已显示 → 重入渲染后保持可见（filtered 含提示行恒非空，
        # show_card/_on_scan_finished 的 has_items 判定不会再隐藏）
        card._visible = True
        card.setVisible(True)
        self._load(card)
        assert card.is_card_visible

    def test_search_no_result_keeps_hint_row(self):
        _ensure_qapp()
        card = self._make_card()
        self._load(card, "zzz不存在的词")
        assert [i["type"] for i in card._filtered_items] == ["empty_hint"]
        assert card._filtered_items[0]["name"] == "无匹配结果"

    def test_browse_changed_signal_emitted(self):
        """Tab 进入/面包屑返回目录 → browseChanged 发射 "rel/" 形式供输入框联动"""
        _ensure_qapp()
        card = self._make_card()
        got = []
        card.browseChanged.connect(got.append)
        self._load(card)
        idxs = [i for i, it in enumerate(card._filtered_items) if it.get("type") == "dir"]
        card._selected_index = idxs[0]
        target = card._filtered_items[idxs[0]]["relative_path"]
        assert card.enter_selected_dir() is True
        assert card._browse_rel == target
        assert got[-1] == target + "/"

    def test_show_card_suffix_slash_resolves_browse_level(self):
        """输入框回填的尾缀 "/" query 解析为浏览层；未命中回落根层；无 query 回根层"""
        _ensure_qapp()
        card = self._make_card()
        # 大小写不敏感解析到真实目录名
        card.show_card("/tmp", "SRC/", project="p")
        assert card._browse_rel == "src"
        # 未命中 → 根层
        card.show_card("/tmp", "nope/", project="p")
        assert card._browse_rel == ""
        # 无 query → 根层（原语义）
        card._browse_rel = "src"
        card.show_card("/tmp", "", project="p")
        assert card._browse_rel == ""
        # 非尾缀 query → 搜索模式，保留浏览层（原语义）
        card._browse_rel = "src"
        card.show_card("/tmp", "config", project="p")
        assert card._browse_rel == "src"

    def test_file_level_gitignore_not_applied(self):
        """文件级 gitignore 规则不再过滤文件：config.json 等必须在列表中可见"""
        assert FileMentionCard._is_ignored("config/config.json", False, ["*.json"]) is False
        assert FileMentionCard._is_ignored("x/app.log", False, ["*.log"]) is False
        # 目录规则仍生效（递归剪枝）
        assert FileMentionCard._is_ignored("build/x.py", True, ["build/"]) is True
        assert FileMentionCard._is_ignored("build", True, ["build/"]) is True
        # always-ignore 扩展名对文件依旧生效
        assert FileMentionCard._is_ignored("a/mod.pyc", False, []) is True

    def test_session_search_pool_and_browse_slice(self):
        """搜索池覆盖历史全量（非最近 30 条）；浏览模式只展示最近 N 条"""
        _ensure_qapp()
        card = self._make_card()
        n = 200  # 覆盖 _SESSION_BROWSE_LIMIT(30) 之外的深部条目
        card._collect_session_items = lambda: [
            {"type": "session", "name": f"会话{i}", "path": f"session://{i}", "relative_path": f"会话{i}"}
            for i in range(n)
        ]
        self._load(card)
        browse_sessions = [i for i in card._filtered_items if i.get("type") == "session"]
        assert len(browse_sessions) == card._SESSION_BROWSE_LIMIT
        # 搜索：老会话（不在前 30）也能命中
        self._load(card, "会话199")
        hits = [i for i in card._filtered_items if i.get("type") == "session"]
        assert len(hits) == 1 and hits[0]["name"] == "会话199"


if __name__ == "__main__":
    sys.exit(0)

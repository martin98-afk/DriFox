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

from PySide6.QtWidgets import QApplication

from app.widgets.cards.floating.file_mention_card import (
    INITIAL_RENDER_COUNT,
    MAX_RENDERED_ITEMS,
    _scan_tree,
    FileMentionCard,
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

    if not patterns:
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
            ref = _ref_is_ignored(rel, is_dir, [pattern])
            assert got == expected, f"pattern={pattern!r} rel={rel!r}: 期望 {expected}, 实际 {got}"
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
    """初始渲染量下调后，搜索结果与懒加载行为不受影响"""

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

    def test_initial_render_count_is_visible_rows(self):
        """初始只渲染 INITIAL_RENDER_COUNT 个 widget（旧值为 200）"""
        _ensure_qapp()
        card = self._make_card(1000)
        card.load_items("")
        card._filter_timer.stop()
        card._do_filter_and_render()
        assert len(card._item_widgets) == INITIAL_RENDER_COUNT

    def test_filtered_items_keep_full_result_set(self):
        """少建 widget 不得减少匹配结果：_filtered_items 仍是全量"""
        _ensure_qapp()
        card = self._make_card(1000)
        card.load_items("")
        card._filter_timer.stop()
        card._do_filter_and_render()
        assert len(card._filtered_items) == 1000
        assert card._rendered_count == INITIAL_RENDER_COUNT

    def test_keyboard_navigation_extends_render(self):
        """键盘一路向下时可持续追加，最终能浏览到全量（旧行为保持）"""
        _ensure_qapp()
        card = self._make_card(300)
        card.load_items("")
        card._filter_timer.stop()
        card._do_filter_and_render()

        steps = 0
        while card._rendered_count < len(card._filtered_items) and steps < 100:
            card._selected_index = len(card._item_widgets) - 1
            if not card.select_next():
                card._extend_render()
            steps += 1

        assert len(card._item_widgets) == 300
        assert len(card._item_widgets) <= MAX_RENDERED_ITEMS

    def test_precise_search_renders_hit(self):
        """精确搜索命中唯一项时，该项必须进入渲染（不受渲染上限影响）"""
        _ensure_qapp()
        card = self._make_card(3000, prefix="deep/a/b/c/")
        card.load_items("f2999")
        card._filter_timer.stop()
        card._do_filter_and_render()
        assert len(card._filtered_items) == 1
        assert len(card._item_widgets) == 1

    def test_no_match_clears_widgets(self):
        _ensure_qapp()
        card = self._make_card(50)
        card.load_items("definitely_not_exists_xyz")
        card._filter_timer.stop()
        card._do_filter_and_render()
        assert len(card._filtered_items) == 0
        assert len(card._item_widgets) == 0


if __name__ == "__main__":
    sys.exit(0)

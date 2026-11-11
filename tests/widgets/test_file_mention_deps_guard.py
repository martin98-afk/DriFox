# -*- coding: utf-8 -*-
"""FileMentionCard 忽略表补 deps 的回归测试（工单 F1b）

1. "deps" 进入 _IGNORED_DIRS 后，_is_ignored / _scan_tree 对任意层级的
   deps 目录命中忽略（对齐 plugin_host_service._EXTRA_SKIP_DIRS 语义）。
2. 增量扫描 _incremental_scan_new_dir 在注册 QFileSystemWatcher 监视器
   前先过忽略判断：忽略目录不再注册监视器、不再递归扫描。
"""

import os
import sys

from PySide6.QtWidgets import QApplication

from app.widgets.cards.floating.file_mention_card import (
    FileMentionCard,
    IgnoreRules,
    _get_always_ignore,
    _scan_tree,
)


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


def test_deps_in_ignored_dirs():
    assert "deps" in FileMentionCard._IGNORED_DIRS
    ignored_dirs, _ext, _patterns = _get_always_ignore()
    assert "deps" in ignored_dirs


def test_is_ignored_hits_deps_at_any_level():
    patterns = []
    assert FileMentionCard._is_ignored("myplugin/deps/lib.py", False, patterns) is True
    assert FileMentionCard._is_ignored("deps/lib.py", False, patterns) is True
    # 精确匹配不误伤
    assert FileMentionCard._is_ignored("dependencies.json", False, patterns) is False
    assert FileMentionCard._is_ignored("myplugin/depsutil/x.py", False, patterns) is False


def test_scan_tree_skips_deps(tmp_path):
    """全量扫描：deps 目录整体不入缓存"""
    root = tmp_path
    (root / "myplugin" / "deps" / "pkg").mkdir(parents=True)
    (root / "myplugin" / "deps" / "pkg" / "lib.py").write_text("x = 1", encoding="utf-8")
    (root / "myplugin" / "src.py").write_text("y = 2", encoding="utf-8")

    items, _snapshots, _watched, _truncated = _scan_tree(
        str(root), IgnoreRules.compile([]), 1000
    )
    rels = {i["relative_path"] for i in items}
    assert "myplugin/src.py" in rels
    assert not any(r.startswith("myplugin/deps") for r in rels)


def test_incremental_scan_skips_deps_dir(tmp_path):
    """增量扫描：deps 目录不注册监视器、不递归扫描（addPath 前守卫）"""
    _ensure_qapp()
    card = FileMentionCard()
    deps = tmp_path / "deps"
    deps.mkdir()
    (deps / "lib.py").write_text("x = 1", encoding="utf-8")

    card._incremental_scan_new_dir(os.path.normpath(str(deps)), "myplugin")

    assert os.path.normpath(str(deps)) not in card._watched_dirs
    assert card._file_cache == []


def test_incremental_scan_normal_dir_still_works(tmp_path):
    """对照：普通目录增量扫描行为不变（注册监视器 + 递归扫描）"""
    _ensure_qapp()
    card = FileMentionCard()
    plain = tmp_path / "myplugin"
    plain.mkdir()
    (plain / "src.py").write_text("y = 2", encoding="utf-8")

    card._incremental_scan_new_dir(os.path.normpath(str(plain)), "")

    assert os.path.normpath(str(plain)) in card._watched_dirs
    rels = {i["relative_path"] for i in card._file_cache}
    assert "src.py" in rels

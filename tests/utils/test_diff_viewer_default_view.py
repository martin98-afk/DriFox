# -*- coding: utf-8 -*-
"""回归测试：diff_viewer 默认进入统一直列（unified）视图。

根因
----
`generate_html_report(default_view="unified")` 默认参数已是 unified，但
预加载文件块由 Python 端 `_file_block` 直接渲染，unified 写死
`display:none`、split 恒显示；而 `applyView` 只在 JS 懒加载路径
（loadFile）调用 → 预加载块从未被纠正 → 默认进去显示并排（split），
需手动切到统一直列。

修复
----
- `_file_block` 按 default_view 控制 unified/split 初始 display（静态正确）
- 初始化 JS 对所有 .file-block 兜底 applyView
- 懒加载 `genBlock` 按 window._cv 控制首帧 display（防闪帧）

测试说明
-------
生成 HTML 后断言字符串（无需真实 WebEngine 渲染）。
"""

import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

# 必须在导入 diff_viewer（其导入 QWebEngineWidgets）之前设置
QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
QApplication.instance() or QApplication(sys.argv)

from app.utils.diff_viewer import DiffHtmlGenerator  # noqa: E402

_DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,5 +1,5 @@
 def foo():
-    return 1
+    return 2
 def bar():
     pass
"""


def _assert_view(html: str, visible: str, hidden: str):
    """断言 visible 视图 display 为空（显示）、hidden 视图 display:none（隐藏）。"""
    assert f'data-view="{visible}" style="display:">' in html, f"{visible} 应为默认显示视图，实际 display 非空"
    assert f'data-view="{hidden}" style="display:none">' in html, f"{hidden} 应为隐藏视图，实际 display 非 none"


def test_default_view_is_unified():
    """默认（不传 default_view）进入统一直列：unified 显示、split 隐藏。"""
    html = DiffHtmlGenerator.generate_html_report(_DIFF, "")
    _assert_view(html, "unified", "split")


def test_default_view_unified_explicit():
    """显式 default_view="unified" 与默认一致。"""
    html = DiffHtmlGenerator.generate_html_report(_DIFF, "", default_view="unified")
    _assert_view(html, "unified", "split")


def test_default_view_split_explicit():
    """显式 default_view="split"：split 显示、unified 隐藏。"""
    html = DiffHtmlGenerator.generate_html_report(_DIFF, "", default_view="split")
    _assert_view(html, "split", "unified")


def test_js_initial_apply_view_bootstrap():
    """初始化 JS 必须对所有 .file-block 兜底 applyView（覆盖预加载块）。"""
    html = DiffHtmlGenerator.generate_html_report(_DIFF, "")
    assert "querySelectorAll('.file-block').forEach(function(b){applyView(b);})" in html, (
        "初始化必须对预加载块兜底应用默认视图"
    )


def test_js_gen_block_uses_current_view():
    """懒加载 genBlock 首帧 display 按 window._cv 控制（防闪帧）。"""
    html = DiffHtmlGenerator.generate_html_report(_DIFF, "")
    assert "window._cv==='unified'?'':'none'" in html, "genBlock 首帧 display 必须按当前视图控制"

# -*- coding: utf-8 -*-
"""回归测试：ChangelogFetcher 后台线程禁用全局 Markdown 实例。

「📜 更新」tab 已迁出为独立系统插件 ``welcome_changelog``，ChangelogFetcher
随之搬入插件 ``plugins/welcome_changelog/ui/_fetcher.py``；本测试改查插件源文件。

根因：``ChangelogFetcher.run()`` 在 QThread 后台线程调用 ``get_markdown_instance()``
（全局单例 ``_md_instance``），与主线程消息渲染并发时，Markdown.reset()/convert()
的解析状态互相打乱 → 消息卡片表格偶发渲染失败、内容串扰（实测并发 3000 次
损坏 13 次 ≈ 0.4%；单线程基线 0 损坏）。

修复：run() 内改用线程私有 Markdown 实例（与 ``_render_markdown_to_html_worker``
的 TLS 模式一致，遵守 message_card.py「全局实例禁止跨线程」铁律）。
"""

import ast
import re
import textwrap
from pathlib import Path

_PLUGIN_FETCHER = (
    Path(__file__).resolve().parent.parent.parent
    / "plugins" / "welcome_changelog" / "ui" / "_fetcher.py"
)


def _find_run_method():
    """定位插件 _fetcher.py 中 ChangelogFetcher.run 方法的 AST 节点。"""
    tree = ast.parse(_PLUGIN_FETCHER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "ChangelogFetcher"):
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "run":
                return item
    return None


def test_changelog_fetcher_run_does_not_use_global_md_instance():
    """AST：插件 ChangelogFetcher.run() 不得调用 get_markdown_instance()（全局实例禁止跨线程）。"""
    run_node = _find_run_method()
    assert run_node is not None, (
        "未找到 plugins/welcome_changelog/ui/_fetcher.py:ChangelogFetcher.run 方法"
    )

    func_src = textwrap.dedent(ast.unparse(run_node))
    assert "get_markdown_instance" not in func_src, (
        "ChangelogFetcher.run 在后台线程使用全局 _md_instance，"
        "与主线程渲染并发会串扰（表格偶发渲染失败根因），"
        "必须在 run() 内创建线程私有 Markdown 实例"
    )


def test_changelog_fetcher_run_builds_private_md_with_tables():
    """AST：插件 ChangelogFetcher.run() 内自建线程私有 Markdown 实例，且保留 tables 扩展。"""
    run_node = _find_run_method()
    assert run_node is not None, (
        "未找到 plugins/welcome_changelog/ui/_fetcher.py:ChangelogFetcher.run 方法"
    )

    func_src = textwrap.dedent(ast.unparse(run_node))
    assert re.search(r"Markdown\s*\(", func_src), (
        "run() 必须自建线程私有 Markdown 实例（extensions 含 tables）"
    )
    assert re.search(r"extensions\s*=\s*\[[^\]]*['\"]tables['\"]", func_src, re.DOTALL), (
        "changelog 渲染需保留 tables 扩展（release note 含表格）"
    )
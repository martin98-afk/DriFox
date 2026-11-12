# -*- coding: utf-8 -*-
"""[H1] 切会话/建会话链路落盘 flush 异步化（回归：主线程同步 flush 大会话 700ms+ 卡顿）

== 背景 ==
切会话基线 T33 P50 312ms / P95 634ms（.perf_out/t33_report_final.md），其中
主线程同步 flush（serialize+zstd+SQLite 写，大会话 700ms+）是最可感的一项。

== 修复 ==
四个切换/建会话入口改异步（照抄 _do_post_stream_cleanup 的 flush_async 先例）：
- _switch_to_session_by_id：flush() → flush_async()
- _create_branched_session / _create_new_session / _load_session_from_record：
  _auto_save_current_session() → flush_mode="async"

== 退出兜底（不得误改）==
_on_app_about_to_quit 遍历存活窗口调 _auto_save_current_session()（默认 sync）
+ flush() 同步写盘——是关窗/退出唯一保数据点（flush_async 非 daemon 线程
join 兜底之外的首道保障）。本测试同时断言退出路径保持 sync，防后续误改。

== 断言口径 ==
AST 源码断言（同 test_session_save_team_welcome.py 风格）。
"""

import ast
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_PATH = _REPO_ROOT / "app" / "main_widget.py"


def _load_method(name: str) -> ast.FunctionDef:
    tree = ast.parse(_SRC_PATH.read_text(encoding="utf-8"), filename=str(_SRC_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "OpenAIChatToolWindow":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return item
    raise AssertionError(f"未找到方法 OpenAIChatToolWindow.{name}")


def _source(method: ast.FunctionDef) -> str:
    return ast.unparse(method)


def _call_attr_names(method: ast.FunctionDef, attr: str):
    """收集 self.<attr>(...) 调用的完整 unparse 文本"""
    out = []
    for sub in ast.walk(method):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == attr:
            out.append(ast.unparse(sub))
    return out


class TestSwitchSessionFlushAsync:
    """切会话/建会话四入口必须异步落盘"""

    def test_switch_to_session_uses_flush_async(self):
        method = _load_method("_switch_to_session_by_id")
        src = _source(method)
        assert "history_manager.flush_async()" in src, (
            "_switch_to_session_by_id 必须用 flush_async（主线程同步 flush 大会话 700ms+，"
            "是切会话卡顿主因）；退出兜底由 _on_app_about_to_quit 的 sync flush 负责"
        )
        # 函数内不得残留同步 flush（save 到 flush 的配对完整性由 flush_async 内部
        # pending 机制保证；同步 flush 在此只会把 700ms 卡顿带回来）
        assert "history_manager.flush()" not in src, (
            "_switch_to_session_by_id 内不得残留主线程同步 flush()"
        )

    @pytest.mark.parametrize(
        "method_name",
        ["_create_branched_session", "_create_new_session", "_load_session_from_record"],
    )
    def test_session_entries_pass_async_flush_mode(self, method_name):
        method = _load_method(method_name)
        calls = _call_attr_names(method, "_auto_save_current_session")
        assert calls, f"{method_name} 必须调用 _auto_save_current_session 保存旧会话"
        for c in calls:
            # ast.unparse 统一输出单引号形态
            assert "flush_mode='async'" in c, (
                f"{method_name} 的 _auto_save_current_session 必须传 flush_mode='async'"
                f"（同步 flush 是主线程 700ms+ 卡顿点），实际调用: {c}"
            )


class TestQuitPathKeepsSyncFlush:
    """退出广播路径保持同步 flush（关窗/退出唯一保数据点，不得异步化）"""

    def test_about_to_quit_sync_flush(self):
        method = _load_method("_on_app_about_to_quit")
        calls = _call_attr_names(method, "_auto_save_current_session")
        assert calls, "_on_app_about_to_quit 必须遍历存活窗口保存会话"
        for c in calls:
            assert "flush_mode" not in c, (
                f"退出广播的 _auto_save_current_session 必须保持默认 sync 模式"
                f"（退出路径数据安全优先于流畅性），实际调用: {c}"
            )

    def test_auto_save_sync_branch_keeps_flush(self):
        """_auto_save_current_session 的 sync 分支必须保留 flush()（退出路径消费）"""
        method = _load_method("_auto_save_current_session")
        src = _source(method)
        assert "flush_mode == 'async'" in src, "async 分支判定缺失（关窗路径依赖）"
        assert "self.history_manager.flush()" in src, (
            "sync 分支必须保留同步 flush()——退出路径（_on_app_about_to_quit）"
            "的数据安全兜底"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

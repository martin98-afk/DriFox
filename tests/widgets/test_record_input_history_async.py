# -*- coding: utf-8 -*-
"""[I12] 发送路径输入历史零 SQL 回归。

== 目标语义 ==
_record_input_history 主线程不再同步执行 4~5 次 SQL 往返（add 去重 SELECT +
INSERT + COUNT(+DELETE) + get_all LIMIT 50）：UI 缓存由内存镜像维护（与
InputHistoryRepository 相邻去重/最新在前/50 条上限同口径），落账转后台
daemon 线程；镜像未就绪时回退原同步路径。

== 等价性验证 ==
内存镜像演化序列 与 真库 add+get_all 演化序列 在同一操作序列下逐条一致。
"""

import ast
import sys
from importlib import import_module
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


def _direct_attr_calls(method: ast.FunctionDef, attr: str) -> int:
    """方法体顶层（不含嵌套函数/lambda）中 self.<attr>(...) 调用数。

    嵌套 FunctionDef 内的调用属于后台闭包（如 _bg_record），不计入主线程
    同步调用。lambda（如 QTimer.singleShot 回调）按表达式内联计入——当前
    该方法无 lambda 调库场景。
    """
    count = 0

    def walk(node):
        nonlocal count
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == attr:
                count += 1
            walk(child)

    walk(method)
    return count


class TestRecordInputHistoryZeroSql:
    """主线程发送路径零同步 SQL"""

    def test_no_sync_add_on_main_path(self):
        method = _load_method("_record_input_history")
        assert _direct_attr_calls(method, "add_input_history") == 0, (
            "_record_input_history 主线程不得同步调 add_input_history（应转后台线程落账）"
        )

    def test_sync_get_only_as_mirror_fallback(self):
        method = _load_method("_record_input_history")
        assert _direct_attr_calls(method, "get_input_history") <= 1, (
            "get_input_history 只允许出现在镜像未就绪的回退分支（≤1 次）"
        )

    def test_background_thread_present(self):
        method = _load_method("_record_input_history")
        src = ast.unparse(method)
        assert "threading.Thread" in src, "落账必须转后台线程"
        assert "daemon=True" in src, "后台落账线程必须 daemon（不阻塞进程退出）"
        assert "_merge_input_history_cache" in src, "UI 缓存必须走内存镜像合并"

    def test_load_history_seeds_mirror(self):
        method = _load_method("_load_input_history")
        src = ast.unparse(method)
        assert "_input_history_cache" in src, "启动加载必须回填内存镜像（发送路径零 SQL 的前提）"


class TestMergeCacheEquivalence:
    """内存镜像演化 与 真库 add+get_all 演化 逐条一致（写入/读取等价性）"""

    def _merge(self, cache, text, attachments=None, limit=50):
        from app.main_widget import OpenAIChatToolWindow

        return OpenAIChatToolWindow._merge_input_history_cache(cache, text, attachments, limit)

    def test_pure_function_semantics(self):
        # prepend 最新在前
        assert self._merge([], "a") == [{"text": "a", "attachments": []}]
        assert self._merge([{"text": "a", "attachments": []}], "b") == [
            {"text": "b", "attachments": []},
            {"text": "a", "attachments": []},
        ]
        # 相邻去重：同 text 同 attachments 跳过
        cache = [{"text": "a", "attachments": ["x"]}]
        assert self._merge(cache, "a", ["x"]) is cache
        # 不同 attachments 不去重
        merged = self._merge(cache, "a", ["y"])
        assert merged[0] == {"text": "a", "attachments": ["y"]}
        # 空白 text 跳过
        assert self._merge(cache, "   ") is cache
        # 截断 50
        big = [{"text": f"t{i}", "attachments": []} for i in range(60)]
        merged = self._merge(big, "new")
        assert len(merged) == 50
        assert merged[0] == {"text": "new", "attachments": []}
        assert merged[1] == big[0]
        # 旧形态（纯字符串）兼容
        merged = self._merge(["old"], "new")
        assert merged == [{"text": "new", "attachments": []}, "old"]

    def test_mirror_matches_sqlite_evolution(self, tmp_path):
        """同一操作序列：真库 add+get_all 演化 == 内存镜像演化。"""
        from app.core.store.session_store import SessionStore
        from app.utils.db_manager import DatabaseManager

        SessionStore._instance = None
        DatabaseManager._instance = None
        store = None
        try:
            store = SessionStore(str(tmp_path))
            assert store.is_initialized

            ops = [
                ("hello", None),
                ("world", ["D:/a.txt"]),
                ("world", ["D:/a.txt"]),  # 相邻重复：两侧都应跳过
                ("hello", None),  # 非相邻重复：两侧都应保留
                ("  spaced  ", None),  # 空白归一后入账
                ("spaced", None),  # 与上一条（strip 后）相邻重复：跳过
                ("tail", []),
            ]
            mirror: list = []
            for text, attrs in ops:
                store.add_input_history(text, attrs)
                mirror = self._merge(mirror, text, attrs)

            db_list = store.get_input_history() or []
            assert len(mirror) == len(db_list), (
                f"镜像与真库条数不一致：mirror={len(mirror)} db={len(db_list)}"
            )
            for i, (m, d) in enumerate(zip(mirror, db_list)):
                assert m.get("text") == (d.get("text") or "").strip(), f"第 {i} 条 text 不一致"
                assert (m.get("attachments") or []) == (d.get("attachments") or []), (
                    f"第 {i} 条 attachments 不一致"
                )
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass
            SessionStore._instance = None
            DatabaseManager._instance = None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

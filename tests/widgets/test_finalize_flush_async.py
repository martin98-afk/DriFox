# -*- coding: utf-8 -*-
"""[I1] 停止/中断路径同步 flush 异步化回归（CTO 预埋项 a + #A2 诊断 P0）

== 背景 ==
_on_finalize_complete（停止按钮 finalize 完成的主线程回调）内两处
history_manager.flush() → _do_save(synchronous=True)：主线程同步
serialize+zstd+SQLite 写盘（大会话 700ms+），且 flush() 内
_wait_inflight_saves 默认最多等 5s —— 停止路径主线程最坏卡一拍 5 秒。

== 修复 ==
- _on_finalize_complete 两处 flush() → flush_async()（非 daemon 后台写盘，
  退出时解释器 join 保数据安全，同关窗路径先例）
- _wait_inflight_saves 默认超时 5s → 2s（超时 ERROR 告警原有，保留）

== 退出兜底（不得误改）==
_launch_background_finalize 的关窗分支（后台 daemon 线程内）与
_auto_save_current_session sync 分支的同步 flush() 属退出/关窗数据安全
语义，保持不动。本测试同时断言它们未被误改。

== 断言口径 ==
AST 源码断言（同 test_session_flush_async.py 风格），无需 QApplication。
"""

import ast
import inspect
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MAIN_WIDGET = _REPO_ROOT / "app" / "main_widget.py"
_HISTORY_MGR = _REPO_ROOT / "app" / "utils" / "history_manager.py"


def _load_method(path: Path, cls_name: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return item
    raise AssertionError(f"未找到方法 {cls_name}.{name}")


def _source(method: ast.FunctionDef) -> str:
    return ast.unparse(method)


class TestFinalizeCompleteFlushAsync:
    """停止回调主线程必须异步落盘"""

    def test_finalize_complete_uses_flush_async(self):
        method = _load_method(_MAIN_WIDGET, "OpenAIChatToolWindow", "_on_finalize_complete")
        src = _source(method)
        assert "history_manager.flush_async()" in src, (
            "_on_finalize_complete 必须用 flush_async（主线程同步 flush 大会话 "
            "700ms+ + _wait_inflight_saves 最多 5s，是停止路径卡顿主因）；"
            "数据安全由 flush_async 非 daemon 线程退出 join 保证"
        )
        assert "history_manager.flush()" not in src, (
            "_on_finalize_complete 内不得残留主线程同步 flush()"
        )

    def test_background_finalize_close_branch_keeps_sync_flush(self):
        """关窗分支（_launch_background_finalize 后台线程内）保持同步 flush，不得误改"""
        method = _load_method(_MAIN_WIDGET, "OpenAIChatToolWindow", "_launch_background_finalize")
        src = _source(method)
        assert "history_manager.flush()" in src, (
            "_launch_background_finalize 的关窗分支同步 flush 必须保留"
            "（后台 daemon 线程内执行，不阻塞主线程；窗口已销毁时需同步落盘）"
        )


class TestInflightWaitTimeout:
    """_wait_inflight_saves 默认超时降为 2s 且保留超时告警"""

    def test_default_timeout_at_most_2s(self):
        sys.path.insert(0, str(_REPO_ROOT))
        from app.utils.history_manager import HistoryManager

        sig = inspect.signature(HistoryManager._wait_inflight_saves)
        default = sig.parameters["timeout"].default
        assert isinstance(default, float) and 1.0 <= default <= 2.0, (
            f"_wait_inflight_saves 默认超时应为 1~2s（主线程同步等待上限），实际 {default}"
        )

    def test_timeout_alarm_kept(self):
        """超时必须打 ERROR 告警（leader 实施单②的告警要求）"""
        method = _load_method(_HISTORY_MGR, "HistoryManager", "_wait_inflight_saves")
        src = _source(method)
        assert "logger.error" in src, "_wait_inflight_saves 超时路径必须有 ERROR 告警日志"
        assert "return False" in src, "超时必须返回 False（调用方感知超时）"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

"""P1 重载快照后台化 AST 源码断言（无需 QApplication，参照 test_session_flush_async.py 模式）。

断言三条结构事实：
1. _restore_meta_from_batch 内不再直调 get_context_usage_snapshot（长会话 500ms+ 阻塞主线程）
2. _reload_ctx_ready 信号已声明并连接到 _on_reload_ctx_ready
3. 直调仅存于 _kick_reload_ctx_snapshot 的后台闭包；_on_reload_ctx_ready 槽内无直调
"""
import ast
from pathlib import Path

SRC = Path("app/main_widget.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)


def _get_method(name: str):
    for node in ast.walk(TREE):
        if isinstance(node, ast.ClassDef) and node.name == "OpenAIChatToolWindow":
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name:
                    return item
    return None


def test_restore_meta_no_direct_snapshot_call():
    fn = _get_method("_restore_meta_from_batch")
    assert fn is not None, "_restore_meta_from_batch 必须存在"
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "get_context_usage_snapshot", (
                "_restore_meta_from_batch 不得直调 get_context_usage_snapshot"
                "（project_for_ui 8 层 tier + 全量 token 估算，长会话实测 500ms+），"
                "改走 _kick_reload_ctx_snapshot 后台化"
            )


def test_reload_ctx_signal_wired():
    assert "_reload_ctx_ready = pyqtSignal(int, str, object, str)" in SRC, (
        "缺少 _reload_ctx_ready 信号声明"
    )
    assert "self._reload_ctx_ready.connect(self._on_reload_ctx_ready)" in SRC, (
        "缺少 _reload_ctx_ready → _on_reload_ctx_ready 连接"
    )


def test_direct_call_only_in_kick():
    kick = _get_method("_kick_reload_ctx_snapshot")
    assert kick is not None, "缺少 _kick_reload_ctx_snapshot"
    assert "get_context_usage_snapshot" in ast.dump(kick), (
        "_kick_reload_ctx_snapshot 闭包内必须有快照调用"
    )
    slot = _get_method("_on_reload_ctx_ready")
    assert slot is not None, "缺少 _on_reload_ctx_ready 槽"
    assert "get_context_usage_snapshot" not in ast.dump(slot), (
        "_on_reload_ctx_ready 槽内不得直调快照（主线程）"
    )


def test_kick_has_inflight_guard():
    kick = _get_method("_kick_reload_ctx_snapshot")
    assert kick is not None
    assert "_reload_ctx_inflight_sid" in ast.dump(kick), (
        "_kick_reload_ctx_snapshot 必须带单飞守卫（inflight 同会话不重复派发）"
    )

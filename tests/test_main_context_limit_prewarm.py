# -*- coding: utf-8 -*-
"""[E7] 启动期 resolve_context_limit 后台预热回归。

覆盖：
1. AST/源码守卫：预热函数存在、daemon 线程、模型名取不到即跳过（尽力而为）
2. 时序红线：预热线程派发在 models.dev 后台同步之后（不改变现有启动顺序）
3. 导入路径 smoke：预热线程调用的 resolve_context_limit 可独立执行（子线程）
"""

import ast
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtCore")  # main.py import 链含 Qt（guard 保护，import 安全）

import main as main_mod  # noqa: E402

_MAIN = Path(__file__).resolve().parent.parent / "main.py"

def _deferred_startup_src() -> str:
    tree = ast.parse(_MAIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_deferred_startup":
            return ast.get_source_segment(_MAIN.read_text(encoding="utf-8"), node) or ""
    raise AssertionError("找不到 _deferred_startup 函数")

def test_prewarm_function_defined_and_daemon():
    """预热函数存在且以 daemon 线程派发（不阻塞启动主路径）。"""
    src = _deferred_startup_src()
    assert "def _prewarm_context_limit()" in src, "预热函数应定义在 _deferred_startup 内"
    assert "target=_prewarm_context_limit, daemon=True" in src, "必须 daemon 线程派发"

def test_prewarm_skips_without_model_name():
    """模型名取不到就跳过（尽力而为，不强行预热）。"""
    src = _deferred_startup_src()
    assert 'llm_cfg.get("模型名称")' in src, "应有模型名空判跳过分支"
    assert "return" in src.split("llm_cfg.get(\"模型名称\")")[1].split("resolve_context_limit")[0], (
        "取不到模型名应在调用 resolve 前返回"
    )

def test_prewarm_thread_after_models_dev_sync():
    """时序红线：预热线程派发位于 models.dev 后台同步线程之后，不改变启动顺序。"""
    src = _deferred_startup_src()
    i_sync = src.find("target=_sync_models_dev")
    i_prewarm = src.find("target=_prewarm_context_limit")
    assert i_sync != -1 and i_prewarm != -1, "两个后台线程都应存在"
    assert i_sync < i_prewarm, "预热必须排在 models.dev 同步之后（不改变现有时序）"

def test_resolve_context_limit_importable_and_callable_in_thread():
    """导入路径 smoke：预热调用链在子线程可独立执行（真实函数、真实注册表）。"""
    from app.core.modelmeta.model_capabilities import resolve_context_limit

    result = {}

    def _run():
        try:
            result["v"] = resolve_context_limit({"模型名称": "nonexistent-model-x", "provider_name": ""})
        except Exception as e:  # noqa: BLE001 - 预热语义：异常也不该崩线程
            result["err"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=30)
    assert not t.is_alive(), "预热链路不得在子线程卡死"
    assert "err" not in result, f"resolve_context_limit 不应抛错：{result.get('err')}"
    assert result.get("v", 0) >= 1, "返回值恒 >= 1"

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

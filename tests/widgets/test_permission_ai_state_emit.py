# -*- coding: utf-8 -*-
"""权限请求回调触发 ai_state_changed("question") 回归测试

== 问题描述 ==
``OpenAIChatToolWindow._on_permission_approval_requested`` 原本没有像
``_on_question_asked`` 那样调用 ``self._set_ai_state("question")``，
导致 TabManagerWindow 中 ``_on_ai_state_changed`` 听不到状态变化，
``update_tab_question`` 不会被调用，TabPanel 边框没有"问题"动画指示。

== 修复 ==
在 ``_on_permission_approval_requested`` 顶部（destroyed 检查之后）加上
``self._set_ai_state("question")``。

== 回归测试 ==
AST 静态检查源码，确认：
1. 该方法存在
2. 方法体内第一段实质语句即包含 ``_set_ai_state("question")`` 调用
"""

import ast
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_PATH = _REPO_ROOT / "app" / "main_widget.py"


def _load_module_ast() -> ast.Module:
    return ast.parse(_SRC_PATH.read_text(encoding="utf-8"), filename=str(_SRC_PATH))


def _get_class(tree: ast.Module, class_name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise AssertionError(f"未找到类 {class_name}")


def _get_method(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"未找到方法 {cls.name}.{name}")


def _calls_set_ai_state_question(method: ast.FunctionDef) -> bool:
    """方法体任何位置出现 ``self._set_ai_state("question")`` 即视为通过

    使用 BFS 遍历整棵 AST 子树，避免遗漏被 if/for 包裹或 ast.Try 内的情况。
    """
    target = "question"

    def _is_call(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        # 形如 self._set_ai_state("question")
        if isinstance(func, ast.Attribute) and func.attr == "_set_ai_state" and node.args:
            arg0 = node.args[0]
            return isinstance(arg0, ast.Constant) and arg0.value == target
        return False

    for sub in ast.walk(method):
        if _is_call(sub):
            return True
    return False


def test_on_permission_approval_requested_emits_question_state():
    """AST 检查：_on_permission_approval_requested 必须 _set_ai_state('question')"""
    tree = _load_module_ast()
    cls = _get_class(tree, "OpenAIChatToolWindow")
    method = _get_method(cls, "_on_permission_approval_requested")
    assert _calls_set_ai_state_question(method), (
        "_on_permission_approval_requested 必须调用 self._set_ai_state('question')，"
        "否则 TabPanel 不会进入 question 动画分支。"
    )


def test_on_question_asked_still_emits_question_state():
    """保护 _on_question_asked 不被回归（保持对比例参照）"""
    tree = _load_module_ast()
    cls = _get_class(tree, "OpenAIChatToolWindow")
    method = _get_method(cls, "_on_question_asked")
    assert _calls_set_ai_state_question(method), "_on_question_asked 缺少 _set_ai_state('question')，原有功能被回归。"


if __name__ == "__main__":
    test_on_permission_approval_requested_emits_question_state()
    test_on_question_asked_still_emits_question_state()
    print("[permission-ai-state-emit] all passed")

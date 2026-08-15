# -*- coding: utf-8 -*-
"""新建项目后 Tab 项目图标同步回归测试

背景：切换已有项目（_on_project_selected）末尾会调用 _update_tab_icon 同步
Tab 面板的项目图标；而新建项目（_on_new_project_created）曾缺失该调用，
导致新建项目后 Tab 图标停留在旧项目（仅依赖 windowTitleChanged 信号间接触发，
标题未变化时永不更新）。
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MAIN_WIDGET_PATH = REPO_ROOT / "app" / "main_widget.py"


def _get_method_src(method_name: str) -> ast.FunctionDef:
    """读取 main_widget.py 中指定方法的方法体 AST"""
    src = MAIN_WIDGET_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src, filename="main_widget.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "OpenAIChatToolWindow":
            for stmt in node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == method_name:
                    return stmt
    raise AssertionError(f"未找到方法 OpenAIChatToolWindow.{method_name}")


def _method_contains_call(method: ast.FunctionDef, call_name: str) -> bool:
    """检查方法体内是否存在对 call_name 的调用"""
    for node in ast.walk(method):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == call_name:
                return True
            if isinstance(func, ast.Attribute) and func.attr == call_name:
                return True
    return False


class TestNewProjectSyncsTabIcon:
    """新建项目必须像切换项目一样显式同步 Tab 项目图标"""

    def test_on_new_project_created_calls_update_tab_icon(self):
        """_on_new_project_created 方法体必须包含 _update_tab_icon 调用

        修复前：新建项目不更新 Tab 图标（仅靠 windowTitleChanged 兜底）。
        修复后：与 _on_project_selected 对齐，显式调用 _update_tab_icon。
        """
        method = _get_method_src("_on_new_project_created")
        assert _method_contains_call(method, "_update_tab_icon"), (
            "新建项目缺少 Tab 项目图标同步：_on_new_project_created 未调用 _update_tab_icon"
        )

    def test_on_new_project_created_icon_sync_inside_tab_mode_guard(self):
        """图标同步必须位于 enable_tab_manager 守卫内（与切换项目写法一致）"""
        method = _get_method_src("_on_new_project_created")
        # 找到 _update_tab_icon 调用所在的最小包含链，向上必须经过
        # cfg.enable_tab_manager.value 的判断
        found = False
        for node in ast.walk(method):
            if isinstance(node, ast.Call):
                func = node.func
                is_update = (isinstance(func, ast.Name) and func.id == "_update_tab_icon") or (
                    isinstance(func, ast.Attribute) and func.attr == "_update_tab_icon"
                )
                if not is_update:
                    continue
                # 向上回溯父链
                parent = _find_parent(method, node)
                while parent is not None:
                    if isinstance(parent, ast.If):
                        test_src = ast.dump(parent.test)
                        if "enable_tab_manager" in test_src or "cfg" in test_src:
                            found = True
                            break
                    parent = _find_parent(method, parent)
                if found:
                    break
        assert found, "_update_tab_icon 调用必须位于 enable_tab_manager 守卫内"


def _find_parent(root: ast.AST, target: ast.AST):
    """查找 target 在 root 中的直接父节点"""
    for node in ast.walk(root):
        for child in ast.iter_child_nodes(node):
            if child is target:
                return node
    return None

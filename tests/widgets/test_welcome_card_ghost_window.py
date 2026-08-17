# -*- coding: utf-8 -*-
"""回归测试：快速批量建标签页时欢迎卡片不得弹出"幽灵窗口"。

根因（2026-08-18）：
- 欢迎卡片的 QWebEngineView 由 MessageCard.ensure_rendered 创建；而同类的
  _schedule_render（setContent 路径）已有 `if not self.isVisible(): _render_deferred=True;
  return` 的可见性门控，ensure_rendered（创建 viewer 路径）却缺这个门控。
- 快速批量建标签页时，每个新会话触发欢迎卡片渲染（_add_chat_widget 把卡片加入
  200ms 懒渲染队列 _pending_lazy_cards，_process_next_lazy_batch 调用
  card.ensure_rendered）。但 Tab 管理器只用 QStackedWidget 承载窗口，只有最后一个
  标签页可见，前 N-1 个在 200ms 后触发 ensure_rendered 时窗口已不可见（被后续
  add_window 挤出 current）。
- QWebEngineView 在 Windows 上创建原生 HWND 子窗口（见 CodeWebViewer._hide_for_dialog
  注释），父链无有效 native window 句柄时 Chromium 弹出独立原生窗口（幽灵窗口）。
- 修复：ensure_rendered 加可见性门控（不可见→_render_deferred=True 并 return，
  viewer 创建延后）；MessageCard.showEvent 在窗口切回可见时补渲（此时父 HWND 已就绪）。

测试策略（纯 AST，不实例化 Qt，可在无头/CI 环境运行）：
- 断言 ensure_rendered 的 _do_ensure_rendered 在创建 viewer 前检查 self.isVisible()，
  不可见时设置 _render_deferred=True 并 return。
- 断言 MessageCard.showEvent 在 _render_deferred 且未渲染时调用 self.ensure_rendered() 补渲。
- 断言 _schedule_render 同样具备可见性门控（对称性，防回归）。
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC = (REPO_ROOT / "app" / "widgets" / "message_card.py").read_text(encoding="utf-8")
_TREE = ast.parse(SRC, filename="message_card.py")


def _class(name: str) -> ast.ClassDef:
    for node in ast.walk(_TREE):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"未找到类 {name}")


def _method(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"类 {cls.name} 未找到方法 {name}")


def _nested_func(method: ast.FunctionDef, name: str) -> ast.FunctionDef:
    for node in method.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"方法 {method.name} 内未找到嵌套函数 {name}")


def _method_text(method: ast.AST) -> str:
    return "\n".join(SRC.splitlines()[method.lineno - 1: method.end_lineno])


def _is_not_visible_test(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and isinstance(test.operand, ast.Call)
        and isinstance(test.operand.func, ast.Attribute)
        and test.operand.func.attr == "isVisible"
    )


def _branch_sets_flag_and_returns(if_node: ast.If) -> bool:
    sets_flag = False
    has_return = False
    for stmt in if_node.body:
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Attribute) and t.attr == "_render_deferred":
                    if isinstance(stmt.value, ast.Constant) and stmt.value.value is True:
                        sets_flag = True
        if isinstance(stmt, ast.Return):
            has_return = True
    return sets_flag and has_return


def test_ensure_rendered_guards_invisible_before_viewer():
    """ensure_rendered 必须在创建 QWebEngineView 前门控不可见，避免弹出幽灵窗口。"""
    mc = _class("MessageCard")
    er = _method(mc, "ensure_rendered")
    do = _nested_func(er, "_do_ensure_rendered")

    # 找到 `if not self.isVisible(): ... _render_deferred=True; return`
    guard = None
    for stmt in do.body:
        if isinstance(stmt, ast.If) and _is_not_visible_test(stmt.test):
            guard = stmt
            break
    assert guard is not None, "ensure_rendered._do_ensure_rendered 缺少 isVisible 可见性守卫"
    assert _branch_sets_flag_and_returns(guard), (
        "isVisible 守卫分支必须（不可见时）设置 _render_deferred=True 并 return"
    )

    # 守卫必须位于 CodeWebViewer( 实例化之前
    text = _method_text(do)
    idx_guard = text.find("isVisible()")
    idx_viewer = text.find("CodeWebViewer(")
    assert idx_guard != -1 and idx_viewer != -1, "无法定位守卫与 CodeWebViewer 实例化"
    assert idx_guard < idx_viewer, "可见性守卫未在 QWebEngineView 实例化之前"


def test_message_card_show_event_resumes_deferred_render():
    """窗口切回可见时，MessageCard.showEvent 必须补渲此前推迟的欢迎卡片 viewer。"""
    mc = _class("MessageCard")
    show = _method(mc, "showEvent")
    text = _method_text(show)
    # 必须在 _render_deferred 判定分支内调用 self.ensure_rendered() 补渲
    assert "ensure_rendered" in text, "MessageCard.showEvent 未调用 self.ensure_rendered() 补渲"
    assert "_render_deferred" in text, (
        "MessageCard.showEvent 应在 _render_deferred 分支内决定是否补渲"
    )


def test_schedule_render_also_guards_invisible():
    """_schedule_render（CodeWebViewer 的 setContent 路径）同样具备可见性门控。

    对称性：MessageCard.ensure_rendered（创建 viewer 路径）与 CodeWebViewer.
    _schedule_render（setContent 渲染路径）都必须在不可见时推迟，避免弹出幽灵窗口。
    """
    cv = _class("CodeWebViewer")
    sr = _method(cv, "_schedule_render")
    # _schedule_render 内含 `if not self.isVisible(): self._render_deferred = True; return`
    found = False
    for node in ast.walk(sr):
        if isinstance(node, ast.If) and _is_not_visible_test(node.test):
            found = _branch_sets_flag_and_returns(node)
            break
    assert found, "_schedule_render 缺少 isVisible 可见性门控（与 ensure_rendered 不一致）"

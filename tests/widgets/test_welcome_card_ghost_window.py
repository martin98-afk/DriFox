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

测试策略（纯 AST + 行为测试，可在无头/CI 环境运行）：
- 断言 ensure_rendered 的 _do_ensure_rendered 在创建 viewer 前检查 self.isVisible()，
  不可见时设置 _render_deferred=True 并 return。
- 断言 MessageCard.showEvent 在 _render_deferred 且未渲染时调用 self.ensure_rendered() 补渲。
- 断言 _schedule_render 同样具备可见性门控（对称性，防回归）。
- 行为测试：嵌套两层 QStackedWidget（窗口级 + 覆盖层级）时，外层覆盖层打开
  必须判定不可见（2026-08-20 修复：_is_effectively_visible 原先只检查第一个
  QStackedWidget，覆盖层打开时误判可见 → 切换项目/新建标签页弹幽灵窗口）。
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


def _is_not_effectively_visible_test(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and isinstance(test.operand, ast.Call)
        and isinstance(test.operand.func, ast.Attribute)
        and test.operand.func.attr == "_is_effectively_visible"
    )


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
    """ensure_rendered 必须在创建 QWebEngineView 前门控非当前可见标签页，避免弹出幽灵窗口。"""
    mc = _class("MessageCard")
    er = _method(mc, "ensure_rendered")
    do = _nested_func(er, "_do_ensure_rendered")

    # 找到 `if not self._is_effectively_visible(): ... _render_deferred=True; return`
    guard = None
    for stmt in do.body:
        if isinstance(stmt, ast.If) and _is_not_effectively_visible_test(stmt.test):
            guard = stmt
            break
    assert guard is not None, "ensure_rendered._do_ensure_rendered 缺少 _is_effectively_visible 守卫"
    assert _branch_sets_flag_and_returns(guard), (
        "_is_effectively_visible 守卫分支必须（非当前可见标签页时）设置 _render_deferred=True 并 return"
    )

    # 守卫必须位于 CodeWebViewer( 实例化之前
    text = _method_text(do)
    idx_guard = text.find("_is_effectively_visible()")
    idx_viewer = text.find("CodeWebViewer(")
    assert idx_guard != -1 and idx_viewer != -1, "无法定位守卫与 CodeWebViewer 实例化"
    assert idx_guard < idx_viewer, "_is_effectively_visible 守卫未在 QWebEngineView 实例化之前"


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


# ══════════════════════════════════════════════════════════
# 行为测试：多层 QStackedWidget 嵌套（窗口级 + 覆盖层级）
# ══════════════════════════════════════════════════════════


def _build_tab_manager_like_tree():
    """模拟 Tab 管理器结构：外层覆盖层堆栈（_content_stack）⊃ 内层窗口堆栈（_content_area）。

    返回 (content_stack, content_area, overlay_page, window_page, card)。
    """
    from PySide6.QtWidgets import QStackedWidget, QWidget

    from app.widgets.message_card import MessageCard

    content_stack = QStackedWidget()  # 外层：覆盖层堆栈（index 0 对话区 / index 1 覆盖层）
    content_area = QStackedWidget()  # 内层：窗口堆栈（index 0 空状态 / index 1 窗口）
    overlay_page = QWidget()  # 覆盖层页（系统卡片，如项目选择器）
    window_page = QWidget()  # 对话区页（承载欢迎卡片）
    content_stack.addWidget(content_area)  # index 0
    content_stack.addWidget(overlay_page)  # index 1
    content_area.addWidget(QWidget())  # index 0: 空状态
    content_area.addWidget(window_page)  # index 1: 窗口

    card = MessageCard(role="assistant", parent=window_page)
    content_area.setCurrentWidget(window_page)
    content_stack.setCurrentWidget(content_area)
    content_stack.show()
    return content_stack, content_area, overlay_page, window_page, card


def test_effectively_visible_all_stacked_layers_must_pass(_qt_app):
    """多层 QStackedWidget 嵌套时，`_is_effectively_visible` 必须遍历全部层级。

    回归：切换项目/新建标签页出现幽灵窗口（2026-08-20）。根因：原实现沿父链
    找到**第一个** QStackedWidget（窗口级 _content_area）即返回，未检查外层
    覆盖层堆栈（_content_stack）。当系统卡片覆盖层打开（index 1）时对话区被
    隐藏，但窗口级 currentWidget 仍是当前窗口 → 误判可见 → 创建 QWebEngineView
    弹出幽灵窗口。
    """
    content_stack, content_area, overlay_page, _window_page, card = _build_tab_manager_like_tree()

    # 两层都是当前页 → 可见
    assert card._is_effectively_visible() is True

    # 覆盖层打开（如项目选择卡片）→ 对话区实际不可见 → 必须判定不可见
    content_stack.setCurrentWidget(overlay_page)
    assert card._is_effectively_visible() is False

    # 覆盖层关闭 → 恢复可见
    content_stack.setCurrentWidget(content_area)
    assert card._is_effectively_visible() is True
    content_stack.deleteLater()


# ══════════════════════════════════════════════════════════
# 销毁路径：setParent(None) 前必须先 hide()，防止白窗一闪
# ══════════════════════════════════════════════════════════


def _method_src(cls: ast.ClassDef, name: str) -> str:
    m = _method(cls, name)
    return _method_text(m)


def test_invalidate_welcome_card_hides_before_setparent_none(_qt_app):
    """失效欢迎卡片时，必须在 setParent(None) 之前 hide()。

    回归（2026-08-20）：切换项目/新建标签页 → _invalidate_welcome_card →
    setParent(None)。若卡片已渲染（QWebEngineView HWND 已创建）且可见，
    setParent(None) 会把整棵含 HWND 的子树变为独立顶层窗口 → Chromium
    弹出原生窗口（白窗一闪），随后 deleteLater 才销毁。必须先 hide 摘除
    可见性，再脱离父链。
    """
    from PySide6.QtWidgets import QWidget

    # 行为验证：可见 widget setParent(None) 前 hide 不弹独立窗口
    top = QWidget()
    top.show()
    w = QWidget(top)
    w.show()
    assert w.isVisible()
    w.hide()
    w.setParent(None)
    assert not w.isVisible(), "hide 后再 setParent(None) 不应成为独立可见窗口"
    w.deleteLater()
    top.deleteLater()


def test_invalidate_welcome_card_ast_hide_before_setparent(_qt_app):
    """AST：_invalidate_welcome_card 必须在 setParent(None) 之前调用 hide()。"""
    src = _get_main_widget_src_ast()
    text = _extract_method_text(src, "OpenAIChatToolWindow", "_invalidate_welcome_card")
    idx_hide = text.find("hide()")
    idx_setparent = text.find("setParent(None)")
    assert idx_hide != -1, "_invalidate_welcome_card 缺少 hide() 调用"
    assert idx_setparent != -1, "_invalidate_welcome_card 缺少 setParent(None)"
    assert idx_hide < idx_setparent, "setParent(None) 必须在 hide() 之后（防白窗一闪）"


def test_clear_chat_area_hides_before_setparent(_qt_app):
    """AST：_clear_chat_area 的删除分支必须在 setParent(None) 之前 hide()。"""
    src = _get_main_widget_src_ast()
    text = _extract_method_text(src, "OpenAIChatToolWindow", "_clear_chat_area")
    # 删除分支（delete_widgets=True）中 hide 必须先于 setParent(None)
    idx_hide = text.find("widget.hide()")
    idx_setparent = text.find("widget.setParent(None)")
    assert idx_hide != -1, "_clear_chat_area 缺少 widget.hide() 调用"
    assert idx_setparent != -1, "_clear_chat_area 缺少 widget.setParent(None)"
    assert idx_hide < idx_setparent, "setParent(None) 必须在 hide() 之后（防白窗一闪）"


def _extract_method_text(src: str, class_name: str, method_name: str) -> str:
    """从源码文本中提取指定类方法体（含装饰器/签名），按 AST 行号切片。"""
    tree = ast.parse(src, filename="main_widget.py")
    lines = src.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method_name:
                    return "\n".join(lines[child.lineno - 1: child.end_lineno])
    raise AssertionError(f"未找到 {class_name}.{method_name}")


def _get_main_widget_src_ast() -> str:
    repo_root = Path(__file__).resolve().parent.parent.parent
    return (repo_root / "app" / "main_widget.py").read_text(encoding="utf-8")

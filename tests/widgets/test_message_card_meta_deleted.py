# -*- coding: utf-8 -*-
"""回归测试：MessageCard 元信息访问在 label 被 C++ 销毁后不抛异常。

根因
----
用户选择模型时触发未处理异常：
    message_card.py set_meta_info → self._footer_tokens_label.setText(text)
    RuntimeError: wrapped C/C++ object of type QLabel has been deleted

链路：send 失败回滚分支 `assistant_card.deleteLater()` 后未清
`_current_assistant_card`；deleteLater 是延迟删除（下一轮事件循环 C++ 对象
才销毁），期间 `_refresh_context_usage_indicator` 等仍取到该卡片并调用
`set_meta_info` → 访问已删除 QLabel → RuntimeError。

修复分三层：
  A. `main_widget` send 失败回滚补 `_current_assistant_card = None`（根因）
  B. `main_widget._refresh_context_usage_indicator` 加 `_is_sip_deleted` 检查（主防线）
  C. `message_card.set_meta_info` / `_refresh_footer_separators` 内部
     label 访问包 try/except RuntimeError（兜底）

本测试覆盖 C 层：label 被 `deleteLater()` + `processEvents()` 强制销毁后，
`set_meta_info` 与 `_refresh_footer_separators` 必须静默不抛异常。
"""

import sys

from PyQt5.QtWidgets import QApplication

from app.widgets.message_card import MessageCard


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


def _make_card() -> MessageCard:
    """构造 assistant MessageCard（__init__ 自动创建 footer labels）。"""
    _ensure_qapp()
    return MessageCard(role="assistant")


def _destroy_label(card: MessageCard, attr: str):
    """deleteLater + processEvents 强制 C++ 侧销毁指定 footer label。"""
    label = getattr(card, attr)
    assert label is not None, f"{attr} 应为 None（前置不满足）"
    label.deleteLater()
    QApplication.processEvents()


def test_set_meta_info_token_survives_deleted_label():
    """tokens label 被销毁后 set_meta_info(token_usage=...) 不抛异常。"""
    card = _make_card()
    _destroy_label(card, "_footer_tokens_label")
    # 修复前：_footer_tokens_label.setText → RuntimeError；修复后静默通过
    card.set_meta_info(token_usage={"total": 1000})
    # token 块之后仍会调 _refresh_footer_separators（其内部访问已删 label），
    # 同样不得抛异常 —— 到这里说明整条链路安全
    assert True


def test_set_meta_info_elapsed_survives_deleted_label():
    """elapsed label 被销毁后 set_meta_info(elapsed=...) 不抛异常。"""
    card = _make_card()
    _destroy_label(card, "_footer_elapsed_label")
    card.set_meta_info(elapsed=3.0)
    assert True


def test_refresh_footer_separators_survives_deleted_labels():
    """tokens + elapsed label 均被销毁后 _refresh_footer_separators 不抛异常。"""
    card = _make_card()
    _destroy_label(card, "_footer_tokens_label")
    _destroy_label(card, "_footer_elapsed_label")
    card._refresh_footer_separators()
    assert True


def test_set_meta_info_both_survives_deleted_labels():
    """elapsed + token_usage 同时传入且两 label 均已销毁 → 不抛异常。"""
    card = _make_card()
    _destroy_label(card, "_footer_tokens_label")
    _destroy_label(card, "_footer_elapsed_label")
    card.set_meta_info(elapsed=3.0, token_usage={"total": 1000})
    assert True


def test_context_usage_indicator_guards_sip_deleted():
    """AST：_refresh_context_usage_indicator 的 set_meta_info 调用前必须含 isdeleted 检查。

    对应修复 B 层（主防线）：即使 A 层漏清引用，此处也能拦截已销毁卡片。
    """
    import ast
    import re
    from pathlib import Path

    src_path = Path(__file__).resolve().parent.parent.parent / "app" / "main_widget.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))

    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_refresh_context_usage_indicator":
            target = node
            break
    assert target is not None, "未找到 _refresh_context_usage_indicator 方法"

    import textwrap

    func_src = textwrap.dedent(ast.unparse(target))

    # set_meta_info 调用前必须检查 _is_sip_deleted
    assert re.search(r"_is_sip_deleted\s*\(", func_src), (
        "_refresh_context_usage_indicator 必须调用 _is_sip_deleted 守卫（B 层防线）"
    )
    assert "set_meta_info" in func_src, "_refresh_context_usage_indicator 必须调用 set_meta_info"


def test_send_fail_rollback_clears_current_card():
    """AST：send_message_to_engine 失败回滚分支必须补 `_current_assistant_card = None`。

    对应修复 A 层（根因）：对照 L13885-13886 规范写法。
    """
    import ast
    import re
    from pathlib import Path

    src_path = Path(__file__).resolve().parent.parent.parent / "app" / "main_widget.py"
    src = src_path.read_text(encoding="utf-8")

    # 定位 send 失败回滚分支（deleteLater 后紧跟 return 的分支）
    m = re.search(
        r"send_message_to_engine\(user_text[^)]*\)[^:]*:\s*\n"
        r"(?P<body>(?:[ \t]+[^\n]*\n)+?)"
        r"[ \t]+return\b",
        src,
    )
    assert m, "未找到 send 失败回滚分支（send_message_to_engine 失败后 return）"

    body = m.group("body")
    assert "deleteLater()" in body, "回滚分支必须 deleteLater 助手卡片"
    assert "_current_assistant_card = None" in body, (
        "回滚分支必须补 `_current_assistant_card = None`（根因修复，防已删卡片被继续访问）"
    )

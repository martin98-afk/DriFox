# -*- coding: utf-8 -*-
"""发送输入元数据 + 撤回保真回填（2026-09-16 回溯乱码 / 撤回渲染异常回归）

背景
----
带附件消息发送时占位符被替换为完整路径，消息上只留终态文本：
- 撤回（撤销到这里）回填的是不可逆的展开终态，附件 chips 丢失、
  再次编辑发送必然偏离原消息（渲染"不正常"的根因）
- @提及胶囊展开与路径替换零分隔粘连（"@空D:\..."），mention 语义失效
- 历史恢复全选导致键入首字符即静默清空整条正文

修复
----
- 发送链路在消息上挂 ``_raw_input_text``（占位符形式正文）+
  ``_input_attachments``（全量附件路径），撤回依此保真回填
- 占位符替换为路径时前后补空格分隔
- mention 还原后边界白名单补 ``[``（``@空[[b]]`` 场景）
- 历史恢复光标置尾替代全选

设计说明：重依赖用 __new__ 绕过 + MagicMock 隔离（同 test_team_mail_preserve_input）
"""

import ast
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.chat_session import ChatSession
from app.core.message_content import normalize_message

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ==================== 数据层：session 消息标记 ====================


def test_add_user_message_persists_input_metadata():
    s = ChatSession(name="t")
    s.add_user_message(
        "正文",
        _input_attachments=["D:/x/hub", "D:/y/a.png"],
        _raw_input_text="当前 [[hub]] [[a.png]]",
    )
    assert s.messages[-1]["_input_attachments"] == ["D:/x/hub", "D:/y/a.png"]
    assert s.messages[-1]["_raw_input_text"] == "当前 [[hub]] [[a.png]]"


def test_add_user_message_skips_empty_input_metadata():
    s = ChatSession(name="t")
    s.add_user_message("纯文本", _input_attachments=[], _raw_input_text="")
    assert "_input_attachments" not in s.messages[-1]
    assert "_raw_input_text" not in s.messages[-1]


def test_normalize_message_keeps_input_metadata():
    """白名单验证：字段缺失会被 consolidate_messages 剥掉，撤回无法保真回填。"""
    msg = normalize_message(
        {
            "role": "user",
            "content": "正文",
            "_input_attachments": ["D:/x/hub"],
            "_raw_input_text": "[[hub]] 看下",
            "ts_ms": 1,
        }
    )
    assert msg["_input_attachments"] == ["D:/x/hub"]
    assert msg["_raw_input_text"] == "[[hub]] 看下"


def test_normalize_message_drops_invalid_input_metadata():
    msg = normalize_message(
        {"role": "user", "content": "x", "_input_attachments": "bad", "_raw_input_text": 123, "ts_ms": 1}
    )
    assert "_input_attachments" not in msg
    assert "_raw_input_text" not in msg


# ==================== 发送链路：engine_kwargs 透传 ====================


def _find_nested_func(class_node: ast.ClassDef, name: str):
    for node in ast.walk(class_node):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


@pytest.mark.parametrize("key", ["_input_attachments", "_raw_input_text"])
def test_deferred_send_passes_input_metadata_to_engine(key):
    """_do_deferred_send 必须把输入元数据放进 engine_kwargs，engine 侧才能写 session。"""
    src = (REPO_ROOT / "app" / "main_widget.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="main_widget.py")
    class_node = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "OpenAIChatToolWindow")
    deferred = _find_nested_func(class_node, "_do_deferred_send")
    assert deferred is not None, "未找到 _do_deferred_send 嵌套函数"

    found = False
    for node in ast.walk(deferred):
        if (
            isinstance(node, ast.Assign)
            and node.targets
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id == "engine_kwargs"
            and isinstance(node.targets[0].slice, ast.Constant)
            and node.targets[0].slice.value == key
        ):
            found = True
            break
    assert found, f"_do_deferred_send 未把 {key} 透传进 engine_kwargs"


# ==================== 发送构建：mention 与路径分隔 ====================


def _make_builder(attachments):
    from app.main_widget import OpenAIChatToolWindow

    inst = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    inst._attachments = list(attachments)
    return inst


def test_build_text_separates_mention_and_path():
    """@胶囊与占位符紧邻（用户删掉空格的编辑产物）→ 替换后不得粘连"""
    inst = _make_builder([r"D:\x\assistant_hub", r"C:\t\a.png"])
    out = inst._build_user_text_with_attachments("@空[[assistant_hub]]@空[[a.png]]@空")
    assert "@空D:" not in out, "mention 与路径不得粘连"
    assert "@空 D:\\x\\assistant_hub" in out
    assert "@空 C:\\t\\a.png" in out


def test_build_text_inline_placeholder_keeps_surroundings():
    """正文内联占位符：替换后前后留一个空格，多余空白压缩"""
    inst = _make_builder([r"C:\t\a.png"])
    out = inst._build_user_text_with_attachments("当前 [[a.png]] 上面的列表")
    assert out == f"当前 C:\\t\\a.png 上面的列表"


def test_build_text_unused_appended_with_newline():
    """未在正文引用的附件：保持既有行为，换行拼到末尾"""
    inst = _make_builder([r"C:\t\a.png", r"C:\t\b.png"])
    out = inst._build_user_text_with_attachments("看这两个")
    assert out == f"看这两个\nC:\\t\\a.png\nC:\\t\\b.png"


def test_build_text_no_attachments_clears_placeholders():
    """无附件：保持既有行为，清除残留占位符"""
    inst = _make_builder([])
    out = inst._build_user_text_with_attachments("当前 [[gone.png]] 上面的")
    assert out == "当前 上面的"


# ==================== 撤回：保真回填 ====================


def _make_undo_instance(msg, raw_text=None, atts=None):
    from app.main_widget import OpenAIChatToolWindow

    inst = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    stored = dict(msg or {})
    if raw_text is not None:
        stored["_raw_input_text"] = raw_text
    if atts is not None:
        stored["_input_attachments"] = atts
    inst._message_batch = [(stored, MagicMock(), MagicMock())] if stored else []
    inst._clear_attachments = MagicMock()
    inst._add_attachment = MagicMock()
    inst._rebuild_attachment_chips = MagicMock()
    inst.input_area = MagicMock()
    card = MagicMock()
    card._message_index = 0
    return inst, card


def test_restore_input_uses_raw_metadata(qapp, monkeypatch):
    """消息带原始输入元数据 → 回填占位符正文 + 重建 chips（保真路径）"""
    from app.main_widget import OpenAIChatToolWindow

    monkeypatch.setattr(os.path, "exists", lambda p: True)
    inst, card = _make_undo_instance(
        {"role": "user"}, raw_text="当前 [[hub]] [[a.png]]", atts=[r"D:\x\hub", r"C:\t\a.png"]
    )
    OpenAIChatToolWindow._restore_input_after_undo(inst, card)

    inst._rebuild_attachment_chips.assert_called_once()
    assert inst._add_attachment.call_count == 2
    # 正文回填为占位符形式，并做胶囊还原
    assert inst.input_area.setPlainText.call_args.args[0] == "当前 [[hub]] [[a.png]]"
    inst.input_area.convert_placeholders_to_mentions.assert_called_once_with([r"D:\x\hub", r"C:\t\a.png"])
    # 光标置尾
    assert inst.input_area.setTextCursor.called


def test_restore_input_skips_missing_files(qapp, monkeypatch):
    """失效路径不重建 chip（与历史模式附件恢复同口径）"""
    from app.main_widget import OpenAIChatToolWindow

    monkeypatch.setattr(os.path, "exists", lambda p: p == r"D:\alive")
    inst, card = _make_undo_instance({"role": "user"}, raw_text="看下", atts=[r"D:\alive", r"D:\dead"])
    OpenAIChatToolWindow._restore_input_after_undo(inst, card)

    inst._add_attachment.assert_called_once_with(r"D:\alive")
    inst.input_area.convert_placeholders_to_mentions.assert_called_once_with([r"D:\alive"])


def test_restore_input_fallback_without_metadata(qapp):
    """旧消息无标记 → 降级纯文本回填（旧行为）"""
    from app.main_widget import OpenAIChatToolWindow
    import app.main_widget as mw

    inst, card = _make_undo_instance({"role": "user", "content": "旧消息"})
    inst._message_batch = [({"role": "user", "content": "旧消息"}, MagicMock(), MagicMock())]
    with pytest.MonkeyPatch.context() as m:
        m.setattr(mw, "restore_input_from_card", MagicMock())
        OpenAIChatToolWindow._restore_input_after_undo(inst, card)
        mw.restore_input_from_card.assert_called_once_with(inst.input_area, card)
    inst._rebuild_attachment_chips.assert_not_called()


# ==================== 输入框：mention 还原 + 历史光标 ====================


def test_mention_span_matches_before_placeholder(qapp):
    """@空[[b]] 相邻形态（后边界补 `[`）：mention 可还原为胶囊"""
    from app.widgets.bottom_input_area import SendableTextEdit

    box = SendableTextEdit()
    spans = box._find_assistant_mention_spans("@空[[a.png]] @空", {"空": "#000000"})
    # 位置 0（[[ 前相邻）+ 串尾两处命中
    assert (0, 2) in [(s, e) for s, e, _ in spans]


def test_history_restore_cursor_at_end(qapp):
    """历史恢复后光标置尾（非全选）：键入首字符不再清空整条正文"""
    from app.widgets.bottom_input_area import SendableTextEdit

    box = SendableTextEdit()
    box.load_history([{"text": "hello 世界", "attachments": []}])
    box._navigate_history(1)
    assert box.toPlainText() == "hello 世界"
    assert box.textCursor().position() == len("hello 世界")

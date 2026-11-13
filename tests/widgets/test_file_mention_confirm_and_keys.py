# -*- coding: utf-8 -*-
"""@ 路径空格确认折叠 + 文件卡片键盘导航回归测试

覆盖两条 2026-11-13 新增链路：
1. _on_at_trigger_check 空白分支：@相对路径 + 空格 → 真实存在则折叠成
   文件胶囊并 emit fileMentionConfirmed；不存在保持字面文本（不做模糊匹配）。
2. FileMentionCard 新增键盘方法：go_up（Esc 分层）、select_page（跨页）、
   select_edge（首尾）、select_group（Ctrl+↑↓ 分组跳转）。

卡片方法只测索引移动逻辑，_ensure_rendered/_update_selection 用桩替换，
不依赖真实渲染与滚动区域尺寸。
"""
from app.widgets.bottom_input_area import SendableTextEdit
from app.widgets.cards.floating.file_mention_card import FileMentionCard
from PyQt5.QtCore import Qt


class _FakeCard:
    """只提供 root_dir / is_card_visible / dismiss 的最小桩"""

    def __init__(self, root_dir: str):
        self.root_dir = root_dir
        self.is_card_visible = False
        self.dismissed = False

    def dismiss(self):
        self.dismissed = True


def _editor_with_root(qapp, root: str):
    editor = SendableTextEdit()
    editor._file_mention_card_ref = _FakeCard(root)
    return editor


def _type(editor, text: str):
    """写入文本并把光标放到末尾（模拟用户键入）"""
    editor.setPlainText(text)
    cursor = editor.textCursor()
    cursor.movePosition(cursor.End)
    editor.setTextCursor(cursor)


# ==================== 空格确认折叠 ====================


def test_space_confirm_collapses_existing_file(qapp, tmp_path):
    target = tmp_path / "demo.py"
    target.write_text("print(1)", encoding="utf-8")
    editor = _editor_with_root(qapp, str(tmp_path))
    confirmed = []
    editor.fileMentionConfirmed.connect(confirmed.append)

    _type(editor, "@demo.py ")
    editor._on_at_trigger_check()

    assert editor.toPlainText() == "[[demo.py]] "
    assert confirmed == [str(target)]
    assert editor._at_trigger_pos == -1


def test_space_confirm_keeps_literal_when_path_missing(qapp, tmp_path):
    editor = _editor_with_root(qapp, str(tmp_path))
    confirmed = []
    editor.fileMentionConfirmed.connect(confirmed.append)

    _type(editor, "@nope.py ")
    editor._on_at_trigger_check()

    assert editor.toPlainText() == "@nope.py "
    assert confirmed == []


def test_space_confirm_handles_directory_with_trailing_slash(qapp, tmp_path):
    (tmp_path / "pkg").mkdir()
    editor = _editor_with_root(qapp, str(tmp_path))
    confirmed = []
    editor.fileMentionConfirmed.connect(confirmed.append)

    _type(editor, "@pkg/ ")
    editor._on_at_trigger_check()

    assert editor.toPlainText() == "[[pkg]] "
    assert confirmed == [str(tmp_path / "pkg")]


def test_space_confirm_preserves_all_trailing_spaces(qapp, tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    editor = _editor_with_root(qapp, str(tmp_path))
    editor.fileMentionConfirmed.connect(lambda _p: None)

    _type(editor, "@a.txt   ")
    editor._on_at_trigger_check()

    assert editor.toPlainText() == "[[a.txt]]   "


def test_space_confirm_supports_filename_with_space(qapp, tmp_path):
    """文件名自身含空格也能折叠：判定依据是「@ 后到光标、去尾随空白」整段
    是否为真实路径，内部空格不参与终止判定"""
    target = tmp_path / "my file.txt"
    target.write_text("x", encoding="utf-8")
    editor = _editor_with_root(qapp, str(tmp_path))
    confirmed = []
    editor.fileMentionConfirmed.connect(confirmed.append)

    _type(editor, "@my file.txt ")
    editor._on_at_trigger_check()

    assert editor.toPlainText() == "[[my file.txt]] "
    assert confirmed == [str(target)]


def test_space_confirm_keeps_natural_language_literal(qapp, tmp_path):
    """@ 后面是自然语言（不指向任何路径）时不得折叠"""
    editor = _editor_with_root(qapp, str(tmp_path))
    confirmed = []
    editor.fileMentionConfirmed.connect(confirmed.append)

    _type(editor, "@请你分析 这段代码 ")
    editor._on_at_trigger_check()

    assert editor.toPlainText() == "@请你分析 这段代码 "
    assert confirmed == []


def test_space_confirm_caret_stays_after_trailing_space(qapp, tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    editor = _editor_with_root(qapp, str(tmp_path))
    editor.fileMentionConfirmed.connect(lambda _p: None)

    _type(editor, "@a.txt ")
    editor._on_at_trigger_check()

    # 折叠后光标仍应在空格之后（可直接继续输入正文）
    assert editor.textCursor().position() == len(editor.document().toPlainText())


# ==================== 卡片键盘导航 ====================

_SECTION = {"type": "section_title"}


def _item(kind: str, name: str) -> dict:
    return {"type": kind, "name": name, "path": f"{kind}://{name}"}


def _make_card(qapp, index: int = 0) -> FileMentionCard:
    card = FileMentionCard()
    card._filtered_items = [
        dict(_SECTION, name="助手", path="section://助手"),
        _item("mention", "A"),
        dict(_SECTION, name="文件", path="section://文件"),
        _item("file", "a.py"),
        _item("file", "b.py"),
        dict(_SECTION, name="会话", path="section://会话"),
        _item("session", "S"),
    ]
    card._selected_index = index
    card._ensure_rendered = lambda idx: True  # 桩：跳过懒加载渲染
    card._update_selection = lambda *a, **k: None  # 桩：不碰真实 widget
    return card


def test_select_group_moves_to_next_section_first_item(qapp):
    card = _make_card(qapp, index=1)
    assert card.select_group(1) is True
    assert card._selected_index == 3


def test_select_group_moves_into_last_section(qapp):
    card = _make_card(qapp, index=3)
    assert card.select_group(1) is True
    assert card._selected_index == 6


def test_select_group_up_returns_to_current_section_head(qapp):
    card = _make_card(qapp, index=4)
    assert card.select_group(-1) is True
    assert card._selected_index == 3


def test_select_group_up_from_head_goes_previous_section(qapp):
    card = _make_card(qapp, index=3)
    assert card.select_group(-1) is True
    assert card._selected_index == 1


def test_select_group_returns_false_at_boundary(qapp):
    card = _make_card(qapp, index=6)
    assert card.select_group(1) is False


def test_select_edge_skips_section_titles(qapp):
    card = _make_card(qapp, index=4)
    assert card.select_edge(first=True) is True
    assert card._selected_index == 1
    assert card.select_edge(first=False) is True
    assert card._selected_index == 6


def test_select_page_moves_and_never_lands_on_section(qapp):
    card = _make_card(qapp, index=1)
    card.select_page(1)
    assert not card._is_section_index(card._selected_index)
    assert card._selected_index >= 1
    before = card._selected_index
    card.select_page(-1)
    assert not card._is_section_index(card._selected_index)
    assert card._selected_index <= before


def test_go_up_rejects_root_and_search_mode(qapp):
    card = _make_card(qapp)
    assert card.go_up() is False  # 根层
    card._browse_rel = "a/b"
    card._current_query = "x"
    assert card.go_up() is False  # 搜索态


def test_go_up_returns_parent_in_browse_mode(qapp):
    card = _make_card(qapp)
    card._browse_rel = "a/b"
    entered = []
    card._enter_dir = lambda rel: entered.append(rel)
    assert card.go_up() is True
    assert entered == ["a"]


# ==================== 输入框键位分发 ====================


class _KeyCard:
    """记录键位调用，模拟可见的文件卡片"""

    is_card_visible = True

    def __init__(self, go_up_result: bool = True):
        self.calls: list[str] = []
        self._go_up_result = go_up_result

    def select_next(self):
        self.calls.append("next")
        return True

    def select_prev(self):
        self.calls.append("prev")
        return True

    def select_group(self, delta):
        self.calls.append(f"group:{delta}")
        return True

    def select_page(self, delta):
        self.calls.append(f"page:{delta}")
        return True

    def select_edge(self, first):
        self.calls.append(f"edge:{first}")
        return True

    def go_up(self):
        self.calls.append("up")
        return self._go_up_result

    def enter_selected_dir(self):
        self.calls.append("enter_dir")
        return False

    def select_current(self):
        self.calls.append("current")

    def dismiss(self):
        self.calls.append("dismiss")


def _press(editor, key, mods=Qt.NoModifier):
    from PyQt5.QtCore import QEvent
    from PyQt5.QtGui import QKeyEvent

    editor.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, mods))


def test_key_dispatch_page_and_edges(qapp):
    from PyQt5.QtCore import Qt

    editor = SendableTextEdit()
    card = _KeyCard()
    editor._file_mention_card_ref = card
    _press(editor, Qt.Key_PageDown)
    _press(editor, Qt.Key_PageUp)
    _press(editor, Qt.Key_Home)
    _press(editor, Qt.Key_End)
    assert card.calls == ["page:1", "page:-1", "edge:True", "edge:False"]


def test_key_dispatch_ctrl_arrows_go_to_groups(qapp):
    from PyQt5.QtCore import Qt

    editor = SendableTextEdit()
    card = _KeyCard()
    editor._file_mention_card_ref = card
    _press(editor, Qt.Key_Down, Qt.ControlModifier)
    _press(editor, Qt.Key_Up, Qt.ControlModifier)
    assert card.calls == ["group:1", "group:-1"]


def test_key_dispatch_home_with_ctrl_stays_in_editor(qapp):
    """Ctrl+Home 不抢，仍交给输入框（卡片无调用即通过；光标行为归 Qt）"""
    editor = SendableTextEdit()
    card = _KeyCard()
    editor._file_mention_card_ref = card
    _type(editor, "abc")
    card.calls.clear()  # 写入文本触发的 @ 检测会 dismiss（卡片恒可见的桩），只关心按键本身
    _press(editor, Qt.Key_Home, Qt.ControlModifier)
    assert card.calls == []


def test_key_dispatch_esc_goes_up_in_browse_mode(qapp):
    from PyQt5.QtCore import Qt

    editor = SendableTextEdit()
    card = _KeyCard(go_up_result=True)
    editor._file_mention_card_ref = card
    _press(editor, Qt.Key_Escape)
    assert card.calls == ["up"]


def test_key_dispatch_esc_dismisses_at_root(qapp):
    from PyQt5.QtCore import Qt

    editor = SendableTextEdit()
    card = _KeyCard(go_up_result=False)
    editor._file_mention_card_ref = card
    _press(editor, Qt.Key_Escape)
    assert card.calls == ["up", "dismiss"]


# ==================== 文件胶囊解封（Backspace 回退到卡片态） ====================


def _make_capsule(editor, path: str, prefix: str = "前文 "):
    """在文本尾部插入一枚文件胶囊，光标停在胶囊之后"""
    from app.widgets.bottom_input_area import _OBJECT_REPLACEMENT

    editor.setPlainText(prefix)
    cursor = editor.textCursor()
    cursor.movePosition(cursor.End)
    cursor.insertText(_OBJECT_REPLACEMENT, editor._make_mention_format(path))
    editor.setTextCursor(cursor)


def test_backspace_unfolds_file_capsule_to_at_path(qapp, tmp_path):
    target = tmp_path / "demo.py"
    target.write_text("x", encoding="utf-8")
    editor = SendableTextEdit()
    editor._file_mention_card_ref = _FakeCard(str(tmp_path))
    _make_capsule(editor, str(target))

    _press(editor, Qt.Key_Backspace)

    assert editor.toPlainText() == "前文 @demo.py"
    # @ 前是空格 → 不需要补边界空格
    assert editor.toPlainText().count("  ") == 0


def test_backspace_unfold_inserts_boundary_space_after_cjk(qapp, tmp_path):
    """@ 前是中文时补一个空格，否则 @ 检测的单词边界不成立、卡片不重开"""
    target = tmp_path / "demo.py"
    target.write_text("x", encoding="utf-8")
    editor = SendableTextEdit()
    editor._file_mention_card_ref = _FakeCard(str(tmp_path))
    _make_capsule(editor, str(target), prefix="看看")

    _press(editor, Qt.Key_Backspace)

    assert editor.toPlainText() == "看看 @demo.py"


def test_backspace_unfold_reopens_file_card(qapp, tmp_path):
    target = tmp_path / "demo.py"
    target.write_text("x", encoding="utf-8")
    editor = SendableTextEdit()
    editor._file_mention_card_ref = _FakeCard(str(tmp_path))
    _make_capsule(editor, str(target))
    triggered = []
    editor.atTriggered.connect(triggered.append)

    _press(editor, Qt.Key_Backspace)

    assert triggered and triggered[0] == "demo.py"


def test_backspace_unfold_drops_attachment(qapp, tmp_path):
    """解封 = 回到未确认态，正文引用消失 → 反向同步上报移除"""
    target = tmp_path / "demo.py"
    target.write_text("x", encoding="utf-8")
    editor = SendableTextEdit()
    editor._file_mention_card_ref = _FakeCard(str(tmp_path))
    _make_capsule(editor, str(target))
    removed = []
    editor.attachmentsRemoved.connect(removed.extend)

    _press(editor, Qt.Key_Backspace)

    assert removed == ["demo.py"]


def test_delete_before_capsule_unfolds_and_keeps_caret(qapp, tmp_path):
    target = tmp_path / "demo.py"
    target.write_text("x", encoding="utf-8")
    editor = SendableTextEdit()
    editor._file_mention_card_ref = _FakeCard(str(tmp_path))
    _make_capsule(editor, str(target))
    cursor = editor.textCursor()
    cursor.movePosition(cursor.Left)  # 光标移到胶囊之前
    editor.setTextCursor(cursor)
    caret = cursor.position()

    _press(editor, Qt.Key_Delete)

    assert editor.toPlainText() == "前文 @demo.py"
    assert editor.textCursor().position() == caret


def test_unfold_skipped_when_selection_exists(qapp, tmp_path):
    target = tmp_path / "demo.py"
    target.write_text("x", encoding="utf-8")
    editor = SendableTextEdit()
    editor._file_mention_card_ref = _FakeCard(str(tmp_path))
    _make_capsule(editor, str(target))
    cursor = editor.textCursor()
    cursor.movePosition(cursor.Left, cursor.KeepAnchor)
    editor.setTextCursor(cursor)

    _press(editor, Qt.Key_Backspace)

    # 交给默认删除：胶囊被删掉，不留 @路径 文本
    assert "@demo.py" not in editor.toPlainText()


def test_unfold_uses_relative_path_against_root(qapp, tmp_path):
    sub = tmp_path / "pkg"
    sub.mkdir()
    target = sub / "mod.py"
    target.write_text("x", encoding="utf-8")
    editor = SendableTextEdit()
    editor._file_mention_card_ref = _FakeCard(str(tmp_path))
    _make_capsule(editor, str(target))

    _press(editor, Qt.Key_Backspace)

    assert editor.toPlainText() == "前文 @pkg/mod.py"


def test_second_backspace_deletes_char_instead_of_unfolding_again(qapp, tmp_path):
    """解封是一次性的：第二次 Backspace 逐字删，不重复解封"""
    target = tmp_path / "demo.py"
    target.write_text("x", encoding="utf-8")
    editor = SendableTextEdit()
    editor._file_mention_card_ref = _FakeCard(str(tmp_path))
    _make_capsule(editor, str(target))

    _press(editor, Qt.Key_Backspace)
    assert editor.toPlainText() == "前文 @demo.py"
    _press(editor, Qt.Key_Backspace)
    assert editor.toPlainText() == "前文 @demo.p"

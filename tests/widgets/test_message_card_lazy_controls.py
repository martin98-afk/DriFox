"""消息卡片在首次差异、重试及切换可见状态时的行为。"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QLabel

from app.widgets.message_card import MessageCard, _stream_boundary_tail


def test_file_diff_controls_created_once(qtbot):
    card = MessageCard("assistant")
    qtbot.addWidget(card)
    card.set_diff_stats()
    assert card._footer_diff_stats_label is None

    card.add_diff_stats(additions=3, seen_files={"a"})
    card.add_diff_stats(deletions=2, seen_files={"a", "b"})
    parts = tuple(card._footer_diff_parts)
    assert [p.text() for p in parts] == ["2 文件 ", "+3", "/", "-2"]
    assert not card._footer_review_icon.isHidden()

    card.set_diff_stats()
    assert all(p.isHidden() for p in parts)
    assert card._footer_review_icon.isHidden()
    card.set_diff_stats(files_count=1, deletions=7)
    assert tuple(card._footer_diff_parts) == parts
    assert [p.text() for p in parts] == ["1 文件 ", "", "", "-7"]


def test_retry_and_error_share_one_status_bar(qtbot):
    card = MessageCard("assistant")
    qtbot.addWidget(card)
    card.set_error_state(False)
    assert card._retry_status_widget is None
    card.set_error_state(True, "连接失败")
    bar = card._retry_status_widget
    assert not bar.isHidden()
    assert card._retry_attempt_label.text() == "连接失败"
    card.start_retry_anim("超时", 1, 3, 2)
    assert card._retry_status_widget is bar
    assert card._retry_attempt_label.text() == "第 1/3 次重试"
    card.stop_streaming_anim()
    assert bar.isHidden()


def test_stream_animation_tracks_visibility(qtbot):
    card = MessageCard("assistant")
    qtbot.addWidget(card)
    card.start_streaming_anim()
    assert not card._anim_timer.isActive()
    card.show()
    assert not card._anim_timer.isActive()
    card.start_retry_anim("超时", 1, 3, 2)
    assert card._anim_timer.isActive()
    card.hide()
    assert not card._anim_timer.isActive()
    card.show()
    assert card._anim_timer.isActive()
    card.hide()
    card.stop_streaming_anim()
    card.show()
    assert not card._anim_timer.isActive()


def test_boundary_tail_spans_committed_text_and_fragments():
    for parts in ([], [""] * 3, ["短"] * 180, ["字" * 300], ["\n", "", "\n"]):
        text = "前文" * 90
        assert _stream_boundary_tail({"text": text, "_pend": parts}) == (text + "".join(parts))[-128:]


def test_options_and_resize_placeholder_follow_body_after_retry(qtbot):
    card = MessageCard("assistant")
    qtbot.addWidget(card)
    card.set_error_state(True, "连接失败")
    assert "resize_placeholder" not in vars(card)
    assert "options_widget" not in vars(card)

    option = {"label": "继续"}
    card.add_interactive_options([option])
    card.show()
    main = card.layout()
    assert main.indexOf(card._assistant_bubble) < main.indexOf(card.resize_placeholder)
    assert main.indexOf(card.resize_placeholder) < main.indexOf(card.options_widget)
    assert main.indexOf(card.options_widget) < main.indexOf(card._retry_status_widget)
    assert main.indexOf(card._retry_status_widget) < main.indexOf(card._footer_bar)
    label = next(label for label in card.options_widget.findChildren(QLabel) if label.text() == "• 继续")
    with qtbot.waitSignal(card.optionSelected) as clicked:
        qtbot.mouseClick(label, Qt.LeftButton)
    assert clicked.args == [option]

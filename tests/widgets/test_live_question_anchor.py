from unittest.mock import Mock

import pytest
from PyQt5.QtWidgets import QWidget

from app.main_widget import OpenAIChatToolWindow
from tests.widgets.test_layout_scan_coalesce import _add_card, _flush, _make_scan_window


@pytest.mark.parametrize('batch_idx', [None, 2])
def test_live_question_before_batch_data(qapp, batch_idx):
    win, layout, container = _make_scan_window()
    win._message_batch = [[{'role': 'user'}], [{'role': 'assistant'}], []]
    _add_card(layout, container, message_index=0, height=250)
    question = _add_card(layout, container, message_index=batch_idx, height=240)
    question.role = 'user'
    question._question_live_round = 1
    reply = _add_card(layout, container, message_index=None, height=350)
    reply.role = 'assistant'
    reply._question_live_round = 1
    _flush(container, win.chat_scroll_area)
    _flush(container, win.chat_scroll_area, reply.y() + 10)
    assert win._scan_chat_layout_once()['question_anchor'] == ('round', 1)
    win._start_question_card_jump = Mock()
    OpenAIChatToolWindow._jump_to_history_question(win, 1)
    win._start_question_card_jump.assert_called_once_with(question)
    win.chat_scroll_area.close()

# -*- coding: utf-8 -*-
"""主题刷新覆盖回归：宿主浮动卡不得漏刷（QueueMessageCard）

背景
----
main_widget 的主题刷新链里有两处手工维护的「宿主自有浮动卡」清单：
1. _apply_runtime_ui_settings 5a 颜色块的浮动卡元组；
2. _refresh_floating_cards_font_style（5b 字体块）的元组。

新增浮动卡（如 input_card_module 装配的 QueueMessageCard）若忘了往任一处补，
卡片样式就永远停在构造时的主题 —— 浅色切深色后仍是白底、白页眉。

本文件锁定两件事：
1. 两个函数的源码清单里都出现 _queue_message_card（静态锁，改属性名会立刻红）；
2. 5b 路径真实调用该卡的 refresh_style，且同周期恰好 1 次（不双刷）。
"""

import inspect
from unittest.mock import MagicMock

from app.main_widget import OpenAIChatToolWindow
from app.widgets.cards.floating.queue_message_card import QueueMessageCard


def test_queue_card_present_in_both_refresh_lists():
    """5a / 5b 两处浮动卡清单都必须含 _queue_message_card。

    用源码文本断言而非构造真实主窗口：窗口构造成本高且与「清单覆盖」无关，
    这里只锁清单内容。
    """
    src_5a = inspect.getsource(OpenAIChatToolWindow._apply_runtime_ui_settings)
    src_5b = inspect.getsource(OpenAIChatToolWindow._refresh_floating_cards_font_style)

    assert "_queue_message_card" in src_5a, "5a 颜色块浮动卡清单漏了 _queue_message_card"
    assert "_queue_message_card" in src_5b, "5b 字体块浮动卡清单漏了 _queue_message_card"


def test_queue_card_refresh_style_invoked_once():
    """5b 路径：_queue_message_card.refresh_style 被调且周期内只调 1 次。

    _safe_refresh 走真实实现（含周期内去重），宿主其余状态用 MagicMock 兜底。
    """
    card = MagicMock(spec=QueueMessageCard)
    win = MagicMock()
    win._queue_message_card = card
    win._refreshed_in_cycle = set()
    win._safe_refresh = lambda w, m="refresh_style": OpenAIChatToolWindow._safe_refresh(win, w, m)

    OpenAIChatToolWindow._refresh_floating_cards_font_style(win)

    assert card.refresh_style.call_count == 1, (
        f"主题刷新应刷 QueueMessageCard 恰好 1 次，实际 {card.refresh_style.call_count}"
    )

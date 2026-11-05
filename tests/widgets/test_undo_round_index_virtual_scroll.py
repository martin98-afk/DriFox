"""回归测试：虚拟滚动回收批次后，「撤销到这里」不得错定位轮次

症状（2026-11-05 真机，会话 c9b7829662864d5182cb3116c798b1c8）：
用户撤销 23:17 的一条 user 消息（真实 round_index=22），UI 只删掉少量卡片，
但 session 被截掉 172 条消息（含 40 分钟的团队汇报与产出），且无二次确认。

根因：
1. `_find_user_round_index_for_card` 首选遍历 `_batch_cards` 找卡片所在 batch；
2. 卡片所属批次被虚拟滚动回收后 `_batch_cards[idx] = None`（只留等高占位），
   首路径找不到 → 降级遍历 `chat_layout` 数 user 卡；
3. 但布局里只剩视口附近几张卡，被回收的批次只剩占位 QWidget，
   数出来 round_index=5（真实 22），`_truncate_session_from_user_round`
   从第 5 轮起截断，连带删除其后全部 172 条。

修复：
- `_find_user_round_index_for_card` 删掉布局遍历降级，改为
  「card._message_index → _batch_cards 反查」，均失败返回 None
- `_undo_from_message` 以 `_message_index` 反算值为权威，与 `card._round_index`
  不一致时告警；大额撤销（>20 条）弹确认框

运行: uv run pytest tests/widgets/test_undo_round_index_virtual_scroll.py -v
"""

import inspect
import sys
from pathlib import Path
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import app.main_widget as mw


class _FakeCard:
    """MessageCard 桩：只需 role / _message_index / _round_index / text"""

    def __init__(self, role="user", message_index=None, round_index=None, text=""):
        self.role = role
        self._message_index = message_index
        self._round_index = round_index
        self.timestamp = ""
        self._text = text

    def get_plain_text(self):
        return self._text


def _make_widget(message_batch, batch_cards):
    """构造只带定位所需字段的窗口桩（绕过 __init__）"""
    widget = mw.OpenAIChatToolWindow.__new__(mw.OpenAIChatToolWindow)
    widget._message_batch = message_batch
    widget._batch_cards = batch_cards
    widget.chat_layout = MagicMock()
    widget.chat_layout.count = MagicMock(return_value=0)
    widget._build_user_prefix_cache()
    return widget


def _user_batch(text):
    return [{"role": "user", "content": text}]


def _assistant_batch(text):
    return [{"role": "assistant", "content": text}]


def test_locate_round_index_when_batch_recycled():
    """批次被虚拟滚动回收（_batch_cards[idx]=None）后仍能正确定位 round_index

    场景复刻事故：27 个 batch，前面 20 个 user 轮次的卡片已被回收成占位，
    目标卡片（第 22 轮）仍在 _batch_cards 中。
    """
    message_batch = []
    batch_cards = []
    for i in range(20):
        message_batch.append(_user_batch(f"q{i}"))
        batch_cards.append(None)  # 已回收：数据在，卡片没了
        message_batch.append(_assistant_batch(f"a{i}"))
        batch_cards.append(None)

    # 目标：第 21 个 user 轮（0-based round_index=20）
    target = _FakeCard(message_index=40, round_index=20)
    message_batch.append(_user_batch("target"))
    batch_cards.append([target])

    widget = _make_widget(message_batch, batch_cards)
    got = widget._find_user_round_index_for_card(target)

    assert got == 20, f"应定位到 round_index=20，实际 {got}（布局遍历降级会数成 0）"


def test_locate_prefers_message_index_over_stale_round_index():
    """card._round_index 偏小时，以 _message_index 反算值为准"""
    message_batch = []
    for i in range(20):
        message_batch.append(_user_batch(f"q{i}"))
        message_batch.append(_assistant_batch(f"a{i}"))
    message_batch.append(_user_batch("target"))

    # 事故形态：_round_index 被 _refresh_all_cards_round_index 写成 5，真实应为 20
    target = _FakeCard(message_index=40, round_index=5)
    widget = _make_widget(message_batch, [None] * len(message_batch))

    got = widget._find_user_round_index_for_card(target)
    assert got == 20, f"_message_index=40 应反算为 round_index=20，实际 {got}"


def test_locate_returns_none_when_unlocatable():
    """两路都失败必须返回 None，不得回落到布局计数（宁可失败也不要错删）"""
    widget = _make_widget([_user_batch("q0")], [None])
    orphan = _FakeCard(message_index=None, round_index=None, text="不存在的消息")

    got = widget._find_user_round_index_for_card(orphan)
    assert got is None, f"无法定位时必须返回 None，实际返回 {got}"


def test_no_layout_traversal_in_finder():
    """源码契约：定位函数内不得再出现 chat_layout 遍历计数

    布局遍历在虚拟滚动下必然算错（占位 widget 不计 user 卡），
    这条断言防止降级路径被无意加回来。
    """
    src = inspect.getsource(mw.OpenAIChatToolWindow._find_user_round_index_for_card)
    assert "chat_layout.count()" not in src, (
        "_find_user_round_index_for_card 不得遍历 chat_layout："
        "虚拟滚动回收后布局里只剩占位，计数必然偏小（2026-11-05 误删 172 条根因）"
    )


def test_undo_has_large_undo_confirmation():
    """源码契约：大额撤销必须二次确认"""
    src = inspect.getsource(mw.OpenAIChatToolWindow._undo_from_message)
    assert "huge_undo_threshold" in src, "_undo_from_message 应包含大额撤销阈值判断"
    assert "QMessageBox.question" in src, "大额撤销必须弹确认框"
    # 阈值判断必须早于截断调用
    threshold_pos = src.find("huge_undo_threshold")
    truncate_pos = src.find("_truncate_session_from_user_round(")
    assert threshold_pos != -1 and truncate_pos != -1
    assert threshold_pos < truncate_pos, "确认框必须位于截断之前，否则拦不住误删"


def test_undo_uses_message_index_as_authority():
    """源码契约：定位以 _message_index 反算值为权威，而非直接信任 _round_index"""
    src = inspect.getsource(mw.OpenAIChatToolWindow._undo_from_message)
    find_pos = src.find("self._find_user_round_index_for_card(card)")
    assert find_pos != -1, "应通过 _find_user_round_index_for_card 定位"
    # _round_index 只能作为回落，不得作为首选
    first_assign = src.find("round_index = card._round_index")
    assert first_assign == -1 or first_assign > find_pos, (
        "不得把 card._round_index 作为首选定位依据（批次回收后该值可能偏小）"
    )

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
from types import SimpleNamespace
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


def test_undo_has_no_confirmation_dialog():
    """源码契约：撤销不得弹二次确认（用户 2026-11-06 明确要求去掉）"""
    src = inspect.getsource(mw.OpenAIChatToolWindow._undo_from_message)
    assert "huge_undo_threshold" not in src, "撤销不应再有阈值确认逻辑"
    assert "QMessageBox" not in src, "撤销不应弹确认框，点一下就执行"


def test_fix_new_card_message_index_keeps_prefix_cache_in_sync():
    """新卡追加空 batch 后，前缀和缓存必须同步（否则撤销定位 IndexError）

    2026-11-06 真机：连续两次点撤销抛
    `IndexError: list index out of range`（_get_user_round_index_for_batch_index
    读 _user_prefix_cache 越界）。根因：_fix_new_card_message_index 在
    「没有可认领的同角色槽位」时 `self._message_batch.append([])`，只扩了
    _message_batch / _batch_cards，没重建 _user_prefix_cache。
    """
    base_user = _FakeCard(role="user", message_index=0)
    base_assistant = _FakeCard(role="assistant", message_index=1)
    message_batch = [_user_batch("q0"), _assistant_batch("a0")]
    batch_cards = [[base_user], [base_assistant]]

    # 两张新卡都追不上任何占位（尾部已占用 → 循环立即 break）
    new_cards = []
    for _ in range(2):
        card = MagicMock(spec=mw.MessageCard)
        card._message_index = None
        card._is_welcome = False
        card.role = "user"
        new_cards.append(card)

    widget = mw.OpenAIChatToolWindow.__new__(mw.OpenAIChatToolWindow)
    widget._message_batch = message_batch
    widget._batch_cards = batch_cards
    widget._batch_placeholders = {}
    widget._is_widget_alive = lambda c: True
    items = []
    for c in new_cards:
        item = MagicMock()
        item.widget = MagicMock(return_value=c)
        items.append(item)
    widget.chat_layout = MagicMock()
    widget.chat_layout.count = MagicMock(return_value=len(items))
    widget.chat_layout.itemAt = MagicMock(side_effect=lambda i: items[i])
    widget._build_user_prefix_cache()

    widget._fix_new_card_message_index()

    assert len(widget._message_batch) == 4, "两张新卡应各追加一个空 batch"
    assert len(widget._user_prefix_cache) == len(widget._message_batch) + 1, (
        "前缀和缓存长度必须始终是 len(_message_batch)+1，否则索引必然越界"
    )
    # 按新追加的尾部 batch 反算不得抛异常
    assert widget._get_user_round_index_for_batch_index(3) == 1


def test_undo_prefers_session_lookup_over_batch_index():
    """源码契约：定位以 session 文本/时间戳为准，批次下标只能做次选

    _message_index 会被 _rebuild_batch_cards_from_layout 在虚拟滚动下按布局
    顺序重编号（必然偏小），不能作为首选依据。
    """
    src = inspect.getsource(mw.OpenAIChatToolWindow._undo_from_message)
    session_pos = src.find("_find_user_round_index_from_session")
    batch_pos = src.find("self._find_user_round_index_for_card(card)")
    assert session_pos != -1, "应优先用 session 文本+时间戳定位"
    assert batch_pos != -1, "保留批次反算作为次选"
    assert session_pos < batch_pos, "session 定位必须前于批次反算"
    # card._round_index 只能作为最后回落
    first_assign = src.find("round_index = card._round_index")
    assert first_assign == -1 or first_assign > session_pos


def test_find_user_round_index_uses_timestamp_when_text_repeats():
    """重复提问：时间戳精确定位，不得短路返回第一个命中"""
    from app.widgets.ui_helpers import find_user_round_index

    session = SimpleNamespace(
        messages=[
            {"role": "user", "content": "继续", "timestamp": "2026-11-06 01:00:00"},
            {"role": "assistant", "content": "a1", "timestamp": "2026-11-06 01:00:10"},
            {"role": "user", "content": "继续", "timestamp": "2026-11-06 01:20:00"},
            {"role": "assistant", "content": "a2", "timestamp": "2026-11-06 01:20:10"},
        ]
    )
    # 卡片时间戳被 MessageCard 截断成 %m-%d %H:%M，必须能对齐到消息的分钟
    assert find_user_round_index(session, "继续", "11-06 01:20") == 1
    # 无时间戳 + 文本重复 → 无法唯一确定，拒绝猜
    assert find_user_round_index(session, "继续", "") == -1


def test_find_user_round_index_no_substring_short_circuit():
    """短句不得命中更早的长文本（旧实现 user_text in content 会返回 0）"""
    from app.widgets.ui_helpers import find_user_round_index

    session = SimpleNamespace(
        messages=[
            {"role": "user", "content": "这个继续优化一下性能", "timestamp": "2026-11-06 01:00:00"},
            {"role": "user", "content": "继续", "timestamp": "2026-11-06 01:05:00"},
        ]
    )
    assert find_user_round_index(session, "继续", "") == 1


def test_find_user_round_index_prefers_source_ts_ms():
    """同一分钟内重复提问：source_message.ts_ms 毫秒锚点精确定位"""
    from app.widgets.ui_helpers import find_user_round_index

    session = SimpleNamespace(
        messages=[
            {"role": "user", "content": "继续", "timestamp": "2026-11-06 01:20:01", "ts_ms": 1001},
            {"role": "assistant", "content": "a1", "timestamp": "2026-11-06 01:20:05"},
            {"role": "user", "content": "继续", "timestamp": "2026-11-06 01:20:40", "ts_ms": 1040},
            {"role": "assistant", "content": "a2", "timestamp": "2026-11-06 01:21:00"},
        ]
    )
    # 分钟级命中两条，必须靠 ts_ms 收敛到第 2 条
    assert find_user_round_index(session, "继续", "11-06 01:20", {"ts_ms": 1040}) == 1
    assert find_user_round_index(session, "继续", "11-06 01:20", {"ts_ms": 1001}) == 0


def test_round_diff_stats_uses_source_message_not_stale_round_index():
    """整轮差异统计定位：按消息源反查，不信任被重编号污染的 _round_index"""
    session = SimpleNamespace(
        messages=[
            {"role": "user", "content": "q0", "timestamp": "2026-11-06 01:00:00", "ts_ms": 1000},
            {"role": "assistant", "content": "a0", "timestamp": "2026-11-06 01:00:10", "ts_ms": 1010},
            {"role": "user", "content": "q1", "timestamp": "2026-11-06 01:00:20", "ts_ms": 1020},
            {"role": "assistant", "content": "a1", "timestamp": "2026-11-06 01:00:30", "ts_ms": 1030},
        ]
    )
    widget = mw.OpenAIChatToolWindow.__new__(mw.OpenAIChatToolWindow)
    card = _FakeCard(role="assistant", round_index=0)  # 偏小：被虚拟滚动重编号过
    card._source_message = {"ts_ms": 1030}
    assert widget._find_round_index_from_card_source(session, card) == 1

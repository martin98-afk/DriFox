# -*- coding: utf-8 -*-
"""提问卡片关不掉 —— 回归测试

症状（用户报障）：所有问题回答提交后，提问卡片不消失且压在输入框上方，
再点「提交」「忽略」均无反应；而正文显示 AI 已经收到回答继续往下输出。

根因：CardManager 用 visible_cards[容器] 单值记录「栈顶卡」，
而 L2 状态层的 refresh_layer() → _apply_visible_set() 会无条件覆写该单值。
提问卡注册在 BOTTOM 且不是 stackable，所以只要提问期间状态层刷过一次
（子智能体进度、auto_hide 定时关闭、排队卡、撤销卡 TTL…），
"question" 这个记账就被抢走；随后 hide_card("question") 命中
`visible_cards[ct] != card_id` 早退，widget 永不 setVisible(False)。

本文件锁死三件事：
1. 状态层刷新后提问卡必须仍能关闭
2. 提问卡占位期间状态层不得抢走栈顶记账（保住「question 覆盖一切」的优先级）
3. 状态卡自身仍按谓词正常显隐（修复不能把 L2 层弄坏）
"""

import pytest

# ── 测试夹具 ──


class _DummyCard:
    """伪卡片：只实现 CardManager 用到的接口，避免拉起 Qt 依赖链

    与 QWidget 版行为一致点：setVisible / isHidden / windowTitle（存活探测）
    / parentWidget（布局重排取容器）/ property（dock 堆叠探测）。
    """

    def __init__(self):
        self._hidden = True
        self.show_calls = 0
        self.hide_calls = 0

    def setVisible(self, visible):
        self._hidden = not visible
        if visible:
            self.show_calls += 1
        else:
            self.hide_calls += 1

    def isHidden(self):
        return self._hidden

    def windowTitle(self):
        return "dummy"

    def parentWidget(self):
        return None

    def property(self, key):
        return None


@pytest.fixture
def mgr():
    from app.widgets.cards.card_manager import CardManager

    CardManager.reset_instance()
    manager = CardManager.get_instance()
    manager.register_window("w1")
    yield manager
    CardManager.reset_instance()


def _register_question_and_status(manager, status_visible=lambda: True):
    """按主程序真实配置注册：question 普通卡 + sub_agent_compact 状态卡，同在 BOTTOM"""
    from app.widgets.cards.card_manager import ContainerType

    question = _DummyCard()
    status = _DummyCard()
    manager.register_card("w1", ContainerType.BOTTOM, "question", question)
    manager.register_card(
        "w1",
        ContainerType.BOTTOM,
        "sub_agent_compact",
        status,
        layer="status",
        stackable=True,
        order_hint=10,
        visible_when=status_visible,
    )
    return question, status


def _stack_top(manager, container_type):
    return manager._window_data["w1"]["visible_cards"][container_type]


# ── 用例 ──


def test_close_question_after_status_layer_refresh(mgr):
    """核心场景：提问期间子智能体卡刷了一次进度，用户提交后卡片必须消失"""
    question, status = _register_question_and_status(mgr)

    mgr.show_card("question", "w1")
    assert not question.isHidden(), "前置条件：提问卡应已显示"

    mgr.refresh_layer("w1", "status")  # 提问等待期间的状态层刷新
    mgr.hide_card("question", "w1")  # 用户点「提交」

    assert question.isHidden(), "提问卡被状态层抢走记账后无法隐藏（关不掉 bug）"
    assert not status.isHidden() or mgr.is_card_visible("sub_agent_compact", "w1"), "状态卡应仍按谓词可见"


def test_repeated_close_attempts_take_effect(mgr):
    """用户反复点「忽略」：任一次调用都必须真正让卡片消失，不能因记账漂移空转"""
    from app.widgets.cards.card_manager import ContainerType

    active = {"v": True}
    question, _status = _register_question_and_status(mgr, lambda: active["v"])

    mgr.show_card("question", "w1")
    mgr.refresh_layer("w1", "status")  # 状态卡上来了
    active["v"] = False
    mgr.refresh_layer("w1", "status")  # 状态卡自己退了，把单值置成 None

    for _ in range(3):
        mgr.hide_card("question", "w1")

    assert question.isHidden(), "反复点击仍关不掉提问卡"
    assert _stack_top(mgr, ContainerType.BOTTOM) != "question", "记账不应仍指向已隐藏的提问卡"


def test_question_keeps_stack_top_during_status_refresh(mgr):
    """优先级不被侵蚀：提问卡占位期间，状态层重算不得抢走栈顶记账"""
    from app.widgets.cards.card_manager import ContainerType

    _question, _status = _register_question_and_status(mgr)

    mgr.show_card("question", "w1")
    mgr.refresh_layer("w1", "status")

    assert mgr.is_card_visible("question", "w1"), "提问卡应仍被判定为可见（压制其他卡的前提）"
    assert _stack_top(mgr, ContainerType.BOTTOM) == "question", "状态层不得抢走栈顶记账"


def test_status_layer_still_works_without_question(mgr):
    """反向保护：没有提问卡时，状态层照常接管栈顶记账"""
    from app.widgets.cards.card_manager import ContainerType

    _question, status = _register_question_and_status(mgr)

    mgr.refresh_layer("w1", "status")
    assert not status.isHidden(), "谓词为真时状态卡应显示"
    assert mgr.is_card_visible("sub_agent_compact", "w1")
    assert _stack_top(mgr, ContainerType.BOTTOM) == "sub_agent_compact"


def test_close_question_still_restores_status_card(mgr):
    """提问卡关闭后状态卡不应丢失（宿主 _restore_after_question_close 依赖谓词重算）"""
    question, status = _register_question_and_status(mgr)

    mgr.show_card("question", "w1")
    mgr.hide_card("question", "w1")
    mgr.refresh_layer("w1", "status")  # _restore_after_question_close 的等价调用

    assert question.isHidden(), "提问卡应已关闭"
    assert not status.isHidden(), "状态卡应随谓词重算回到可见集"

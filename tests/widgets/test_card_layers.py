# -*- coding: utf-8 -*-
"""CardManager 分层通道：L2 状态层多卡共存 + 谓词重算恢复

对应设计稿 docs/superpowers/specs/2026-09-12-bottom-card-layers-design.md
的回归用例 1 / 2 / 6 / 7 / 8。

核心契约：
- L2（stackable 卡）豁免同容器互斥 —— 输入补全卡显示不会吞掉状态卡
- 显隐由 visible_when 谓词重算，而非调用顺序 —— 被 L3 覆盖后能自动恢复
- 层内按 order_hint 排序
"""

import pytest

from app.widgets.cards.card_manager import CardManager, ContainerType


class _FakeCard:
    """最小卡片桩：记录显隐状态，满足 CardManager 的探测契约"""

    def __init__(self):
        self.visible = False

    def show_card(self):
        self.visible = True

    def hide_card(self):
        self.visible = False

    def setVisible(self, v):
        self.visible = bool(v)

    def windowTitle(self):
        return "fake"


@pytest.fixture()
def cm():
    CardManager.reset_instance()
    yield CardManager.get_instance()
    CardManager.reset_instance()


def _setup(cm):
    """注册 L1 输入补全卡 + L2 状态层三卡 + L3 系统卡（question）"""
    state = {"sub_agent": True, "queue": True, "undo": True}
    cards = {k: _FakeCard() for k in ("command", "sub_agent_compact", "message_queue", "undo_delete", "question")}
    W = "w"
    cm.register_window(W)
    cm.register_card(
        W,
        ContainerType.BOTTOM,
        "command",
        cards["command"],
        suppress_others=["sub_agent", "sub_agent_compact"],
        layer="completion",
    )
    cm.register_card(
        W,
        ContainerType.BOTTOM,
        "sub_agent_compact",
        cards["sub_agent_compact"],
        layer="status",
        stackable=True,
        order_hint=10,
        visible_when=lambda: state["sub_agent"],
    )
    cm.register_card(
        W,
        ContainerType.BOTTOM,
        "message_queue",
        cards["message_queue"],
        layer="status",
        stackable=True,
        order_hint=20,
        visible_when=lambda: state["queue"],
    )
    cm.register_card(
        W,
        ContainerType.BOTTOM,
        "undo_delete",
        cards["undo_delete"],
        layer="status",
        stackable=True,
        order_hint=30,
        visible_when=lambda: state["undo"],
    )
    cm.register_card(W, ContainerType.BOTTOM, "question", cards["question"], layer="system")
    return W, state, cards


_STATUS_IDS = ("sub_agent_compact", "message_queue", "undo_delete")


def _status_visible(cm, W):
    return [cid for cid in _STATUS_IDS if cm.is_card_visible(cid, W)]


class TestStatusLayerStacking:
    def test_refresh_layer_shows_all_when_predicates_true(self, cm):
        W, _state, cards = _setup(cm)
        cm.refresh_layer(W, "status")
        assert _status_visible(cm, W) == list(_STATUS_IDS)
        assert all(cards[cid].visible for cid in _STATUS_IDS)

    def test_layer_ordered_by_order_hint(self, cm):
        """层内顺序 = order_hint，而非注册顺序"""
        W, _state, _cards = _setup(cm)
        cm.refresh_layer(W, "status")
        assert cm._window_data[W]["multi_visible"][ContainerType.BOTTOM] == list(_STATUS_IDS)

    def test_predicate_false_removes_card(self, cm):
        """用例 8：队列清空 → 队列卡自动退出可见集"""
        W, state, cards = _setup(cm)
        cm.refresh_layer(W, "status")
        state["queue"] = False
        cm.refresh_layer(W, "status")
        assert "message_queue" not in _status_visible(cm, W)
        assert cards["message_queue"].visible is False

    def test_case1_input_completion_does_not_eat_status_card(self, cm):
        """用例 1：子智能体运行中打 / 再关闭 → 子智能体卡全程可见"""
        W, _state, _cards = _setup(cm)
        cm.refresh_layer(W, "status")
        assert cm.is_card_visible("sub_agent_compact", W) is True

        cm.show_card("command", W)
        assert cm.is_card_visible("sub_agent_compact", W) is True, "输入补全卡显示不得吞掉 L2 状态卡"

        cm.hide_card("command", W)
        assert cm.is_card_visible("sub_agent_compact", W) is True

    def test_case2_l1_and_l2_coexist(self, cm):
        """用例 2：排队卡可见时打 @ → L1 与 L2 同时可见"""
        W, _state, _cards = _setup(cm)
        cm.refresh_layer(W, "status")
        cm.show_card("command", W)
        assert cm.is_card_visible("message_queue", W) is True
        assert cm.is_card_visible("command", W) is True

    def test_case6_system_card_covers_then_restores(self, cm):
        """用例 6：系统模态卡覆盖 L2，关闭后按谓词恢复（撤销条不被吞掉）"""
        W, _state, _cards = _setup(cm)
        cm.register_card(W, ContainerType.BOTTOM, "model_config", _FakeCard(), system_card=True)
        cm.refresh_layer(W, "status")

        cm.show_card("model_config", W)
        assert _status_visible(cm, W) == [], "系统模态卡应压制 L2 状态层"

        cm.hide_card("model_config", W)
        cm.refresh_layer(W, "status")
        assert _status_visible(cm, W) == list(_STATUS_IDS)

    def test_case7_question_covers_then_restores(self, cm):
        """用例 7：question 显示 → 关闭 → L2 按谓词全部恢复"""
        W, _state, _cards = _setup(cm)
        cm.refresh_layer(W, "status")

        cm.show_card("question", W)
        assert _status_visible(cm, W) == [], "question 应强制覆盖 L2"

        cm.hide_card("question", W)
        cm.refresh_layer(W, "status")
        assert _status_visible(cm, W) == list(_STATUS_IDS)

    def test_dismissed_undo_not_resurrected(self, cm):
        """撤销窗口已关闭（谓词转假）时，恢复时机不得把它复活"""
        W, state, _cards = _setup(cm)
        cm.refresh_layer(W, "status")
        cm.show_card("question", W)
        state["undo"] = False  # 用户 ✕ / TTL 到期 → store 清空
        cm.hide_card("question", W)
        cm.refresh_layer(W, "status")
        assert "undo_delete" not in _status_visible(cm, W)

    def test_hide_non_top_state_card(self, cm):
        """隐藏非栈顶状态卡：其余卡不受影响，栈顶单值同步"""
        W, _state, _cards = _setup(cm)
        cm.refresh_layer(W, "status")
        cm.hide_card("sub_agent_compact", W)
        assert cm.is_card_visible("sub_agent_compact", W) is False
        assert cm.is_card_visible("message_queue", W) is True
        assert cm.is_card_visible("undo_delete", W) is True
        assert cm._window_data[W]["visible_cards"][ContainerType.BOTTOM] == "message_queue"

    def test_show_card_on_stackable_reroutes_to_layer(self, cm):
        """直接 show_card(stackable) 也走层重算，而非旧互斥路径"""
        W, state, _cards = _setup(cm)
        state["queue"] = False
        cm.show_card("sub_agent_compact", W)
        assert cm.is_card_visible("sub_agent_compact", W) is True
        assert cm.is_card_visible("message_queue", W) is False

    def test_unregister_clears_layer_state(self, cm):
        W, _state, _cards = _setup(cm)
        cm.refresh_layer(W, "status")
        cm.unregister_card("message_queue", W)
        assert "message_queue" not in cm._window_data[W]["card_meta"]
        assert "message_queue" not in cm._window_data[W]["multi_visible"][ContainerType.BOTTOM]
        assert cm.is_card_visible("message_queue", W) is False

    def test_predicate_exception_treated_as_invisible(self, cm):
        """谓词持有已销毁对象时按不可见处理，不得让 refresh_layer 抛异常"""
        W = "w"
        cm.register_window(W)

        def _boom():
            raise RuntimeError("widget deleted")

        cm.register_card(
            W,
            ContainerType.BOTTOM,
            "boom",
            _FakeCard(),
            layer="status",
            stackable=True,
            visible_when=_boom,
        )
        cm.refresh_layer(W, "status")
        assert cm.is_card_visible("boom", W) is False

    def test_non_stackable_card_unaffected_by_layer(self, cm):
        """非 stackable 卡不参与层重算（谓词为 None 时不被动显隐）"""
        W, _state, _cards = _setup(cm)
        cm.register_card(W, ContainerType.BOTTOM, "plain", _FakeCard(), layer="status")
        cm.refresh_layer(W, "status")
        assert cm.is_card_visible("plain", W) is False


class TestCompletionLayerSeparation:
    """L1 输入补全层：拆成独立容器（ContainerType.COMPLETION）

    背景：命令卡（参数态）与排队卡同处 BOTTOM 容器时会争抢同一段高度预算，
    实测排队卡被压在命令卡参数行上（重叠 56px）。拆层后两者分属不同桶，
    互不干扰，且补全浮层始终紧贴输入框。
    """

    def test_container_type_has_single_definition(self):
        """ContainerType 只能有一个类对象

        历史上 app/widgets/cards/__init__.py 重复定义过一份只含 TOP/BOTTOM 的
        同名枚举。Enum 成员按 `is` 比较（哈希虽相同），而 CardManager 用容器类型
        做 dict 键 —— 两份枚举混用会让注册与查询落进不同的桶，且完全不报错。
        """
        import app.widgets.cards as pkg
        from app.widgets.cards import card_manager as cm_mod

        assert pkg.ContainerType is cm_mod.ContainerType
        assert pkg.ContainerType.COMPLETION is cm_mod.ContainerType.COMPLETION

    def test_completion_bucket_isolated_from_bottom(self, cm):
        """command 在 COMPLETION 桶：刷新 BOTTOM 状态层不得波及它"""
        W = "w"
        cm.register_window(W)
        cmd, queue = _FakeCard(), _FakeCard()
        cm.register_card(W, ContainerType.COMPLETION, "command", cmd, layer="completion")
        cm.register_card(
            W,
            ContainerType.BOTTOM,
            "message_queue",
            queue,
            layer="status",
            stackable=True,
            visible_when=lambda: True,
        )

        cm.refresh_layer(W, "status")
        assert cm.is_card_visible("message_queue", W) is True

        cm.show_card("command", W)
        assert cm.is_card_visible("command", W) is True
        assert cm.is_card_visible("message_queue", W) is True, "跨桶不得互斥"

        cm.hide_card("message_queue", W)
        assert cm.is_card_visible("command", W) is True, "BOTTOM 桶的显隐不得波及 COMPLETION 桶"

    def test_completion_layer_is_mutually_exclusive_inside(self, cm):
        """L1 层内仍互斥：file_mention 显示时 command 关闭"""
        W = "w"
        cm.register_window(W)
        cmd, fm = _FakeCard(), _FakeCard()
        cm.register_card(W, ContainerType.COMPLETION, "command", cmd, layer="completion")
        cm.register_card(W, ContainerType.COMPLETION, "file_mention", fm, layer="completion")

        cm.show_card("command", W)
        assert cm.is_card_visible("command", W) is True
        cm.show_card("file_mention", W)
        assert cm.is_card_visible("file_mention", W) is True
        assert cm.is_card_visible("command", W) is False

    def test_question_covers_completion_layer(self, cm):
        """question（系统模态）仍覆盖 L1：打开 question → 补全卡关闭"""
        W = "w"
        cm.register_window(W)
        cm.register_card(W, ContainerType.COMPLETION, "command", _FakeCard(), layer="completion")
        cm.register_card(W, ContainerType.BOTTOM, "question", _FakeCard(), layer="system")
        cm.show_card("command", W)
        cm.show_card("question", W)
        assert cm.is_card_visible("command", W) is False


class TestMultiCardFollowContentHeight:
    """容器高度预算必须覆盖**所有**可见卡，而非只覆盖实现 heightForWidth 的卡

    旧口径把未实现 heightForWidth 的卡（排队消息卡：内部全是定高行、无 wordWrap
    文本）整卡漏掉 → 容器按"只有命令卡"锁高 → 排队卡高度预算为负，被压进命令卡
    参数行里。这是"命令卡参数态与排队卡冲突"的直接机制。
    """

    def test_natural_h_includes_cards_without_height_for_width(self, qapp):
        from PyQt5.QtCore import QSize
        from PyQt5.QtWidgets import QWidget

        from app.widgets.cards.card_container import CardContainer

        class _HfwCard(QWidget):
            HEIGHT = 60

            def hasHeightForWidth(self):
                return True

            def heightForWidth(self, w):
                return self.HEIGHT

            def sizeHint(self):
                return QSize(200, self.HEIGHT)

        class _FixedCard(QWidget):
            """不实现 heightForWidth（等价于排队消息卡：整卡只有 sizeHint 可用）"""

            HEIGHT = 90

            def sizeHint(self):
                return QSize(200, self.HEIGHT)

        c = CardContainer(ContainerType.BOTTOM)
        c.setFixedWidth(400)
        a, b = _HfwCard(), _FixedCard()
        c.add_card("a", a)
        c.add_card("b", b)
        a.setVisible(True)
        b.setVisible(True)

        m = c._layout.contentsMargins()
        spacing = c._layout.spacing()
        expected = m.top() + _HfwCard.HEIGHT + spacing + _FixedCard.HEIGHT + m.bottom()

        assert c._follow_content_natural_h() == expected, "未实现 heightForWidth 的卡被整卡漏算了"

    def test_completion_container_is_transparent_and_own_type(self, qapp):
        from app.widgets.cards.card_container import CompletionCardContainer

        c = CompletionCardContainer()
        assert c.container_type is ContainerType.COMPLETION
        # 表面由卡片自绘，容器必须是透明承托（否则框套框）
        assert "transparent" in c.styleSheet()
        # 左右留白与 BottomCardContainer 对齐，两层卡片边缘才不会错位
        m = c._layout.contentsMargins()
        assert (m.left(), m.right()) == (8, 8)

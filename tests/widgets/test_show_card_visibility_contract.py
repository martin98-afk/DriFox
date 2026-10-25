# -*- coding: utf-8 -*-
"""回归：浮动卡 show_card 显隐契约兜底 + tab 回调死锁自愈

Bug 链条（2026-10-25 magpie 0.5.2 排查）：
1. CardContainer.add_card 统一 card_widget.setVisible(False) 藏卡
2. CardManager.show_card 对实现 show_card() 的卡只调业务方法、不代为 setVisible
3. 沿用旧弹窗语义的 show_card（只做业务激活，不 setVisible）→ 卡永远
   isHidden=True → 容器 has_visible=False 折叠 + overlayStateChanged(False)
   → stack 切回对话区。表象为「状态层 visible_cards 已置位但视觉不可见」，
   show-only tab 回调（已可见忽略）从此死锁，连点无反应。

修复：
- CardManager.show_card 三条路径调用后补 isHidden 兜底（对正确实现幂等）
- tab 回调死锁自愈：状态可见但 TOP 容器不可见 → hide_card 清假状态再走 show
"""
import sys

from PyQt5.QtWidgets import QApplication, QWidget


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


class LegacyPopupSemanticsCard(QWidget):
    """沿用旧弹窗语义的卡：show_card 只做业务激活，不管可见性（bug 形态）"""

    shown_flag = False

    def show_card(self):
        type(self).shown_flag = True  # 业务激活（旧实现只做这个）


class CorrectCard(QWidget):
    """正确实现：show_card 自管可见性（agent_trace 等的写法）"""

    shown_flag = False

    def show_card(self):
        type(self).shown_flag = True
        self.setVisible(True)


def _register_window_card(mgr, card_id, widget, window_id="w1"):
    """注册窗口数据与卡片到 TOP 容器（对齐 _show_floating_card 的注册序）"""
    from app.widgets.cards.card_manager import ContainerType

    mgr.register_card(window_id, ContainerType.TOP, card_id, widget, system_card=True)
    return mgr


def test_show_card_contract_fallback(qtbot):
    """漏实现自管可见性的卡：show_card 后宿主兜底补显（isHidden=False）"""
    from app.widgets.cards.card_manager import CardManager

    _ensure_qapp()
    mgr = CardManager.get_instance()
    LegacyPopupSemanticsCard.shown_flag = False
    card = LegacyPopupSemanticsCard()
    qtbot.addWidget(card)
    _register_window_card(mgr, "legacy:card", card)

    mgr.show_card("legacy:card", "w1")

    # 业务方法确实被调用（语义保留）
    assert LegacyPopupSemanticsCard.shown_flag is True
    # 契约兜底：不再永远 isHidden
    assert card.isHidden() is False

    mgr.hide_card("legacy:card", "w1")


def test_show_card_correct_impl_still_works(qtbot):
    """正确实现的卡：业务方法被调且可见性不被破坏（兜底幂等无害）"""
    from app.widgets.cards.card_manager import CardManager

    _ensure_qapp()
    mgr = CardManager.get_instance()
    CorrectCard.shown_flag = False
    card = CorrectCard()
    qtbot.addWidget(card)
    _register_window_card(mgr, "correct:card", card)

    mgr.show_card("correct:card", "w1")

    assert CorrectCard.shown_flag is True
    assert card.isHidden() is False

    mgr.hide_card("correct:card", "w1")


def test_tab_callback_deadlock_selfheal(qtbot):
    """死锁态（状态可见 + TOP 容器不可见）：tab 回调自愈而非忽略

    不实例化真 TopCardContainer（样式系统拖 qfluentwidgets 全家桶），用带
    add_card no-op 的 stub 容器——被测逻辑只读 _top_card_container.isVisible()
    与 CardManager 状态，不依赖容器样式。
    """
    from types import SimpleNamespace

    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
    from app.widgets.cards.card_manager import CardManager

    _ensure_qapp()
    UIPluginRegistry.get_instance().reset()
    reg = UIPluginRegistry.get_instance()
    mgr = CardManager.get_instance()

    class _Cont(QWidget):
        """stub 容器：真 QWidget（宿主以它为 parent 实例化卡片）+ add_card 探针"""

        def __init__(self):
            super().__init__()
            self.added = []

        def add_card(self, cid, w):
            self.added.append(cid)

    stub_cont = _Cont()
    qtbot.addWidget(stub_cont)
    host = SimpleNamespace(_card_manager=mgr, _window_id="w1", _top_card_container=stub_cont)
    reg._resolve_global_host = lambda: host

    class BrokenCard(QWidget):
        def show_card(self):
            pass  # bug 形态：不管可见性（宿主兜底会补显）

    reg.register_floating_card("t", "t:card", BrokenCard, "full", title="T")
    cb = reg._make_card_tab_callback("t:card")

    stub_cont.show()
    cb()  # 首次点击：懒创建 + 显示（正常路径）
    assert stub_cont.added == ["t:card"]
    assert mgr.is_card_visible("t:card", "w1") is True

    # 构造死锁态：状态可见但 TOP 容器不可见（模拟切页断链）
    stub_cont.hide()
    assert mgr.is_card_visible("t:card", "w1") is True

    # 死锁态点击：自愈（清假状态 → 重 show），而非「已可见忽略」
    cb()
    assert mgr.is_card_visible("t:card", "w1") is True

    UIPluginRegistry.get_instance().reset()


def test_dispatcher_preheat_main_thread_affinity(qtbot):
    """preheat 在主线程预建 _Dispatcher：单例亲和主线程（防后台线程首建损坏）"""
    from PyQt5.QtCore import QThread
    from PyQt5.QtWidgets import QApplication

    from app.plugins.capabilities.providers import ui_bridge

    _ensure_qapp()
    ui_bridge._DISPATCHER = None  # 强制重建以验证首建亲和
    ui_bridge.preheat_main_thread_dispatcher()
    d = ui_bridge._DISPATCHER
    assert d is not None
    assert d._host.thread() is QApplication.instance().thread()
    # 幂等：二次预热不换实例
    ui_bridge.preheat_main_thread_dispatcher()
    assert ui_bridge._DISPATCHER is d


def test_tab_callback_normal_ignore_kept(qtbot):
    """真可见时点击维持「再点不关闭」直觉（死锁自愈不改变正常语义）"""
    from types import SimpleNamespace

    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
    from app.widgets.cards.card_manager import CardManager

    _ensure_qapp()
    UIPluginRegistry.get_instance().reset()
    reg = UIPluginRegistry.get_instance()
    mgr = CardManager.get_instance()

    class _Cont(QWidget):
        def __init__(self):
            super().__init__()
            self.added = []

        def add_card(self, cid, w):
            self.added.append(cid)

    stub_cont = _Cont()
    qtbot.addWidget(stub_cont)
    stub_cont.show()
    host = SimpleNamespace(_card_manager=mgr, _window_id="w1", _top_card_container=stub_cont)
    reg._resolve_global_host = lambda: host

    class OkCard(QWidget):
        def show_card(self):
            self.setVisible(True)

    reg.register_floating_card("t", "t:ok", OkCard, "full", title="T")
    cb = reg._make_card_tab_callback("t:ok")

    cb()  # 首次：显示
    assert mgr.is_card_visible("t:ok", "w1") is True
    cb()  # 再点：真可见 → 忽略（不关闭）
    assert mgr.is_card_visible("t:ok", "w1") is True

    UIPluginRegistry.get_instance().reset()

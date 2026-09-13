# -*- coding: utf-8 -*-
"""回归测试：繁忙插话 + 立刻停止 → 幽灵用户卡片导致撤回静默失败（2026-09-13）

症状：AI 流式中发消息（走插话通道）后立刻点停止，点击用户消息卡片的「撤回」
无反应，日志报 [UNDO] Cannot determine valid round_index for card；
随后尝试删除同样失败（[DELETE] Invalid round_index）。

根因：
1. 繁忙发送走 _interject_entry：消息 put 进 hook 队列（_interject=True）+
   UI 立即出用户卡片，但消息不写 session（worker 消费时才落库）
2. 立刻停止 → worker 取消路径 _cancel_with_stop_hook 排空 hook 队列，
   插话消息被 stash 回收（take_recovered_interjects 回填输入框）
3. finalize 用 worker 基线+partial 全量覆写 session（插话从未进基线）
4. UI 卡片无人删除 → session 无对应消息的幽灵卡片
5. 撤回四层 round_index 定位全败（层 1-3 越界、层 4 文本匹配不到）→ 静默 warning

修复：
- M1: _interject_entry 出卡时打 _interject_pending 标记
- M2: _on_finalize_complete 回收回填时按标记删除幽灵卡片
- M3: _undo_from_message 四层全败时兜底删除幽灵卡片（仅删卡片不截断 session）

运行: uv run pytest tests/widgets/test_interject_ghost_card.py -v
"""

import inspect
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import app.main_widget as mw


# ============ 源码契约测试（无 Qt 依赖） ============


def test_m1_interject_entry_marks_pending_card():
    """M1：_interject_entry 出卡时必须打 _interject_pending 标记"""
    src = inspect.getsource(mw.OpenAIChatToolWindow._interject_entry)
    assert "card._interject_pending = True" in src, (
        "_interject_entry 创建用户卡片时必须打 _interject_pending 标记，供幽灵卡片清理与撤回兜底定位"
    )


def test_m2_finalize_recovery_removes_ghost_card():
    """M2：_on_finalize_complete 回收回填插话时必须清理幽灵卡片"""
    src = inspect.getsource(mw.OpenAIChatToolWindow._on_finalize_complete)
    assert "_remove_interject_ghost_card" in src, "回收插话回填输入框时必须同步删除幽灵用户卡片（session 无对应消息）"


def test_m3_undo_fallback_before_warning():
    """M3：_undo_from_message 兜底清理必须先于告警返回"""
    src = inspect.getsource(mw.OpenAIChatToolWindow._undo_from_message)
    fallback_pos = src.find("self._remove_interject_ghost_card(")
    warning_pos = src.find("[UNDO] Cannot determine valid round_index for card")
    assert fallback_pos != -1, "撤回失败分支应先尝试幽灵卡片兜底清理"
    assert warning_pos != -1, "非幽灵卡片场景仍需保留告警"
    assert fallback_pos < warning_pos, "兜底清理必须先于告警返回"


# ============ 行为测试（Fake layout，无 Qt 依赖） ============


class _FakeCard:
    """最小卡片：满足 _remove_interject_ghost_card / delete_widgets_from_layout 所需接口"""

    def __init__(self, role="user", text="", pending=False, welcome=False):
        self.role = role
        self._text = text
        self._interject_pending = pending
        self._is_welcome = welcome
        self.cleaned = False
        self.delete_later_called = False

    def get_plain_text(self):
        return self._text

    def cleanup(self):
        self.cleaned = True

    def hide(self):
        pass

    def setParent(self, p):
        pass

    def deleteLater(self):
        self.delete_later_called = True


class _FakeLayoutItem:
    def __init__(self, w):
        self._w = w

    def widget(self):
        return self._w


class _FakeLayout:
    def __init__(self, widgets):
        self._widgets = list(widgets)

    def count(self):
        return len(self._widgets)

    def itemAt(self, i):
        if 0 <= i < len(self._widgets):
            return _FakeLayoutItem(self._widgets[i])
        return None

    def removeWidget(self, w):
        self._widgets.remove(w)


def _make_widget(layout_widgets, monkeypatch):
    """构造最小 widget：__new__ 绕过 Qt 初始化，注入幽灵卡片清理所需依赖"""
    monkeypatch.setattr(mw, "MessageCard", _FakeCard)
    w = mw.OpenAIChatToolWindow.__new__(mw.OpenAIChatToolWindow)
    w.chat_layout = _FakeLayout(layout_widgets)
    w._rebuild_batch_cards_from_layout = lambda: None
    w._refresh_all_cards_round_index = lambda: None
    return w


def test_ghost_card_removed_by_marker_and_text(monkeypatch):
    """按标记 + 文本倒序定位幽灵卡片并删除（含其后到下一 user 卡的 widgets）"""
    normal_user = _FakeCard(role="user", text="真实消息一")
    assistant = _FakeCard(role="assistant", text="回复")
    ghost = _FakeCard(role="user", text="插话消息", pending=True)
    trailing = _FakeCard(role="assistant", text="插话的回复")
    w = _make_widget([normal_user, assistant, ghost, trailing], monkeypatch)

    assert w._remove_interject_ghost_card("插话消息") is True
    remaining = w.chat_layout._widgets
    assert ghost not in remaining, "幽灵卡片应被删除"
    assert trailing not in remaining, "幽灵卡片之后到下一 user 卡的 widgets 应一并删除"
    assert normal_user in remaining and assistant in remaining, "此前的真实卡片不受影响"


def test_ghost_card_same_text_takes_latest(monkeypatch):
    """同文本多次插话：倒序取最近一张（session 中可能已有同名旧消息卡片）"""
    older = _FakeCard(role="user", text="重复文本", pending=True)
    normal = _FakeCard(role="user", text="真实消息")
    newer = _FakeCard(role="user", text="重复文本", pending=True)
    w = _make_widget([older, normal, newer], monkeypatch)

    assert w._remove_interject_ghost_card("重复文本") is True
    remaining = w.chat_layout._widgets
    assert newer not in remaining, "应删除最近一张幽灵卡片"
    assert older in remaining, "更早的同文本卡片不受影响"


def test_unmarked_card_is_not_removed(monkeypatch):
    """无 _interject_pending 标记的普通卡片不得误删"""
    normal = _FakeCard(role="user", text="普通用户消息")
    w = _make_widget([normal], monkeypatch)

    assert w._remove_interject_ghost_card("普通用户消息") is False
    assert normal in w.chat_layout._widgets


def test_no_match_returns_false(monkeypatch):
    """文本不匹配时返回 False 且布局不动"""
    ghost = _FakeCard(role="user", text="插话消息", pending=True)
    w = _make_widget([ghost], monkeypatch)

    assert w._remove_interject_ghost_card("不存在的文本") is False
    assert ghost in w.chat_layout._widgets

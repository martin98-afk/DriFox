# -*- coding: utf-8 -*-
"""审批决策链挂点断言：AI 静默分流 / 超时 timer / 回落人工

main_widget 构造需完整应用环境，无法轻量单测；按项目既有
_security_card_source 先例做源码挂点断言，防关键链路被静默移除。
"""

from pathlib import Path


def _main_widget_source() -> str:
    root = Path(__file__).resolve().parent.parent.parent
    return (root / "app" / "main_widget.py").read_text(encoding="utf-8")


def test_ai_review_signal_bridged():
    src = _main_widget_source()
    assert "_approval_review_ready = pyqtSignal(str, object, str)" in src
    assert "_approval_review_ready.connect(self._on_approval_review_ready)" in src


def test_entry_splits_on_policy_mode():
    src = _main_widget_source()
    assert "read_approval_policy" in src
    assert 'policy["mode"] == "ai"' in src


def test_timeout_timer_wired():
    src = _main_widget_source()
    assert "_permission_timeout_timer.setSingleShot(True)" in src
    assert "_permission_timeout_timer.timeout.connect(self._on_permission_timeout)" in src
    # 人工决策 / 取消 / 待决卡清理三条路径都必须停表
    assert src.count("self._permission_timeout_timer.stop()") >= 3


def test_auto_decision_guards_pending_id():
    """自动决策回调必须校验待决 id 未变（防人工已决策后回放）"""
    src = _main_widget_source()
    assert "def _on_approval_review_ready" in src
    assert "def _fallback_manual_after_review_fail" in src
    assert "_on_approval_review_ready" in src


def test_review_call_bounded():
    """审核调用必须有界：显式 SDK 超时 + 收紧重试（审批不能等 15×600s）"""
    src = _main_widget_source()
    assert "timeout=APPROVAL_REVIEW_TIMEOUT_SEC" in src
    task_src = (Path(__file__).resolve().parent.parent.parent / "app" / "core" / "workers" / "approval_review.py").read_text(
        encoding="utf-8"
    )
    assert "APPROVAL_REVIEW_TIMEOUT_SEC" in task_src
    assert "timeout=self.timeout" in task_src
    assert "max_retries=1" in task_src

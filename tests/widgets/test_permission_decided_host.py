# -*- coding: utf-8 -*-
"""审批卡宿主接线：结构化决策落点 + 来源分类 + 风险分级 + 僵尸卡修复

不构造完整主窗口（重依赖 + WebEngine 环境崩溃），用 `__new__` + 最小桩
绑定被测方法，聚焦宿主逻辑本身：
- `_on_permission_decided` 的 id 自持与投递反馈（要求 1/2）
- `_classify_permission_source` / `_grade_permission_risk` 判定
- `_dismiss_pending_overlay_cards` 幂等性与三路径接入（缺陷 3）
- 异常路径恢复输入区（缺陷 1）
"""

import sys
from pathlib import Path

import pytest
from PyQt5.QtWidgets import QApplication

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(scope="session")
def _app_keepalive():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(autouse=True)
def _qapp(_app_keepalive):
    yield


# ── 最小宿主桩 ──


class _FakeBackend:
    """记录决策调用；decide 返回值可控（模拟 engine 就绪/未就绪）"""

    def __init__(self, delivered=True, raise_exc=False):
        self.calls = []
        self._delivered = delivered
        self._raise = raise_exc
        self.chat_engine = object() if delivered else None

    def decide_tool_permission(self, tool_call_id, decision, remember="", reason=""):
        if self._raise:
            raise RuntimeError("boom")
        self.calls.append((tool_call_id, decision, remember, reason))
        return self._delivered

    def deny_tool_permission(self, tool_call_id, reason=""):
        self.calls.append((tool_call_id, "deny", "", reason))
        return self._delivered


class _FakeCardManager:
    def __init__(self):
        self.hidden = []

    def hide_card(self, card_id, window_id):
        self.hidden.append(card_id)


class _FakeWidget:
    """假浮动卡：记录 clear 调用与显隐状态"""

    def __init__(self):
        self.cleared = 0
        self._hidden = False
        self.shown = False

    def clear(self):
        self.cleared += 1
        self._hidden = True

    def isVisible(self):
        return not self._hidden

    def setVisible(self, v):
        self._hidden = not v

    def show(self):
        self.shown = True
        self._hidden = False


def _make_host(*, delivered=True, raise_exc=False):
    """构造只带被测方法所需属性的宿主（不跑 __init__）"""
    from app.main_widget import OpenAIChatToolWindow

    host = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    host._is_destroyed = False
    host._window_id = "w1"
    host.backend = _FakeBackend(delivered=delivered, raise_exc=raise_exc)
    host._card_manager = _FakeCardManager()
    host._pending_permission_id = None
    host._permission_floating_widget = None
    host._question_floating_widget = None
    host._question_tool_call_id = None
    # 记录 UI 收尾动作
    host.input_visible_log = []
    host._set_bottom_input_visible = lambda v: host.input_visible_log.append(v)
    host.ai_state_log = []
    host._set_ai_state = lambda s: host.ai_state_log.append(s)
    host._pet_set_state = lambda s: None
    host._restore_after_question_close = lambda: None
    host._focus_input_if_active = lambda *a, **k: None
    host.notify_log = []
    host._notify_if_inactive = lambda t, m: host.notify_log.append((t, m))
    host.infobar_log = []
    # InfoBar 静态方法在测试环境无窗口，替换为记录
    return host


def _patch_infobar(monkeypatch, host):
    import app.main_widget as mw

    class _FakeInfoBar:
        @staticmethod
        def warning(title, content, **kwargs):
            host.infobar_log.append((title, content))

    monkeypatch.setattr(mw, "InfoBar", _FakeInfoBar)


# ── 一、决策回传（要求 1：id 自持）──


def test_decided_allow_calls_backend(monkeypatch):
    """allow 携带全部字段回传，并消费掉待决 id"""
    host = _make_host()
    _patch_infobar(monkeypatch, host)
    host._pending_permission_id = "call_1"
    host._on_permission_decided("allow", "", "")
    assert host.backend.calls == [("call_1", "allow", "", "")]
    assert host._pending_permission_id is None, "id 必须读完即清（防重放）"


def test_decided_allow_round_passes_auto_allow(monkeypatch):
    """remember=round 必须出现在第 3 位（round↔session 串位不报错，必须锁位置）"""
    host = _make_host()
    _patch_infobar(monkeypatch, host)
    host._pending_permission_id = "call_2"
    host._on_permission_decided("allow", "round", "")
    assert host.backend.calls == [("call_2", "allow", "round", "")]


def test_decided_allow_session_passes_session_allow(monkeypatch):
    host = _make_host()
    _patch_infobar(monkeypatch, host)
    host._pending_permission_id = "call_3"
    host._on_permission_decided("allow", "session", "")
    assert host.backend.calls == [("call_3", "allow", "session", "")]


def test_decided_deny_passes_reason(monkeypatch):
    host = _make_host()
    _patch_infobar(monkeypatch, host)
    host._pending_permission_id = "call_4"
    host._on_permission_decided("deny", "", "别删这个目录")
    assert host.backend.calls == [("call_4", "deny", "", "别删这个目录")]


def test_decided_without_id_is_not_allow(qapp, monkeypatch):
    """无待决 id：不得回传 allow（安全默认），且不得抛异常"""
    host = _make_host()
    _patch_infobar(monkeypatch, host)
    host._on_permission_decided("allow", "", "")
    assert host.backend.calls == [], "无 id 时不得投递允许"
    assert host.input_visible_log == [True], "仍应收尾恢复输入区"


def test_decided_destroyed_window_noop(monkeypatch):
    """要求 4：窗口已销毁时新槽直接返回"""
    host = _make_host()
    _patch_infobar(monkeypatch, host)
    host._is_destroyed = True
    host._pending_permission_id = "call_5"
    host._on_permission_decided("allow", "", "")
    assert host.backend.calls == []


# ── 二、投递反馈（要求 2 / S6）──


def test_undelivered_decision_shows_infobar(monkeypatch):
    """engine 未就绪（门面返回 False）→ InfoBar 警告 + 仍收尾，不留僵尸卡"""
    host = _make_host(delivered=False)
    _patch_infobar(monkeypatch, host)
    host._pending_permission_id = "call_6"
    host._on_permission_decided("allow", "", "")
    assert host.infobar_log, "未送达必须有可见反馈"
    assert "未送达" in host.infobar_log[0][0]
    assert host.input_visible_log == [True]


def test_delivered_decision_no_infobar(monkeypatch):
    """正常投递不打扰用户"""
    host = _make_host(delivered=True)
    _patch_infobar(monkeypatch, host)
    host._pending_permission_id = "call_7"
    host._on_permission_decided("allow", "", "")
    assert host.infobar_log == []


def test_backend_exception_does_not_hang_ui(monkeypatch):
    """回传抛异常：提示 + 收尾，UI 不得卡住"""
    host = _make_host(raise_exc=True)
    _patch_infobar(monkeypatch, host)
    host._pending_permission_id = "call_8"
    host._on_permission_decided("allow", "", "")
    assert host.infobar_log, "异常应有可见反馈"
    assert host.input_visible_log == [True]


def test_cancelled_denies(monkeypatch):
    """卡片关闭 → 按 deny 处理（安全默认）"""
    host = _make_host()
    _patch_infobar(monkeypatch, host)
    host._pending_permission_id = "call_9"
    host._on_permission_cancelled()
    assert host.backend.calls == [("call_9", "deny", "", "")]
    assert host._pending_permission_id is None


# ── 三、来源分类与风险分级 ──


def test_source_classification_sandbox(monkeypatch):
    """命中沙箱 → sandbox 来源（文案含「安全中心」）"""
    import app.tools.sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod, "sandbox_check_tool", lambda *a, **k: "confirm")
    monkeypatch.setattr(sandbox_mod, "is_delete_command", lambda c: False)
    host = _make_host()
    source, text = host._classify_permission_source("bash", {"command": "curl http://x"})
    assert source == "sandbox"
    assert "安全中心" in text


def test_source_classification_delete(monkeypatch):
    """沙箱放行但为删除命令 → delete 来源"""
    import app.tools.sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod, "sandbox_check_tool", lambda *a, **k: "allow")
    monkeypatch.setattr(sandbox_mod, "is_delete_command", lambda c: c.startswith("rm"))
    host = _make_host()
    source, text = host._classify_permission_source("bash", {"command": "rm x.txt"})
    assert source == "delete"
    assert "删除保护" in text


def test_source_classification_policy(monkeypatch):
    """既非沙箱也非删除 → policy 来源"""
    import app.tools.sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod, "sandbox_check_tool", lambda *a, **k: "allow")
    monkeypatch.setattr(sandbox_mod, "is_delete_command", lambda c: False)
    host = _make_host()
    source, text = host._classify_permission_source("write", {"path": "D:/x"})
    assert source == "policy"
    assert "权限策略" in text


def test_risk_grading_delete_is_danger(monkeypatch):
    """删除类 → danger（红色 + 500ms 防护）"""
    import app.tools.sandbox as sandbox_mod
    from app.tools import registry as registry_mod

    monkeypatch.setattr(sandbox_mod, "is_delete_command", lambda c: True)
    monkeypatch.setattr(registry_mod.ToolRegistry, "get_instance", staticmethod(lambda: _FakeRegistry("safe")))
    host = _make_host()
    assert host._grade_permission_risk("bash", {"command": "rm x"}, "delete") == "danger"


def test_risk_grading_dangerous_tool_is_danger(monkeypatch):
    """registry 标 dangerous 的工具 → danger"""
    import app.tools.sandbox as sandbox_mod
    from app.tools import registry as registry_mod

    monkeypatch.setattr(sandbox_mod, "is_delete_command", lambda c: False)
    monkeypatch.setattr(registry_mod.ToolRegistry, "get_instance", staticmethod(lambda: _FakeRegistry("dangerous")))
    host = _make_host()
    assert host._grade_permission_risk("bash", {"command": "ls"}, "policy") == "danger"


def test_risk_grading_sandbox_is_warn(monkeypatch):
    """沙箱拦截（非危险工具/非删除）→ warn"""
    import app.tools.sandbox as sandbox_mod
    from app.tools import registry as registry_mod

    monkeypatch.setattr(sandbox_mod, "is_delete_command", lambda c: False)
    monkeypatch.setattr(registry_mod.ToolRegistry, "get_instance", staticmethod(lambda: _FakeRegistry("safe")))
    host = _make_host()
    assert host._grade_permission_risk("bash", {"command": "curl http://x"}, "sandbox") == "warn"


def test_risk_grading_default_info(monkeypatch):
    import app.tools.sandbox as sandbox_mod
    from app.tools import registry as registry_mod

    monkeypatch.setattr(sandbox_mod, "is_delete_command", lambda c: False)
    monkeypatch.setattr(registry_mod.ToolRegistry, "get_instance", staticmethod(lambda: _FakeRegistry("safe")))
    host = _make_host()
    assert host._grade_permission_risk("write", {"path": "D:/x"}, "policy") == "info"


class _FakeRegistry:
    def __init__(self, danger):
        self._danger = danger

    def get_danger(self, name):
        return self._danger

    def tools_in_group(self, group):
        return frozenset()


# ── 四、影响范围 ──


def test_impact_delete_paths(monkeypatch):
    import app.tools.sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod, "is_delete_command", lambda c: True)
    monkeypatch.setattr(
        sandbox_mod,
        "delete_targets",
        lambda c: {"existing": [Path("D:/a"), Path("D:/b")], "missing": ["D:/c"]},
    )
    host = _make_host()
    impact = host._build_permission_impact("bash", {"command": "rm D:/a D:/b D:/c"})
    assert impact["paths"] == ["D:\\a", "D:\\b"] or impact["paths"] == ["D:/a", "D:/b"]
    assert len(impact["missing"]) == 1


def test_impact_extracts_domains(monkeypatch):
    import app.tools.sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod, "is_delete_command", lambda c: False)
    host = _make_host()
    impact = host._build_permission_impact("bash", {"command": "curl https://evil.com/x https://ok.io/y"})
    assert "evil.com" in impact["domains"]
    assert "ok.io" in impact["domains"]


def test_impact_empty_for_plain_command(monkeypatch):
    import app.tools.sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod, "is_delete_command", lambda c: False)
    host = _make_host()
    impact = host._build_permission_impact("bash", {"command": "git status"})
    assert impact["paths"] == [] and impact["domains"] == [] and impact["writes"] is False


# ── 五、缺陷 3：僵尸卡清理 ──


def test_dismiss_cards_noop_when_nothing_pending():
    """幂等：无卡无 id 时完全无操作（不得误恢复输入区）"""
    host = _make_host()
    host._dismiss_pending_overlay_cards()
    assert host._card_manager.hidden == []
    assert host.input_visible_log == []


def test_dismiss_permission_card_when_id_pending():
    """有待决审批 id → 收起审批卡 + 恢复输入区"""
    host = _make_host()
    host._pending_permission_id = "call_x"
    host._permission_floating_widget = _FakeWidget()
    host._dismiss_pending_overlay_cards()
    assert "permission" in host._card_manager.hidden
    assert host._permission_floating_widget.cleared == 1
    assert host._pending_permission_id is None
    assert host.input_visible_log == [True]


def test_dismiss_question_card_when_visible():
    """提问卡可见 → 一并收起（共性问题修复）"""
    host = _make_host()
    host._question_floating_widget = _FakeWidget()
    host._question_floating_widget.show()
    host._question_tool_call_id = "q_1"
    host._dismiss_pending_overlay_cards()
    assert "question" in host._card_manager.hidden
    assert host._question_floating_widget.cleared == 1
    assert host._question_tool_call_id is None


def test_dismiss_both_cards():
    host = _make_host()
    host._pending_permission_id = "p_1"
    host._permission_floating_widget = _FakeWidget()
    host._question_floating_widget = _FakeWidget()
    host._question_floating_widget.show()
    host._question_tool_call_id = "q_1"
    host._dismiss_pending_overlay_cards()
    assert set(host._card_manager.hidden) == {"question", "permission"}


def test_dismiss_destroyed_window_noop():
    host = _make_host()
    host._is_destroyed = True
    host._pending_permission_id = "p_1"
    host._dismiss_pending_overlay_cards()
    assert host._card_manager.hidden == []


# ── 六、契约与自检 ──


def test_pending_permission_auto_allow_removed():
    """死字段已删（全仓仅一处赋值、零读取）"""
    from app.main_widget import OpenAIChatToolWindow

    assert not hasattr(OpenAIChatToolWindow, "_pending_permission_auto_allow")


def test_hide_all_cards_for_question_includes_permission():
    """提问卡隐藏清单必须含 permission（否则提问卡显示时审批卡不被隐藏）"""
    import inspect

    from app.main_widget import OpenAIChatToolWindow

    src = inspect.getsource(OpenAIChatToolWindow._hide_all_cards_for_question)
    assert '"permission"' in src


def test_no_text_label_parsing_in_main_widget():
    """权限路径不再产生/消费【label】文本（静默 deny 的根因已消除）"""
    src = (_REPO_ROOT / "app" / "main_widget.py").read_text(encoding="utf-8")
    assert "【允许】" not in src
    assert "【确认删除】" not in src
    assert "【本次会话允许】" not in src


def test_ai_state_question_contract_kept():
    """三合一契约：_set_ai_state("question") 必须保留（Tab 动画 + 桌宠 + 并行会话计数）"""
    import inspect

    from app.main_widget import OpenAIChatToolWindow

    src = inspect.getsource(OpenAIChatToolWindow._on_permission_approval_requested)
    assert '_set_ai_state("question")' in src

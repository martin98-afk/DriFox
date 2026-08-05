# -*- coding: utf-8 -*-
"""F6（Bug11）：TeamMail 旧数据无 _hook_event 标记的迁移修复测试

覆盖：
- normalize_message 对 TeamMail 格式消息（📨 + 任务邮件）自动补 _hook_event="TeamMail"
  （main_widget._process_team_task 真实格式 / _inject_team_mail_as_hook 变体 / 无方括号变体）
- 普通系统 hook（<system-reminder> 格式）仍按原逻辑识别，不受影响
- 普通用户消息不加标记；含"任务邮件"但无 📨 的不误标
- assistant 引用邮件文本不误标（TeamMail 识别仅 user 角色启用，防误伤）
- 迁移回写原 message 引用（持久化生效）
- F2 衔接：迁移后消息带 _hook_event="TeamMail" → get_team_first_question 第一层跳过
  （旧数据不再污染团队首问预览）
"""

from app.core.message_content import normalize_message


def _team_mail_msg(content="📨 **来自 [build@win_01] 的任务邮件：**\n\n任务内容", role="user"):
    return {"role": role, "content": content, "timestamp": "2026-01-01 09:00:00"}


class TestNormalizeTeamMailMigration:
    def test_team_mail_format_gets_marked(self):
        """main_widget 真实格式（📨 **来自 [agent@window] 的任务邮件）→ 补 TeamMail 标记"""
        msg = _team_mail_msg()
        out = normalize_message(msg)
        assert out is not None
        assert out.get("_hook_event") == "TeamMail", f"TeamMail 消息应补标记，实际 {out.get('_hook_event')}"

    def test_team_mail_variant_without_bracket(self):
        """无方括号变体（📨 来自 team 的任务邮件，测试/历史格式）也识别"""
        msg = _team_mail_msg(content="📨 来自 team 的任务邮件")
        out = normalize_message(msg)
        assert out.get("_hook_event") == "TeamMail"

    def test_system_hook_still_detected(self):
        """普通系统 hook（<system-reminder> 格式）仍按原逻辑识别"""
        msg = {
            "role": "user",
            "content": "<system-reminder><pre-user-message-hook>内容</pre-user-message-hook></system-reminder>",
        }
        out = normalize_message(msg)
        assert out.get("_hook_event") == "pre-user-message-hook"

    def test_normal_user_message_no_marker(self):
        """普通用户消息不加 _hook_event"""
        msg = {"role": "user", "content": "你好，帮我看看这个"}
        out = normalize_message(msg)
        assert "_hook_event" not in out

    def test_user_mentioning_task_without_emoji_not_marked(self):
        """用户消息含"任务邮件"但无 📨 特征 → 不误标"""
        msg = {"role": "user", "content": "帮我处理一下这个任务邮件里的问题"}
        out = normalize_message(msg)
        assert "_hook_event" not in out

    def test_assistant_citing_mail_not_marked(self):
        """assistant 回复引用邮件文本（含 📨 + 任务邮件）→ 不误标 TeamMail（Bug11 防误伤）"""
        msg = _team_mail_msg(role="assistant", content="收到，我处理 📨 **来自 [build] 的任务邮件**")
        out = normalize_message(msg)
        assert out.get("_hook_event") is None, f"assistant 引用邮件不应误标，实际 {out.get('_hook_event')}"

    def test_writes_back_to_original_message(self):
        """迁移回写原 message 引用（持久化 save/load 生效）"""
        msg = _team_mail_msg()
        normalize_message(msg)
        assert msg.get("_hook_event") == "TeamMail", "原消息引用应被回写 _hook_event"

    def test_multimodal_content_text_block_marked(self):
        """content 为 list（多模态 text block）时 TeamMail 文本块也识别"""
        msg = {"role": "user", "content": [{"type": "text", "text": "📨 **来自 [plan@win_02] 的任务邮件：**\n请评估"}]}
        out = normalize_message(msg)
        assert out.get("_hook_event") == "TeamMail"


class TestGetTeamFirstQuestionSkipsMigratedMail:
    """F2 衔接回归：迁移后旧 TeamMail 带标记 → 团队首问预览不再被邮件污染"""

    def test_first_question_skips_migrated_team_mail(self):
        """会话首条为旧 TeamMail（无标记，经 normalize 迁移后带标记）→ 首问取真实 user 问题"""
        from types import MethodType, SimpleNamespace

        from app.main_widget import OpenAIChatToolWindow  # noqa: F401  （确保导入路径无副作用）

        from app.utils.history_manager import HistoryManager

        # 构造 HistoryManager 实例（跳过 __init__，仅测 get_team_first_question 过滤逻辑）
        hm = HistoryManager.__new__(HistoryManager)
        # 消息列表：旧 TeamMail（无标记）+ 真实用户问题 + assistant 回复
        # 先经 normalize_message 迁移（模拟恢复/加载旧数据路径）
        sessions = [
            {"session_id": "s1"},
            {"session_id": "s2"},
        ]
        messages_s1 = [
            {"role": "user", "content": "📨 **来自 [build@win_01] 的任务邮件：**\n请分析代码", "timestamp": "09:00:00"},
            {"role": "assistant", "content": "开始分析", "timestamp": "09:01:00"},
        ]
        messages_s2 = [
            {"role": "user", "content": "这是真实的首问问题", "timestamp": "09:02:00"},
        ]
        # 迁移（模拟 consolidate/normalize 路径）：
        normalized_s1 = [m for m in (normalize_message(m) for m in messages_s1) if m]
        assert normalized_s1[0].get("_hook_event") == "TeamMail", "迁移前置条件：旧 TeamMail 应被补标记"

        hm.get_team_sessions_by_run_id = lambda run_id: sessions
        hm.get_session_by_session_id = lambda sid: {"messages": normalized_s1 if sid == "s1" else messages_s2}
        hm.get_team_first_question = MethodType(HistoryManager.get_team_first_question, hm)

        first_question = hm.get_team_first_question("run-1")
        assert first_question == "这是真实的首问问题", f"首问不应被 TeamMail 污染，实际 {first_question!r}"

    def test_migrated_mail_has_marker_for_skip_layers(self):
        """迁移后的 TeamMail 同时满足 get_team_first_question 的两层跳过（_hook_event + 📨 前缀）"""
        msg = _team_mail_msg()
        out = normalize_message(msg)
        assert out["_hook_event"] == "TeamMail"
        # 第一层：get_team_first_question 的 `if msg.get("_hook_event"): continue`
        assert out.get("_hook_event") is not None
        # 第二层（旧数据无标记时的兜底）：📨 **来自 前缀
        assert str(out.get("content", "")).startswith("📨 **来自")

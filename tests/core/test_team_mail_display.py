# -*- coding: utf-8 -*-
"""团队邮件消息（_hook_event="TeamMail"）显示过滤一致性测试（子任务 #15）

覆盖：
- 修复 1: group_messages_for_display 对 TeamMail user 消息生成 batch（放行显示为卡片）
- 修复 2: build_node_preview_data 对 TeamMail 生成时间线节点（与问题列表 index 对齐）
- Bug 2 定位对齐: 用户A + 邮件X + 用户B 场景，_build_node_to_batch_mapping
  含邮件 batch 索引，节点数 = 映射数 = 批次数一致
- 回归: 其他 hook（SessionStart 等）仍被过滤不显示
- UI 兜底: _is_hook_message_ui 同步放行 TeamMail（与 message_content 一致）
"""

from types import MethodType, SimpleNamespace

from app.core.message_content import _is_hook_message, group_messages_for_display
from app.widgets.ui_helpers import _is_hook_message_ui, build_node_preview_data


def _user(content="用户问题", ts="2026-01-01 09:00:00"):
    return {"role": "user", "content": content, "timestamp": ts}


def _mail(content="📨 来自 team 的任务邮件", ts="2026-01-01 09:30:00"):
    return {"role": "user", "content": content, "timestamp": ts, "_hook_event": "TeamMail"}


def _assistant(content="AI 回复", ts="2026-01-01 09:31:00"):
    return {"role": "assistant", "content": content, "timestamp": ts}


def _hook(content="<system-reminder><pre-user-message-hook>系统注入</pre-user-message-hook></system-reminder>"):
    return {"role": "user", "content": content, "_hook_event": "SessionStart"}


def _mail_preview_data(messages):
    """模拟 main_widget._build_node_to_batch_mapping（MethodType 绑定真实方法）。"""
    from app.main_widget import OpenAIChatToolWindow

    batches = group_messages_for_display(messages)
    fake = SimpleNamespace(_message_batch=batches)
    fake._build_node_to_batch_mapping = MethodType(OpenAIChatToolWindow._build_node_to_batch_mapping, fake)
    return batches, fake._build_node_to_batch_mapping()


class TestIsHookMessage:
    """修复 1 判定函数：TeamMail 放行，其他 hook 过滤"""

    def test_team_mail_allowed(self):
        """TeamMail 标记的消息不是 hook 内部通知（放行显示）。"""
        assert _is_hook_message(_mail()) is False

    def test_other_hook_filtered(self):
        """其他 hook（SessionStart）仍被过滤。"""
        assert _is_hook_message(_hook()) is True

    def test_no_event_not_hook(self):
        """无 _hook_event 的普通 user 消息不是 hook。"""
        assert _is_hook_message(_user()) is False


class TestGroupMessagesForDisplay:
    """修复 1：渲染层对 TeamMail 生成 batch"""

    def test_team_mail_generates_batch(self):
        """用户A + 邮件X + 用户B → 3 个 user batch（邮件独立成 batch）。"""
        batches = group_messages_for_display([_user("A"), _mail("邮件X"), _user("B")])
        user_batches = [b[0] for b in batches if b and b[0].get("role") == "user"]
        assert len(user_batches) == 3, "TeamMail 应放行并独立成 batch"
        assert user_batches[1]["_hook_event"] == "TeamMail", "邮件 batch 内容保留"

    def test_team_mail_content_visible(self):
        """邮件 batch 的 content 完整保留（显示为卡片）。"""
        batches = group_messages_for_display([_user("A"), _mail("任务邮件正文")])
        mail_batch = next(b for b in batches if b and b[0].get("_hook_event") == "TeamMail")
        assert mail_batch[0]["content"] == "任务邮件正文"

    def test_other_hook_still_filtered(self):
        """回归：SessionStart hook 仍被过滤不生成 batch。"""
        batches = group_messages_for_display([_user("A"), _hook()])
        user_batches = [b for b in batches if b and b[0].get("role") == "user"]
        assert len(user_batches) == 1, "其他 hook 不应生成 batch"


class TestBuildNodePreviewData:
    """修复 2：时间线节点含 TeamMail（与问题列表 index 对齐）"""

    def test_team_mail_includes_node(self):
        """用户A + 邮件X + 用户B 各带 assistant 配对：连续 user 只保留最后一个
        （与 _build_node_to_batch_mapping 语义一致），邮件节点存在且内容正确。"""
        msgs = [
            _user("A", ts="09:00:00"),
            _mail("邮件X", ts="09:30:00"),
            _assistant("回复1", ts="09:31:00"),
            _user("B", ts="10:00:00"),
            _assistant("回复2", ts="10:01:00"),
        ]
        nodes = build_node_preview_data(msgs)
        # 邮件覆盖了 A（连续 user 保留最后一个），随后 B 独立成节点 → 2 个节点
        assert len(nodes) == 2, "TeamMail 应生成时间线节点（覆盖前一 user）"
        assert nodes[0][0] == "邮件X", "邮件节点内容正确"
        assert nodes[1][0] == "B"

    def test_other_hook_not_in_node(self):
        """回归：SessionStart hook 不生成节点（current_user_msg 保持前一个 user）。"""
        msgs = [_user("A", ts="09:00:00"), _hook(), _assistant("回复", ts="09:01:00")]
        nodes = build_node_preview_data(msgs)
        assert len(nodes) == 1
        assert nodes[0][0] == "A", "hook 不应生成节点"


class TestNodeToBatchMappingAlignment:
    """Bug 2：节点索引 → batch 索引映射对齐（点击邮件定位邮件卡片）"""

    def test_mail_batch_mapped(self):
        """用户A + 邮件X + 用户B：邮件在映射中，节点数=映射数=批次数一致。"""
        msgs = [
            _user("A", ts="09:00:00"),
            _mail("邮件X", ts="09:30:00"),
            _assistant("回复1", ts="09:31:00"),
            _user("B", ts="10:00:00"),
            _assistant("回复2", ts="10:01:00"),
        ]
        batches, mapping = _mail_preview_data(msgs)
        nodes = build_node_preview_data(msgs)

        # 邮件 batch 在 _message_batch 中
        mail_batch_idx = next(i for i, b in enumerate(batches) if b and b[0].get("_hook_event") == "TeamMail")
        # 映射含邮件 batch（点击邮件节点 → 定位邮件 batch）
        assert mail_batch_idx in mapping, "邮件 batch 应在定位映射中"
        # 对齐基础：节点数 == 映射数（问题列表与定位不漂移）
        assert len(nodes) == len(mapping), f"节点数 {len(nodes)} 与映射数 {len(mapping)} 不一致"
        # 邮件节点索引 → 映射指向邮件 batch
        mail_node_idx = next(i for i, n in enumerate(nodes) if n[0] == "邮件X")
        assert mapping[mail_node_idx] == mail_batch_idx, "点击邮件节点应定位邮件 batch"

    def test_without_team_mail_behavior_unchanged(self):
        """回归：无 TeamMail 时映射与修复前一致（用户A+用户B 两节点）。"""
        msgs = [
            _user("A", ts="09:00:00"),
            _assistant("回复1", ts="09:01:00"),
            _user("B", ts="10:00:00"),
            _assistant("回复2", ts="10:01:00"),
        ]
        batches, mapping = _mail_preview_data(msgs)
        nodes = build_node_preview_data(msgs)
        assert len(nodes) == 2 and len(mapping) == 2


class TestIsHookMessageUi:
    """改动 3：UI 层兜底判断同步放行 TeamMail"""

    def test_ui_allows_team_mail(self):
        assert _is_hook_message_ui(_mail()) is False

    def test_ui_filters_other_hook(self):
        assert _is_hook_message_ui(_hook()) is True


class TestGetUserRoundRangesTeamMail:
    """子任务 #22：数据 round 范围与 UI 渲染/卡片 round_index 口径统一（TeamMail 独立 round）

    会话 [A(用户), X(TeamMail), B(用户)]：B 卡片 round_index=2（batch 含 X），
    若 round_ranges 只有 2 个 → 撤回静默失败 + 差异统计 cannot determine valid round_index。
    修复后 get_user_round_ranges 放行 TeamMail，三者口径一致（TeamMail 撤回/统计双杀修复）。
    """

    def test_team_mail_forms_independent_round(self):
        """用户A + 邮件X + 用户B → 3 个 round（TeamMail 独立成 round）。"""
        from app.core.message_content import get_user_round_ranges

        msgs = [_user("A"), _mail("邮件X"), _user("B")]
        ranges = get_user_round_ranges(msgs)
        assert len(ranges) == 3, f"TeamMail 应独立成 round，实际 {len(ranges)} 个：{ranges}"
        # 各 round 独立：A / X / B
        assert ranges[0] == (0, 1), "round 0 应为 A"
        assert ranges[1] == (1, 2), "round 1 应为 TeamMail X"
        assert ranges[2] == (2, 3), "round 2 应为 B"

    def test_team_mail_round_index_matches_display(self):
        """B 卡片 round_index=2 必须落在 ranges[2]（UI 渲染 batch 口径一致）。"""
        from app.core.message_content import get_user_round_ranges

        msgs = [_user("A"), _mail("邮件X"), _user("B")]
        ranges = get_user_round_ranges(msgs)
        # 卡片 round_index 由 batch 计数（含 TeamMail）得到：B 是第 3 个 user batch → index=2
        # 必须 < len(ranges)=3，否则 _on_card_diff_requested 走 cannot determine 分支
        assert 2 < len(ranges), "round_index=2 必须落在 round_ranges 内（否则差异统计失败）"
        assert ranges[2][0] <= 2 < ranges[2][1]

    def test_leading_hook_merges_into_team_mail_round(self):
        """TeamMail 前导 hook 并入 TeamMail round（删除邮件轮次时连同 hook 一起删）。

        注意：用 PreUserMessage hook（非 SessionStart —— SessionStart 是会话级，
        不并入任何 round）。
        """
        from app.core.message_content import get_user_round_ranges

        pre_hook = {"role": "user", "content": "<reminder>pre</reminder>", "_hook_event": "PreUserMessage"}
        msgs = [_user("A"), pre_hook, _mail("邮件X"), _user("B")]
        ranges = get_user_round_ranges(msgs)
        assert len(ranges) == 3, f"前导 hook 并入 TeamMail round，仍 3 个 round：{ranges}"
        # round 1 向前扩展：起点含前导 hook（idx 1），end 为 B 起点（idx 3）
        assert ranges[1] == (1, 3), f"TeamMail round 应含前导 hook：{ranges}"

    def test_stop_block_user_still_merged(self):
        """回归：非 TeamMail 的 hook 合成 user（StopBlock）仍并入前一个真实 round。"""
        from app.core.message_content import get_user_round_ranges

        stop = {"role": "user", "content": "x", "_hook_event": "StopBlock"}
        msgs = [_user("A"), stop, _user("B")]
        ranges = get_user_round_ranges(msgs)
        assert len(ranges) == 2, f"StopBlock 不应独立成 round：{ranges}"

    def test_plain_session_unchanged(self):
        """回归：无 TeamMail 的普通会话 round_ranges 不变。"""
        from app.core.message_content import get_user_round_ranges

        msgs = [_user("A"), _assistant("回复1"), _user("B"), _assistant("回复2")]
        ranges = get_user_round_ranges(msgs)
        assert len(ranges) == 2, f"普通会话应 2 个 round：{ranges}"

    def test_ast_round_ranges_allows_team_mail(self):
        """AST 回归：get_user_round_ranges 的 user_indices 过滤必须放行 TeamMail。

        防止未来被改回 `not msg.get('_hook_event')`（会重新造成 TeamMail 撤回/统计双杀）。
        """
        import ast
        import re
        from pathlib import Path

        src_path = Path(__file__).resolve().parent.parent.parent / "app" / "core" / "message_content.py"
        tree = ast.parse(src_path.read_text(encoding="utf-8"))

        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "get_user_round_ranges":
                target = node
                break
        assert target is not None, "未找到 get_user_round_ranges 方法"

        import textwrap

        func_src = textwrap.dedent(ast.unparse(target))
        # 过滤条件必须含 TeamMail 放行：or msg.get('_hook_event') == 'TeamMail'
        assert re.search(
            r"""not msg\.get\(['"]_hook_event['"]\) or msg\.get\(['"]_hook_event['"]\) == ['"]TeamMail['"]""",
            func_src,
        ), (
            "get_user_round_ranges 的 user_indices 过滤必须放行 TeamMail"
            "（`not _hook_event or _hook_event == 'TeamMail'`），否则 TeamMail 撤回/统计双杀回归"
        )

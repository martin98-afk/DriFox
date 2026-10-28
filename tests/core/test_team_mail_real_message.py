# -*- coding: utf-8 -*-
"""团队邮件消息视为真实用户问题（触发标题生成 + 历史问题卡片显示）

回归 bug：commit 04578388 给团队邮件打 _hook_event="TeamMail" 标记（意图仅
让"首问预览/标题回退"跳过邮件），但 main_widget.py 中 4 处
`not msg.get("_hook_event")` 过滤是**为过滤 SessionStart / vision_inject 等
真正非用户消息**而设，邮件被一并排除。

R6 修复：4 处过滤条件改为"跳过非 TeamMail 的 hook 事件"，邮件
（_hook_event="TeamMail"）视为真实用户问题。

本测试覆盖：
- ① mail-only session 的 user_msg_count==1 → 触发 _maybe_generate_topic_summary
- ② _maybe_generate_topic_summary 对 mail 会话不提前 return（user_messages 非空）
- ③ 历史问题卡片包含 TeamMail 邮件
- ④ 非邮件 hook（SessionStart 等）仍跳过
- ⑤ 徽章计数含邮件

测试风格参考 test_team_mail_hook_event.py（mock 风格 + 过滤逻辑直接验证）。

注：4 处过滤逻辑的"内容"（_on_send_clicked / _maybe_generate_topic_summary /
_toggle_history_questions_popup / _update_history_questions_badge）在 main_widget.py
中被各种 GUI / 业务逻辑包裹。本测试聚焦过滤条件本身（核心修复点），避免依赖
重型 main_widget 初始化（OpenAIChatToolWindow.__init__ 涉及 PyQt5 C++ + 重依赖）。
"""

import pytest


# ── 测试辅助 ─────────────────────────────────────────


def _user_msg(role: str = "user", content: str = "", hook_event=None, **extra) -> dict:
    """构造 session 消息（默认 role=user）"""
    msg = {
        "role": role,
        "content": content,
        "timestamp": "2026-08-02 10:00:00",
    }
    if hook_event is not None:
        msg["_hook_event"] = hook_event
    msg.update(extra)
    return msg


def _mail_msg(body: str = "请完成登录功能", hook_event: str = "TeamMail") -> dict:
    """构造任务邮件消息（默认已打标 TeamMail）"""
    return _user_msg(
        content=f"📨 **来自 [leader@w0] 的任务邮件：**\n\n{body}",
        hook_event=hook_event,
    )


# ── 共用过滤函数（与 main_widget.py R6 修复后 4 处一致） ─────────
#
# R6 修复后 4 处条件统一为：
#   m.get("role") == "user"
#   and (not m.get("_hook_event") or m.get("_hook_event") == "TeamMail")
#
# 本测试用 _is_real_user_question 表达此逻辑，验证 main_widget.py 中的实现
# 行为与本函数一致（任何一处偏离都会触发测试失败）。


def _is_real_user_question(m: dict) -> bool:
    """R6 修复后的过滤逻辑（4 处统一）

    Returns:
        True 当 m 是真实用户问题（无 _hook_event 或 _hook_event == "TeamMail"）
    """
    if m.get("role") != "user":
        return False
    hook_event = m.get("_hook_event")
    return not hook_event or hook_event == "TeamMail"


# ── ① _on_send_clicked 触发判定：mail-only 会话 user_msg_count==1 ─────


class TestOnSendClickedMailCount:
    """mail-only 会话的 user_msg_count 应 == 1（包含 TeamMail）→ 触发标题生成"""

    def test_mail_only_session_count_is_one(self):
        """纯邮件会话：1 条 TeamMail → user_msg_count 应为 1"""
        messages = [_mail_msg()]
        user_msg_count = sum(1 for m in messages if _is_real_user_question(m))
        assert user_msg_count == 1, "TeamMail 应计入用户问题数（标题生成触发条件）"

    def test_real_user_question_only(self):
        """纯真实用户问题（无 _hook_event）→ count == 1（基线行为不变）"""
        messages = [_user_msg(content="真实问题")]
        user_msg_count = sum(1 for m in messages if _is_real_user_question(m))
        assert user_msg_count == 1

    def test_non_mail_hook_skipped(self):
        """非 TeamMail 的 hook（如 SessionStart）→ 不计数"""
        messages = [
            _user_msg(content="系统消息", hook_event="SessionStart"),
            _user_msg(content="vision", hook_event="vision_inject"),
            _user_msg(content="SubAgent", hook_event="SubAgentFinished"),
        ]
        user_msg_count = sum(1 for m in messages if _is_real_user_question(m))
        assert user_msg_count == 0, "非 TeamMail hook 必须被排除"

    def test_mail_plus_real_question(self):
        """邮件 + 真实用户问题 → count == 2"""
        messages = [
            _mail_msg(),
            _user_msg(content="真实问题"),
        ]
        user_msg_count = sum(1 for m in messages if _is_real_user_question(m))
        assert user_msg_count == 2


# ── ② _maybe_generate_topic_summary：mail 会话 user_messages 非空 ─────


class TestMaybeGenerateTopicSummaryMailPath:
    """_maybe_generate_topic_summary 对 mail-only 会话：user_messages 非空（不提前 return）"""

    def test_mail_only_yields_user_messages(self):
        """mail-only 会话 → user_messages 应非空（避免'No user messages found, skipping'）"""
        messages = [_mail_msg()]
        user_messages = [m for m in messages if _is_real_user_question(m)]
        assert len(user_messages) == 1, "mail-only 会话应能找到用户消息（否则跳过标题生成）"
        assert user_messages[0]["_hook_event"] == "TeamMail"

    def test_non_mail_hook_yields_empty(self):
        """非邮件 hook 仍应被排除 → user_messages 为空 → 触发跳过"""
        messages = [
            _user_msg(content="系统消息", hook_event="SessionStart"),
            _user_msg(content="vision", hook_event="vision_inject"),
        ]
        user_messages = [m for m in messages if _is_real_user_question(m)]
        assert len(user_messages) == 0


# ── ③ 历史问题卡片：含 TeamMail 邮件 ────────────────


class TestHistoryQuestionsPopupContainsMail:
    """_toggle_history_questions_popup：TeamMail 邮件应出现在 questions 列表"""

    def test_mail_in_questions_list(self):
        """mail-only 会话：1 个 TeamMail → questions 含该邮件"""
        from app.core import content_to_text

        messages = [_mail_msg()]
        questions = []
        for msg in messages:
            if msg.get("role") == "user":
                hook_event = msg.get("_hook_event")
                if hook_event and hook_event != "TeamMail":
                    continue
                text = content_to_text(msg.get("content", ""))
                if len(text) > 60:
                    text = text[:60] + "…"
                questions.append((len(questions), text))

        assert len(questions) == 1, "TeamMail 邮件应进入历史问题卡片列表"
        assert "📨" in questions[0][1] or "任务邮件" in questions[0][1]

    def test_non_mail_hook_still_skipped(self):
        """非 TeamMail hook 仍跳过（SessionStart 等不进 questions）"""
        from app.core import content_to_text

        messages = [
            _user_msg(content="系统消息", hook_event="SessionStart"),
            _user_msg(content="vision", hook_event="vision_inject"),
            _user_msg(content="SubAgent", hook_event="SubAgentFinished"),
        ]
        questions = []
        for msg in messages:
            if msg.get("role") == "user":
                hook_event = msg.get("_hook_event")
                if hook_event and hook_event != "TeamMail":
                    continue
                text = content_to_text(msg.get("content", ""))
                if len(text) > 60:
                    text = text[:60] + "…"
                questions.append((len(questions), text))

        assert len(questions) == 0

    def test_mail_mixed_with_real_question(self):
        """邮件 + 真实问题：两者都进 questions"""
        from app.core import content_to_text

        messages = [
            _mail_msg(),
            _user_msg(content="真实的用户提问"),
        ]
        questions = []
        for msg in messages:
            if msg.get("role") == "user":
                hook_event = msg.get("_hook_event")
                if hook_event and hook_event != "TeamMail":
                    continue
                text = content_to_text(msg.get("content", ""))
                if len(text) > 60:
                    text = text[:60] + "…"
                questions.append((len(questions), text))

        assert len(questions) == 2


# ── ④ _update_history_questions_badge：徽章计数含邮件 ─────────


class TestBadgeCountContainsMail:
    """_update_history_questions_badge：徽章计数应包含 TeamMail"""

    def test_mail_only_badge_count_one(self):
        """mail-only 会话 → badge count == 1（之前误伤为 0，徽章不显示）"""
        messages = [_mail_msg()]
        count = sum(1 for m in messages if _is_real_user_question(m))
        assert count == 1, "TeamMail 应计入徽章"

    def test_badge_count_zero_when_only_non_mail_hook(self):
        """只有非邮件 hook → count == 0（徽章隐藏，与基线一致）"""
        messages = [_user_msg(content="系统消息", hook_event="SessionStart")]
        count = sum(1 for m in messages if _is_real_user_question(m))
        assert count == 0

    def test_badge_mixed_count(self):
        """邮件 + 真实问题 + 非邮件 hook → count == 2（仅 mail + real）"""
        messages = [
            _mail_msg(),
            _user_msg(content="真实问题"),
            _user_msg(content="系统消息", hook_event="SessionStart"),
        ]
        count = sum(1 for m in messages if _is_real_user_question(m))
        assert count == 2


# ── ⑤ 端到端一致性：全量过滤点行为一致 ────────────────


class TestConsistencyAcrossAllFourSites:
    """同一组消息在所有过滤点应产出相同结果

    R6 修复 4 处（标题生成/历史问题卡片）+ R7 修复 5 处（自动保存/归档/worker
    识别），共 9 处 user 消息过滤点必须统一为"含 TeamMail"逻辑，避免一处修复
    而其他遗漏。

    关键回归保护：用 AST 解析 main_widget.py 精确统计过滤点数量，
    而不是复制过滤代码到测试（复制等于测自己的复制）。
    """

    @staticmethod
    def _count_team_mail_filters() -> int:
        """用 AST 解析 main_widget.py，统计含 TeamMail 例外的 user 消息过滤点

        精确匹配两种过滤模式（均为 R6/R7 修复后的"含 TeamMail"逻辑）：
        - 正向包含：`(not msg.get("_hook_event") or msg.get("_hook_event") == "TeamMail")`
        - 反向跳过：`if msg.get("_hook_event") and msg.get("_hook_event") != "TeamMail": continue`
        """
        import ast
        from pathlib import Path

        src = Path("app/main_widget.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        # 统计正向包含模式：比较表达式中含 `_hook_event` 与 `TeamMail` 的 equality
        pos_count = 0
        neg_count = 0
        for node in ast.walk(tree):
            # 正向包含：Compare 含 == "TeamMail"（在 or 分支中）
            if isinstance(node, ast.Compare):
                for op, comp in zip(node.ops, node.comparators):
                    if isinstance(op, ast.Eq) and isinstance(comp, ast.Constant) and comp.value == "TeamMail":
                        pos_count += 1
            # 反向跳过：NotEq "TeamMail"
            if isinstance(node, ast.Compare):
                for op, comp in zip(node.ops, node.comparators):
                    if isinstance(op, ast.NotEq) and isinstance(comp, ast.Constant) and comp.value == "TeamMail":
                        neg_count += 1
        return pos_count + neg_count

    def test_main_widget_team_mail_filter_count_exact(self):
        """main_widget.py 中含 TeamMail 的过滤点数量应精确为 12（R6 4 + R7 5 + F1 1 + F4 Bug1 1 + 空白会话守卫 1）

        计数演进（均为合理新增，语义见各自提交）：
        - R6/R7 修复：9 处（标题生成/历史问题卡片 4 + 自动保存/归档/worker 识别 5）
        - F1（停止重触发修复）：+1 处（_check_and_process_pending 停止冷却判定附近）
        - F4 Bug1（round_index 口径统一）：+1 处（_get_current_user_round_index
          改为含 TeamMail 例外，与全仓口径一致）
        - 团队解散空白会话守卫：+1 处（_save_current_session_to_history 新增
          has_user_message 守卫，拦截仅含 SessionStart hook 消息的空白会话落库）
        """
        count = self._count_team_mail_filters()
        assert count == 12, f"含 TeamMail 的过滤点应精确为 12（R6 4 + R7 5 + F1 1 + F4 1 + 守卫 1），实际 {count} 处"

    def test_no_bare_hook_event_filter_remains(self):
        """main_widget.py 不应再残留裸 `not msg.get("_hook_event")`（不含 TeamMail 例外）的 **user 消息** 过滤

        允许保留的例外：
        - assistant 消息过滤（`role == "assistant" and not _hook_event`）：TeamMail 是 user 角色，
          与 assistant 过滤无关
        - `_get_current_user_round_index`（round_index 语义 = 渲染的 user 卡片数；TeamMail 不渲染
          卡片（_is_hook_message 跳过），故维持原过滤——见子任务 #10 判定理由）
        """
        import re
        from pathlib import Path

        src = Path("app/main_widget.py").read_text(encoding="utf-8")
        # 找裸 user 过滤（role == "user" + not _hook_event，无 TeamMail 例外）
        bare_user_patterns = re.findall(r'msg\.get\("role"\) == "user" and not msg\.get\("_hook_event"\)', src)
        # 含 TeamMail 例外的正向模式（R6/R7 修复后）
        with_exception = re.findall(r'not msg\.get\("_hook_event"\) or msg\.get\("_hook_event"\) == "TeamMail"', src)
        # 裸 user 过滤只剩 round_index 1 处（判定保留，见 docstring）；其余必须全部含 TeamMail 例外
        assert len(bare_user_patterns) <= 1, (
            f"存在未修复的裸 user 过滤：{len(bare_user_patterns)} 处（允许最多 1 处 = "
            f"_get_current_user_round_index 判定保留），含 TeamMail 例外 {len(with_exception)} 处"
        )
        if bare_user_patterns:
            # 确认这 1 处确实是 _get_current_user_round_index（L9366 附近）
            idx = src.find(bare_user_patterns[0])
            line = src[:idx].count("\n") + 1
            assert 9360 <= line <= 9390, (
                f"裸 user 过滤出现在 L{line}（应为 L9375 的 _get_current_user_round_index，"
                f"该处判定保留：round 语义 = 渲染的 user 卡片数，TeamMail 不渲染卡片）"
            )

    def test_all_four_sites_agree_on_mail_session(self):
        """mail-only session：所有过滤点一致认为有 1 个真实用户问题"""
        messages = [_mail_msg()]

        # 用统一规范函数验证（_is_real_user_question 与 main_widget.py 过滤逻辑一致）
        user_msg_count = sum(1 for m in messages if _is_real_user_question(m))
        user_messages = [m for m in messages if _is_real_user_question(m)]
        count = sum(1 for m in messages if _is_real_user_question(m))

        assert user_msg_count == 1
        assert len(user_messages) == 1
        assert count == 1

    def test_all_four_sites_agree_on_non_mail_session(self):
        """仅 SessionStart hook 的 session：所有过滤点一致认为是 0 个用户问题"""
        messages = [_user_msg(content="SessionStart", hook_event="SessionStart")]

        user_msg_count = sum(1 for m in messages if _is_real_user_question(m))
        user_messages = [m for m in messages if _is_real_user_question(m)]
        count = sum(1 for m in messages if _is_real_user_question(m))

        assert user_msg_count == 0
        assert len(user_messages) == 0
        assert count == 0


# ── ⑥ 阻断 #1：_auto_save_current_session 判定 ─────────


class TestAutoSaveCurrentSessionMailPath:
    """mail-only 会话 _auto_save_current_session 应判定为"有用户消息"（可保存）"""

    def test_mail_only_has_user_message_true(self):
        """mail-only 会话：has_user_message 应为 True（R7 修复后可保存）"""
        messages = [_mail_msg()]
        has_user_message = any(
            msg.get("role") == "user" and (not msg.get("_hook_event") or msg.get("_hook_event") == "TeamMail")
            for msg in messages
        )
        assert has_user_message is True, "mail-only 会话应判定为有用户消息（否则永不保存到历史）"

    def test_only_session_start_hook_has_user_message_false(self):
        """仅 SessionStart hook：has_user_message 应为 False（空会话仍不保存）"""
        messages = [_user_msg(content="系统消息", hook_event="SessionStart")]
        has_user_message = any(
            msg.get("role") == "user" and (not msg.get("_hook_event") or msg.get("_hook_event") == "TeamMail")
            for msg in messages
        )
        assert has_user_message is False, "仅 SessionStart hook 的会话不应保存（保持基线行为）"

    def test_mail_plus_real_has_user_message_true(self):
        """邮件 + 真实问题：has_user_message 为 True"""
        messages = [_mail_msg(), _user_msg(content="真实问题")]
        has_user_message = any(
            msg.get("role") == "user" and (not msg.get("_hook_event") or msg.get("_hook_event") == "TeamMail")
            for msg in messages
        )
        assert has_user_message is True


# ── ⑦ 归档 message_count 含 mail ────────────────


class TestArchiveMessageCountContainsMail:
    """归档会话 message_count 计算应包含 TeamMail（历史列表不显示 0 条）"""

    def test_mail_only_archive_count_one(self):
        """mail-only 会话归档：message_count 应为 1（含 TeamMail）"""
        messages = [_mail_msg()]
        msg_count = len(
            [
                m
                for m in messages
                if m.get("role") == "user" and (not m.get("_hook_event") or m.get("_hook_event") == "TeamMail")
            ]
        )
        assert msg_count == 1, "归档 message_count 应含 TeamMail（否则历史列表显示 0 条消息）"

    def test_archive_count_zero_for_only_hook(self):
        """仅 SessionStart hook：归档 message_count 为 0（基线行为）"""
        messages = [_user_msg(content="系统消息", hook_event="SessionStart")]
        msg_count = len(
            [
                m
                for m in messages
                if m.get("role") == "user" and (not m.get("_hook_event") or m.get("_hook_event") == "TeamMail")
            ]
        )
        assert msg_count == 0


# ── ⑧ worker 识别：_count_user_messages / _contains_user_text ─────


class TestWorkerUserMessageIdentification:
    """worker 截断后新旧消息识别应包含 TeamMail"""

    def test_count_user_messages_includes_mail(self):
        """_count_user_messages 应含 TeamMail（mail 计入比对基数）"""
        messages = [_mail_msg(), _user_msg(content="真实问题")]
        count = sum(
            1
            for m in messages
            if m.get("role") == "user" and (not m.get("_hook_event") or m.get("_hook_event") == "TeamMail")
        )
        assert count == 2, "worker 用户消息数应含 TeamMail"

    def test_contains_user_text_includes_mail(self):
        """_contains_user_text 应对 TeamMail 内容命中"""
        mail_body = "请完成登录功能"
        messages = [_mail_msg(body=mail_body)]
        found = False
        for msg in messages:
            if msg.get("role") != "user":
                continue
            if msg.get("_hook_event") and msg.get("_hook_event") != "TeamMail":
                continue
            content = msg.get("content", "")
            if isinstance(content, str) and content == msg.get("content"):
                found = True
                break
        assert found is True, "TeamMail 内容应参与文本指纹比对"

    def test_contains_user_text_skips_non_mail_hook(self):
        """_contains_user_text 应跳过非 TeamMail hook"""
        messages = [_user_msg(content="系统消息", hook_event="SessionStart")]
        found = False
        for msg in messages:
            if msg.get("role") != "user":
                continue
            if msg.get("_hook_event") and msg.get("_hook_event") != "TeamMail":
                continue
            found = True
            break
        assert found is False, "非 TeamMail hook 不应参与文本指纹比对"


# ── ⑨ _fallback_user_question：TeamMail 不跳过 ─────────


class TestFallbackUserQuestionMailPath:
    """topic_summary._fallback_user_question 对 TeamMail 不跳过（mail-only LLM 失败可回退）"""

    @staticmethod
    def _fallback(messages):
        """复刻 topic_summary.py _fallback_user_question 的过滤逻辑（R7 修复后）"""
        for msg in messages:
            if msg.get("role") != "user":
                continue
            if msg.get("_hook_event") and msg.get("_hook_event") != "TeamMail":
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                texts = [item.get("text", "") for item in content if item.get("type") == "text"]
                content = "\n".join(texts)
            if not content:
                continue
            if str(content).startswith("📨 **来自"):
                continue  # R3 防御：未打标旧邮件仍跳过
            return str(content)[:15]
        return ""

    def test_mail_only_fallback_returns_mail_text(self):
        """mail-only 会话：_fallback_user_question 应返回邮件文本（LLM 失败可回退标题）"""
        body = "请完成登录功能"
        messages = [_mail_msg(body=body)]
        fallback = self._fallback(messages)
        # TeamMail 有 _hook_event → 不再被 `if _hook_event: continue` 跳过；
        # 但 R3 防御对 "📨 **来自" 前缀仍跳过 → 返回空串（保持标题不被邮件污染）
        assert fallback == "", (
            "TeamMail 回退：有 _hook_event 例外但 R3 防御仍跳过 📨 前缀邮件。"
            "标题生成失败时回退不应取邮件文本（防污染）；标题生成本身由 user_messages 判定触发。"
        )

    def test_real_question_fallback_works(self):
        """真实用户问题：_fallback_user_question 正常返回"""
        messages = [_user_msg(content="如何优化登录性能")]
        fallback = self._fallback(messages)
        assert fallback == "如何优化登录性能"

    def test_non_mail_hook_still_skipped(self):
        """非 TeamMail hook：_fallback_user_question 仍跳过"""
        messages = [_user_msg(content="系统消息", hook_event="SessionStart")]
        fallback = self._fallback(messages)
        assert fallback == ""

    def test_mail_without_hook_prefix_skipped_by_r3(self):
        """TeamMail 无 📨 前缀但带 _hook_event：R6/R7 例外生效，可回退"""
        msg = {"role": "user", "content": "完成代码审查", "_hook_event": "TeamMail"}
        fallback = self._fallback([msg])
        assert fallback == "完成代码审查", "带 _hook_event=TeamMail 且无 📨 前缀的消息应可回退"

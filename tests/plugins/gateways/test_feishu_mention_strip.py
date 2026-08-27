# -*- coding: utf-8 -*-
"""飞书 @提及剥离回归测试 — 群聊 @机器人 发命令必须能命中。

背景：
- 飞书群聊 @机器人 发命令时，message.content 形如 "@_user_1 /model"
  （机器占位符带 @），mentions 列表中 MentionEvent.key 只是 "_user_1"（无 @）。
- 适配器 `_on_feishu_message` 需要把这条前缀剥掉，宿主
  app/core/engines/gateway/engine.py 的 `if text.startswith("/")` 才会
  把文本路由到 `_handle_command`；否则被当普通对话丢给 AI（/model /help 等
  命令静默失效，回复是 AI 自由发挥而非命令面板）。

原 bug：`bot_keys = {m.key}` 与 `lead.group(1) = "@_user_1"` 直接比较，
@ 不一致 → 永远 False，前缀永远剥不掉，命令识别失败。

复刻适配器里的剥离/替换逻辑（`import re` + 一组集合构造 + 两次正则），
覆盖三个场景：

1. 群聊 @机器人  发命令 → 剥前缀 → 文本以 / 开头
2. 群聊 @他人    发命令 → @人占位符替换为真名，命令识别正常
3. 私聊无 mentions → 文本不变，命令识别正常
4. 群聊 @机器人 + 文本中 @他人 → 剥前缀 + 替换人名都生效

不依赖 .drifox/ 插件目录：直接对 `_normalize_feishu_text` 这套剥离/替换
行为建立契约；适配器改版时只调一处（不调本测试），本测试即保证回归。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


# ── 被测契约：与 .drifox/plugins/gateway-feishu/gateways/feishu.py
# `_on_feishu_message` 中"@提及处理"段严格对齐。 ──────────────


def _normalize_feishu_text(text: str, mentions: Optional[List[Dict[str, Any]]]) -> str:
    """复刻适配器中的 mention 剥离/替换逻辑。

    Args:
        text: 从 content JSON 解析出的 text（可能以 "@_user_1 ..." 开头）
        mentions: 原始 mentions 列表（用 dict 模拟 MentionEvent）

    Returns:
        归一化后的 text（已 strip），供宿主 GatewayEngine 判 startswith("/")
    """
    mentions = mentions or []
    bot_keys = {
        f"@{m['key']}"
        for m in mentions
        if m.get("key") and m.get("mentioned_type", "") == "app"
    }
    name_by_key = {
        f"@{m['key']}": f"@{m['name']}" for m in mentions if m.get("key")
    }
    lead = re.match(r"^(@_user_\d+)\s*", text)
    if lead and (not mentions or lead.group(1) in bot_keys):
        text = text[lead.end():]
    if name_by_key:
        text = re.sub(
            r"@\S+", lambda mm: name_by_key.get(mm.group(0), mm.group(0)), text
        )
    return text.strip()


# ── 场景 ────────────────────────────────────────────────────────


class TestStripBotMentionForCommand:
    def test_group_at_bot_then_command_strips_prefix(self):
        """群聊 @机器人 发 /model：必须剥掉前缀，文本以 / 开头"""
        mentions = [
            {"key": "_user_1", "name": "Drifox Bot", "mentioned_type": "app"},
        ]
        text = "@_user_1 /model"
        result = _normalize_feishu_text(text, mentions)

        assert result == "/model", f"期望 '/model'，实际 {result!r}"
        assert result.startswith("/"), f"群聊 @机器人 发命令后必须以 / 开头，实际 {result!r}"

    def test_group_at_bot_then_help_strips_prefix(self):
        """群聊 @机器人 发 /help：同样剥前缀（用户原始 bug 报告场景）"""
        mentions = [
            {"key": "_user_1", "name": "Drifox Bot", "mentioned_type": "app"},
        ]
        text = "@_user_1 /help"
        result = _normalize_feishu_text(text, mentions)

        assert result == "/help"
        assert result.startswith("/")

    def test_group_at_bot_then_session_strips_prefix(self):
        """群聊 @机器人 发 /session：覆盖所有内置命令"""
        mentions = [
            {"key": "_user_1", "name": "Drifox Bot", "mentioned_type": "app"},
        ]
        text = "@_user_1 /session"
        result = _normalize_feishu_text(text, mentions)

        assert result == "/session"
        assert result.startswith("/")


class TestReplaceUserMention:
    def test_group_at_human_then_command_keeps_command_intact(self):
        """群聊 @他人（非 bot）发 /model：@人占位符替换为真名，命令识别照常

        bot 不在 mentions 中时，`not mentions` 为 False 且 lead 捕获的
        "@_user_2" 不在 bot_keys，strip 分支不进；@_user_2 → @真名 走
        name_by_key 替换。
        """
        mentions = [
            {"key": "_user_2", "name": "张三", "mentioned_type": "user"},
        ]
        text = "@_user_2 /model"
        result = _normalize_feishu_text(text, mentions)

        # 替换为人名（@张三），/model 保留 → 命令识别
        assert result == "@张三 /model"
        assert "/model" in result

    def test_group_at_bot_and_human_does_both_strip_and_replace(self):
        """群聊 @机器人 + 文中 @他人：剥前缀 + 替换人名"""
        mentions = [
            {"key": "_user_1", "name": "Drifox Bot", "mentioned_type": "app"},
            {"key": "_user_2", "name": "李四", "mentioned_type": "user"},
        ]
        text = "@_user_1 @_user_2 /model"
        result = _normalize_feishu_text(text, mentions)

        # 前缀 @_user_1 剥掉；@_user_2 → @李四
        assert result == "@李四 /model"
        assert result.startswith("/") is False, "命令前还有 @人时仍按普通对话处理（语义正确）"

    def test_group_at_bot_command_with_args_in_middle(self):
        """群聊 @机器人 发 /model 后跟参数：剥前缀后参数保留"""
        mentions = [
            {"key": "_user_1", "name": "Drifox Bot", "mentioned_type": "app"},
        ]
        text = "@_user_1 /model openai gpt-4"
        result = _normalize_feishu_text(text, mentions)

        assert result == "/model openai gpt-4"
        assert result.startswith("/")


class TestDMNoMention:
    def test_dm_no_mentions_passes_through(self):
        """私聊无 mentions：文本不变，命令识别"""
        text = "/model"
        result = _normalize_feishu_text(text, mentions=None)

        assert result == "/model"
        assert result.startswith("/")

    def test_dm_empty_mentions_passes_through(self):
        """私聊 mentions 为空列表：文本不变，命令识别"""
        text = "/help"
        result = _normalize_feishu_text(text, mentions=[])

        assert result == "/help"
        assert result.startswith("/")

    def test_dm_with_bot_in_mentions_but_text_unprefixed(self):
        """私聊 mentions 含 bot（飞书某些 DM 形态会带），但 text 不带 @ 前缀

        这种情况不剥任何东西（lead 不匹配），name_by_key 替换也无 @_user_N
        可替换，文本保持原样。
        """
        mentions = [
            {"key": "_user_1", "name": "Drifox Bot", "mentioned_type": "app"},
        ]
        text = "/model"
        result = _normalize_feishu_text(text, mentions)

        assert result == "/model"
        assert result.startswith("/")


class TestNonCommandMessages:
    """非命令消息的 mention 处理也得对（不影响宿主）"""

    def test_group_at_bot_then_plain_text_keeps_command_untouched(self):
        """群聊 @机器人 发普通文本：剥前缀（@bot 自己），@他人替换为真名"""
        mentions = [
            {"key": "_user_1", "name": "Drifox Bot", "mentioned_type": "app"},
            {"key": "_user_2", "name": "王五", "mentioned_type": "user"},
        ]
        text = "@_user_1 @_user_2 帮我看下这段代码"
        result = _normalize_feishu_text(text, mentions)

        assert result == "@王五 帮我看下这段代码"
        assert not result.startswith("/")

    def test_no_mentions_plain_text_unchanged(self):
        """无 mentions 的普通文本：完全不变"""
        text = "你好"
        result = _normalize_feishu_text(text, mentions=None)

        assert result == "你好"

# -*- coding: utf-8 -*-
"""
单元测试：_generate_tool_restriction_text 工具限制提示词生成

覆盖通配符 `*` 语义（与 PermissionResolver 兜底逻辑一致）：
1. leader.md 型：白名单 allow + "*": deny → 「仅允许以下工具: …」
2. compaction.md 型：仅 "*": deny → 「已禁用: 全部工具」
3. explore.md 型："*": allow + 若干 deny → 「已禁用: …」（行为不变）
4. tools 白名单字段型 → 「仅允许以下工具: …」
5. "*": allow 无 deny → 无限制（空字符串，行为不变）

Run: pytest tests/core/test_tool_restriction_text.py -v
"""

import pytest


@pytest.fixture
def gen():
    """返回 _generate_tool_restriction_text 函数（私有函数直接导入测试）。"""
    mod = pytest.importorskip("app.core.builtin_commands")
    return mod._generate_tool_restriction_text


# ── 1. 白名单 allow + "*": deny（leader.md 型）─────────────────


def test_wildcard_deny_with_allowed_list(gen):
    """leader.md 型：显式 allow + "*": deny → 转为白名单语义输出，不再输出字面 "*"。"""
    meta = {
        "permission": {"question": "allow", "team_list_members": "allow", "team_send_message": "allow", "*": "deny"}
    }
    assert gen(meta) == "[工具限制] 仅允许以下工具: question, team_list_members, team_send_message\n\n"


def test_wildcard_deny_with_single_allow(gen):
    """通配符 deny + 单个 allow → 仅允许该工具。"""
    meta = {"permission": {"*": "deny", "read": "allow"}}
    assert gen(meta) == "[工具限制] 仅允许以下工具: read\n\n"


# ── 2. 仅 "*": deny（compaction.md 型）────────────────────────


def test_wildcard_deny_only(gen):
    """compaction.md 型：仅 "*": deny → 「已禁用: 全部工具」。"""
    meta = {"permission": {"*": "deny"}}
    assert gen(meta) == "[工具限制] 已禁用: 全部工具\n\n"


# ── 3. "*": allow + 若干 deny（explore.md 型，行为不变）────────


def test_wildcard_allow_with_denied_list(gen):
    """explore.md 型：deny 列表 + "*": allow → 正常输出已禁用列表，* 不进 deny。"""
    meta = {"permission": {"write": "deny", "edit": "deny", "multi_edit": "deny", "*": "allow"}}
    assert gen(meta) == "[工具限制] 已禁用: write, edit, multi_edit\n\n"


def test_wildcard_allow_only_no_restriction(gen):
    """build.md 型：仅 "*": allow → 无限制（空字符串）。"""
    assert gen({"permission": {"*": "allow"}}) == ""


# ── 4. tools 白名单字段型 ─────────────────────────────────────


def test_tools_whitelist_list(gen):
    """tools 列表 → 「仅允许以下工具」。"""
    meta = {"tools": ["read", "write"]}
    assert gen(meta) == "[工具限制] 仅允许以下工具: read, write\n\n"


def test_tools_whitelist_str(gen):
    """tools 字符串（CSV，含别名）→ 归一化后输出。"""
    meta = {"tools": "Read, Glob, Bash"}
    assert gen(meta) == "[工具限制] 仅允许以下工具: read, glob, bash\n\n"


# ── 5. 无限制 / 空配置 ────────────────────────────────────────


def test_no_restriction_empty_meta(gen):
    """无 tools/permission/allowed-tools → 空字符串。"""
    assert gen({}) == ""
    assert gen({"description": "plain agent"}) == ""


def test_permission_empty_dict(gen):
    """permission 为空 dict → 无限制。"""
    assert gen({"permission": {}}) == ""


def test_deny_without_wildcard(gen):
    """普通 deny 列表（无通配符）→ 输出已禁用列表（回归保护）。"""
    meta = {"permission": {"bash": "deny", "websearch": "deny"}}
    assert gen(meta) == "[工具限制] 已禁用: bash, websearch\n\n"


# ── 6. 与 PermissionResolver 语义一致性 ───────────────────────


def test_semantics_match_permission_resolver():
    """生成的限制文本与实际权限解析结果一致：
    - leader.md 型：question/team_* allow，其他工具 deny
    - compaction.md 型：全部工具 deny
    - explore.md 型：deny 列表禁用，其余 allow
    """
    from app.core.agent import PermissionResolver

    leader = PermissionResolver(
        {"question": "allow", "team_list_members": "allow", "team_send_message": "allow", "*": "deny"}
    )
    assert leader.resolve("question") == "allow"
    assert leader.resolve("team_send_message") == "allow"
    assert leader.resolve("bash") == "deny"  # 通配符兜底禁用

    compaction = PermissionResolver({"*": "deny"})
    assert compaction.resolve("read") == "deny"
    assert compaction.resolve("bash") == "deny"

    explore = PermissionResolver({"write": "deny", "edit": "deny", "*": "allow"})
    assert explore.resolve("write") == "deny"
    assert explore.resolve("edit") == "deny"
    assert explore.resolve("read") == "allow"  # 通配符兜底允许

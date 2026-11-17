# -*- coding: utf-8 -*-
"""HookMatchRule 正则预编译回归测试（2026-11-17 N3 性能优化）

锁定三条不变式：
1. 坏正则在加载期被捕获（_compiled=None + logger.warning），匹配期早退 False，
   不再依赖每次匹配的 try/except 静默失败；
2. 合法正则三条匹配路径（message / response / tool_name）行为与预编译前一致；
3. response 匹配的 64KB 超长护栏生效：截断点之后的内容不参与匹配
   （有意行为变化，仅 DoS 级超长回复触发，正常回复不受影响）。
"""

import pytest
from loguru import logger

from app.core.hooks.hook_manager import HookMatchRule


def _ctx(event: str = "PostAssistantMessage", **kw) -> dict:
    ctx = {
        "event_name": event,
        "message": "",
        "response": "",
        "assistant_response": "",
    }
    ctx.update(kw)
    return ctx


def test_bad_regex_compiles_to_none_and_matches_false():
    """坏正则：加载期不抛异常，匹配期早退 False"""
    rule = HookMatchRule(matcher="[")  # 非法正则（未闭合字符集）
    assert rule._compiled is None
    assert rule._compiled_ic is None
    assert rule.matches(_ctx(message="anything")) is False


def test_bad_regex_logs_warning_at_load():
    """坏正则加载期输出 warning（含 skill_name 与 matcher，静默变可见）"""
    records = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        HookMatchRule(matcher="(", skill_name="my-skill")
    finally:
        logger.remove(sink_id)
    assert any("my-skill" in r for r in records)


def test_valid_regex_matches_message_and_response():
    """合法正则 message / response 两条路径行为与预编译前一致"""
    rule = HookMatchRule(matcher=r"^deploy\s+\w+")
    assert rule.matches(_ctx(message="deploy now")) is True
    assert rule.matches(_ctx(response="deploy now")) is True
    assert rule.matches(_ctx(message="rollback now")) is False


def test_tool_name_matcher_ignorecase_still_works():
    """工具名匹配保持大小写不敏感（编译版不能把 flags 误当 pos）"""
    rule = HookMatchRule(matcher="Write|Edit")
    assert rule.matches(_ctx(event="PreToolUse", tool_name="write")) is True
    assert rule.matches(_ctx(event="PreToolUse", tool_name="read")) is False


def test_long_response_truncated_at_64kb():
    """超长护栏：64KB 截断点之后的内容不参与匹配（有意行为变化）"""
    rule = HookMatchRule(matcher=r"end$")
    assert rule.matches(_ctx(response="x" * 70000 + "end")) is False
    # 截断点之内的正常匹配不受影响
    rule2 = HookMatchRule(matcher=r"^ok")
    assert rule2.matches(_ctx(response="ok" + "x" * 100000)) is True

# -*- coding: utf-8 -*-
"""TopicSummaryTask 标题生成失败回退测试

覆盖需求：标题生成失败（API 异常 / JSON 解析失败 / 返回空 topic_summary）
时，回退用「用户问题」（第一条真实用户消息，跳过 _hook_event 与未打标
任务邮件）作为标题，而不是：
- 旧行为：取最后一条消息前 15 字（可能命中 assistant 消息）
- 旧行为：callback(None, error) 导致标题完全不更新

数据准备说明：直接构造消息列表驱动 TopicSummaryTask.run()，
通过 monkeypatch create_api_call_with_retry 控制 API 成功/失败分支。
"""

import pytest

from app.core.workers.topic_summary import TopicSummaryTask

_LLM_CONFIG = {"API_KEY": "test-key", "API_URL": "https://test.example", "模型名称": "gpt-4o"}


class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeResp:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]


def _run_task(messages, resp_content=None, exc=None):
    """执行 TopicSummaryTask，返回 [(result, error), ...] 回调记录。"""
    results = []

    def callback(result, error=None):
        results.append((result, error))

    def fake_retry(client, create_task, cancel_check=None):
        if exc is not None:
            raise exc
        return _FakeResp(resp_content)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("app.core.workers.topic_summary.create_api_call_with_retry", fake_retry)
    try:
        task = TopicSummaryTask(messages=messages, llm_config=_LLM_CONFIG, callback=callback)
        task.run()
    finally:
        monkeypatch.undo()
    return results


def _user_msg(content: str, **extra) -> dict:
    msg = {"role": "user", "content": content}
    msg.update(extra)
    return msg


def _assistant_msg(content: str) -> dict:
    return {"role": "assistant", "content": content}


def _mail_msg(content: str = "📨 **来自 [leader@w0] 的任务邮件：**\n\n请完成登录功能", **extra) -> dict:
    """构造任务邮件消息（默认无 _hook_event，模拟未打标旧记录）。"""
    msg = {"role": "user", "content": content}
    msg.update(extra)
    return msg


# ---------- 成功路径（回归保护） ----------


def test_success_returns_llm_summary():
    """API 正常返回 JSON → 直接采用 LLM 生成的标题。"""
    results = _run_task(
        messages=[_user_msg("帮我写一个登录页面")],
        resp_content='{"topic_summary": "实现登录页面"}',
    )
    assert len(results) == 1
    result, error = results[0]
    assert error is None
    assert result == {"topic_summary": "实现登录页面"}


# ---------- JSON 解析失败 → 回退用户问题 ----------


def test_parse_failure_falls_back_to_first_user_question():
    """LLM 返回非 JSON 文本（解析失败）→ 回退第一条用户问题。"""
    results = _run_task(
        messages=[_user_msg("帮我排查登录接口 500 报错")],
        resp_content="抱歉，我无法生成标题。",
    )
    result, error = results[0]
    assert error is None
    assert result["topic_summary"] == "帮我排查登录接口 500 报错"


def test_parse_failure_skips_hook_mail_takes_real_question():
    """首条是 _hook_event 任务邮件 → 回退取第一条真实用户问题。"""
    results = _run_task(
        messages=[
            _mail_msg(_hook_event="TeamMail"),
            _user_msg("帮我设计数据库表结构"),
        ],
        resp_content="not-json",
    )
    result, error = results[0]
    assert error is None
    assert result["topic_summary"] == "帮我设计数据库表结构"


def test_parse_failure_skips_untagged_mail_takes_real_question():
    """首条是未打标任务邮件（📨 前缀，R3 防御）→ 回退取真实用户问题。"""
    results = _run_task(
        messages=[
            _mail_msg(),
            _user_msg("实现用户注册功能"),
        ],
        resp_content="not-json",
    )
    result, error = results[0]
    assert error is None
    assert result["topic_summary"] == "实现用户注册功能"


def test_parse_failure_skips_assistant_and_takes_user_question():
    """回退时跳过 assistant 消息，取第一条用户问题。"""
    results = _run_task(
        messages=[
            _assistant_msg("好的，我来帮你。"),
            _user_msg("怎么优化这个慢查询？"),
        ],
        resp_content="not-json",
    )
    result, error = results[0]
    assert error is None
    assert result["topic_summary"] == "怎么优化这个慢查询？"


def test_parse_failure_truncates_long_question():
    """用户问题过长 → 回退标题截断到 15 字。"""
    long_question = "请帮我分析一下这个电商系统的订单超时未支付自动取消功能应该怎么设计"
    results = _run_task(
        messages=[_user_msg(long_question)],
        resp_content="not-json",
    )
    result, error = results[0]
    assert error is None
    assert result["topic_summary"] == long_question[:15]


# ---------- API 异常 → 回退用户问题 ----------


def test_api_exception_falls_back_to_user_question():
    """API 调用抛异常 → 回退第一条用户问题（而非回调 None+error）。"""
    results = _run_task(
        messages=[_user_msg("帮我生成一个 PPT 大纲")],
        exc=RuntimeError("connection timeout"),
    )
    result, error = results[0]
    assert error is None
    assert result["topic_summary"] == "帮我生成一个 PPT 大纲"


def test_api_exception_without_user_message_keeps_error():
    """API 异常且没有可回退的用户消息 → 保留 error 回调（主线程不更新标题）。"""
    results = _run_task(
        messages=[_assistant_msg("你好")],
        exc=RuntimeError("connection timeout"),
    )
    result, error = results[0]
    assert result is None
    assert error is not None


# ---------- 返回空 topic_summary → 回退用户问题 ----------


def test_empty_topic_summary_falls_back_to_user_question():
    """LLM 返回空 topic_summary → 回退第一条用户问题。"""
    results = _run_task(
        messages=[_user_msg("帮我调试这个 bug")],
        resp_content='{"topic_summary": ""}',
    )
    result, error = results[0]
    assert error is None
    assert result["topic_summary"] == "帮我调试这个 bug"


def test_blank_topic_summary_falls_back_to_user_question():
    """LLM 返回空白 topic_summary → 回退第一条用户问题。"""
    results = _run_task(
        messages=[_user_msg("帮我调试这个 bug")],
        resp_content='{"topic_summary": "   "}',
    )
    result, error = results[0]
    assert error is None
    assert result["topic_summary"] == "帮我调试这个 bug"


def test_api_empty_choices_falls_back_to_user_question():
    """API 返回空 choices → 回退第一条用户问题。"""
    results = []

    def callback(result, error=None):
        results.append((result, error))

    def fake_retry(client, create_task, cancel_check=None):
        return _FakeResp("")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("app.core.workers.topic_summary.create_api_call_with_retry", fake_retry)
    try:
        task = TopicSummaryTask(
            messages=[_user_msg("帮我配置 CI 流水线")],
            llm_config=_LLM_CONFIG,
            callback=callback,
        )
        task.run()
    finally:
        monkeypatch.undo()
    result, error = results[0]
    assert error is None
    assert result["topic_summary"] == "帮我配置 CI 流水线"

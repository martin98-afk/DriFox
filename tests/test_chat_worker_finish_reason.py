# -*- coding: utf-8 -*-
"""
回归测试：流式响应截断（finish_reason='length' / content_filter / 空响应）不得静默当正常完成

背景：
- ChatWorker._process_response 流式循环从不检查 chunk.choices[0].finish_reason。
- 当服务端因 max_tokens 截断（finish_reason='length'）、内容过滤（'content_filter'）
  或异常提前结束流时，代码把不完整的响应当「正常完成」→ 无报错、无重试、无提示，
  UI 显示"完成"但回复只有前半截 → 用户感知「莫名其妙中断」（工具调用迭代中
  输出 token 消耗大，最易触达截断）。
- 修复：流结束后校验 finish_reason，截断/过滤/空响应抛 StreamInterruptedError，
  由 _handle_error 给出明确提示（partial 内容仍保留）。

本测试验证：
1. finish_reason='length' → 抛出 StreamInterruptedError（而非静默返回正常完成）
2. finish_reason='content_filter' → 抛出 StreamInterruptedError
3. 空响应（finish_reason='stop' 但无任何内容/reasoning/tool_calls）→ 抛出
4. 正常完成（'stop' 有内容）→ 不抛，正常返回
5. 正常工具调用结束（'tool_calls'）→ 不抛
"""

import sys
from pathlib import Path

# 仓库根目录
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class FakeDelta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class FakeChunk:
    def __init__(self, choices, usage=None):
        self.choices = choices
        self.usage = usage


class FakeResp:
    """可迭代的流式响应"""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __iter__(self):
        return iter(self._chunks)


class FakeClient:
    def __init__(self, resp):
        self.calls = 0
        self.resp = resp
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        self.calls += 1
        return self.resp


def _make_worker():
    from app.core.workers.chat_worker import OpenAIChatWorker

    w = OpenAIChatWorker(
        messages=[],
        session_messages=[],
        llm_config={
            "模型名称": "gpt-4",
            "API_KEY": "test-key",
            "API_URL": "https://api.openai.com/v1",
            "温度": 0,
            "思考模式": "off",
        },
    )
    w._api_messages_cache = None
    return w


def pytest_raises(exc):
    """轻量 pytest.raises 替代（避免依赖 pytest 安装）"""
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        try:
            yield
        except exc:
            return
        except Exception as e:
            raise AssertionError(f"期望抛出 {exc.__name__}，实际 {type(e).__name__}: {e}")
        raise AssertionError(f"期望抛出 {exc.__name__}，但未抛出")

    return _ctx()


def test_length_truncation_raises(monkeypatch):
    """finish_reason='length'（max_tokens 截断）→ 必须抛 StreamInterruptedError"""
    from app.core.workers import chat_worker as cw
    from app.core.workers.chat_worker import StreamInterruptedError

    resp = FakeResp(
        [
            FakeChunk([FakeChoice(FakeDelta("你好，这是一段"), None)]),
            FakeChunk([FakeChoice(FakeDelta("很长的回复内容"), None)]),
            FakeChunk([FakeChoice(FakeDelta(None), "length")]),
        ]
    )
    w = _make_worker()
    w._http_client = FakeClient(resp)

    with pytest_raises(StreamInterruptedError):
        w._make_api_call([{"role": "user", "content": "hi"}], use_cache=False)
    print("  ✓ length 截断抛出 StreamInterruptedError")


def test_content_filter_raises(monkeypatch):
    """finish_reason='content_filter' → 必须抛 StreamInterruptedError"""
    from app.core.workers.chat_worker import StreamInterruptedError

    resp = FakeResp(
        [
            FakeChunk([FakeChoice(FakeDelta("部分内容"), None)]),
            FakeChunk([FakeChoice(FakeDelta(None), "content_filter")]),
        ]
    )
    w = _make_worker()
    w._http_client = FakeClient(resp)

    with pytest_raises(StreamInterruptedError):
        w._make_api_call([{"role": "user", "content": "hi"}], use_cache=False)
    print("  ✓ content_filter 抛出 StreamInterruptedError")


def test_empty_response_raises(monkeypatch):
    """空响应（stop 但无内容/reasoning/tool_calls）→ 必须抛 StreamInterruptedError"""
    from app.core.workers.chat_worker import StreamInterruptedError

    resp = FakeResp(
        [
            FakeChunk([FakeChoice(FakeDelta(None), "stop")]),
        ]
    )
    w = _make_worker()
    w._http_client = FakeClient(resp)

    with pytest_raises(StreamInterruptedError):
        w._make_api_call([{"role": "user", "content": "hi"}], use_cache=False)
    print("  ✓ 空响应抛出 StreamInterruptedError")


def test_normal_stop_ok(monkeypatch):
    """正常完成（stop + 有内容）→ 不抛，返回 (False, _)"""
    resp = FakeResp(
        [
            FakeChunk([FakeChoice(FakeDelta("完整的回复内容"), None)]),
            FakeChunk([FakeChoice(FakeDelta(None), "stop")]),
        ]
    )
    w = _make_worker()
    w._http_client = FakeClient(resp)

    result = w._make_api_call([{"role": "user", "content": "hi"}], use_cache=False)
    assert result[0] is False, f"正常完成应无工具调用，实际 tool_calls_found={result[0]}"
    print("  ✓ 正常 stop 完成不抛异常")


def test_tool_calls_finish_ok(monkeypatch):
    """工具调用结束（tool_calls）→ 不抛，返回 (True, _)"""
    resp = FakeResp(
        [
            FakeChunk(
                [
                    FakeChoice(
                        FakeDelta(
                            content=None,
                            tool_calls=[
                                type(
                                    "TC",
                                    (),
                                    {
                                        "id": "call_1",
                                        "index": 0,
                                        "type": "function",
                                        "function": type("F", (), {"name": "bash", "arguments": ""})(),
                                    },
                                )()
                            ],
                        ),
                        None,
                    )
                ]
            ),
            FakeChunk([FakeChoice(FakeDelta(None), "tool_calls")]),
        ]
    )
    w = _make_worker()
    w._http_client = FakeClient(resp)

    result = w._make_api_call([{"role": "user", "content": "hi"}], use_cache=False)
    assert result[0] is True, f"工具调用应 tool_calls_found=True，实际 {result[0]}"
    print("  ✓ tool_calls 结束不抛异常")


def test_usage_only_response_raises(monkeypatch):
    """仅 usage chunk（choices 为空且无内容）→ 空响应，必须抛 StreamInterruptedError"""
    from app.core.workers.chat_worker import StreamInterruptedError

    resp = FakeResp(
        [
            FakeChunk([], usage=type("U", (), {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10})()),
        ]
    )
    w = _make_worker()
    w._http_client = FakeClient(resp)

    with pytest_raises(StreamInterruptedError):
        w._make_api_call([{"role": "user", "content": "hi"}], use_cache=False)
    print("  ✓ 仅 usage 空响应抛出 StreamInterruptedError")


if __name__ == "__main__":
    test_length_truncation_raises(None)
    test_content_filter_raises(None)
    test_empty_response_raises(None)
    test_normal_stop_ok(None)
    test_tool_calls_finish_ok(None)
    test_usage_only_response_raises(None)
    print("手动运行通过")

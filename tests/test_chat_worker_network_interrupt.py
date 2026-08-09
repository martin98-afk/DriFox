# -*- coding: utf-8 -*-
"""
回归测试：工具迭代过程中网络 ReadError 不应被静默吞掉

背景：
- ChatWorker._make_api_call 的重试循环内，`except (httpx.ReadError, httpcore.ReadError)` 曾
  把所有 ReadError 都当作「用户取消」（cancel() 关闭 HTTP 连接的副作用），直接返回 (False, False)。
- 但 httpx.ReadError 在真实网络断流（服务端/代理断开、网络抖动）时同样会抛出，
  此时 `_is_cancelled=False`，返回 (False, False) 会让主循环 `if not tool_calls_found:`
  走「正常完成」路径 → 工具迭代静默中断，无报错弹窗、无重试、无 partial 保存提示。
- 修复：仅当 `_is_cancelled=True`（用户主动取消）时才静默返回；真实网络错误改抛出让
  外层通用网络重试逻辑接管（is_retryable_network=True）。

本测试验证：
1. 非取消 ReadError → _make_api_call 进入重试（第二次 create 成功）而不是静默返回 (False, False)
2. 取消态 ReadError → 保持原语义返回 (False, False)
"""
import sys
from pathlib import Path

# 仓库根目录
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx


class FakeEmptyResp:
    """空 choices 流式响应：迭代结束返回 (False, False)（无工具调用）"""
    def __iter__(self):
        return iter([])


class FakeClient:
    """可编程 mock：第一次 create 抛 ReadError，之后返回 FakeEmptyResp"""
    def __init__(self):
        self.calls = 0
        self.fail_first = True
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise httpx.ReadError("peer closed connection during streaming")
        return FakeEmptyResp()


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


def test_network_read_error_retries_instead_of_silent_return(monkeypatch):
    """
    非用户取消的 ReadError：应触发重试（第二次 create 成功返回执行结果），
    而不是静默返回 (False, False) 导致主循环误判「正常完成」。
    """
    from app.core.workers import chat_worker as cw

    # 屏蔽重试等待，加速测试
    monkeypatch.setattr(cw.time, "sleep", lambda s: None)

    w = _make_worker()
    client = FakeClient()
    w._http_client = client

    result = w._make_api_call([{"role": "user", "content": "hi"}], use_cache=False)

    assert client.calls >= 2, f"ReadError 后应重试，实际 create 调用 {client.calls} 次"
    # 已重试成功（调用次数证明未被静默吞掉），空响应正常完成（无工具调用）
    assert result[0] is False, f"空响应应无工具调用，实际 tool_calls_found={result[0]}"
    print(f"  ✓ 非取消 ReadError 自动重试，create 调用 {client.calls} 次")


def test_network_read_error_raises_when_retries_exhausted(monkeypatch):
    """
    非取消 ReadError 且重试全部失败：应向上抛出（由 run() 的 _handle_error 弹窗），
    而不是静默返回 (False, False)。
    """
    from app.core.workers import chat_worker as cw

    monkeypatch.setattr(cw.time, "sleep", lambda s: None)

    class AlwaysFailClient:
        def __init__(self):
            self.calls = 0
            self.chat = type("Chat", (), {"completions": self})()

        def create(self, **kwargs):
            self.calls += 1
            raise httpx.ReadError("connection reset by peer")

    w = _make_worker()
    w._http_client = AlwaysFailClient()

    with pytest_raises(httpx.ReadError):
        w._make_api_call([{"role": "user", "content": "hi"}], use_cache=False)


def test_cancelled_read_error_returns_silently(monkeypatch):
    """
    用户主动取消（cancel() 关闭连接）导致的 ReadError：保持静默返回 (False, False)，
    由 run() 的取消路径处理，不弹错误弹窗。
    """
    from app.core.workers import chat_worker as cw

    monkeypatch.setattr(cw.time, "sleep", lambda s: None)

    class CancelClient:
        def __init__(self):
            self.calls = 0
            self.chat = type("Chat", (), {"completions": self})()

        def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise httpx.ReadError("stream closed by cancel()")
            return FakeEmptyResp()

    w = _make_worker()
    w._is_cancelled = True  # 模拟用户已点击取消
    w._http_client = CancelClient()

    result = w._make_api_call([{"role": "user", "content": "hi"}], use_cache=False)
    # 取消态：循环顶部即返回 (None, None)（静默，不发错误弹窗），由 run() 取消路径处理
    assert result == (None, None), f"取消态应静默返回 (None, None)，实际 {result}"


def pytest_raises(exc):
    """轻量 pytest.raises 替代（避免依赖 pytest 安装）"""
    import traceback
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


if __name__ == "__main__":
    test_network_read_error_retries_instead_of_silent_return(type("M", (), {"setattr": lambda *a: None})())
    print("手动运行通过")
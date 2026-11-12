# -*- coding: utf-8 -*-
"""回归测试：API 重试退避封顶 + 重试状态回调

背景（2026-11-12）：
- 子智能体用不可达模型时，``create_api_call_with_retry`` 的指数退避
  ``base_delay * 2**attempt``（15 次）累计可达 22 小时，且重试期间不上报
  任何 UI 信号；面板只剩计时器在动，观感等同于「卡死在正在运行中」。
- 修复：单次退避封顶 30s；新增 ``on_retry`` 回调供 worker 推面板状态。
"""
from unittest.mock import patch

import httpx
import pytest
from openai import InternalServerError

from app.core.workers.error_handler.retry_helper import (
    MAX_RETRY_DELAY_SECONDS,
    create_api_call_with_retry,
    get_retry_delay,
)


def _server_error() -> InternalServerError:
    request = httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions")
    return InternalServerError("boom", response=httpx.Response(500, request=request), body=None)


class TestRetryDelayCap:
    def test_exponential_backoff_is_capped(self):
        err = _server_error()
        delays = [get_retry_delay(err, attempt, 5.0) for attempt in range(8)]
        assert delays[:3] == [5.0, 10.0, 20.0]
        assert all(d <= MAX_RETRY_DELAY_SECONDS for d in delays)
        # 封顶后 15 次累计远小于旧实现的 81915s
        assert sum(delays) < 300

    def test_billing_still_returns_inf(self):
        from app.core.workers.error_handler.error_classifier import FailoverReason

        err = _server_error()
        with patch("app.core.workers.error_handler.retry_helper.classify_error") as classify:
            classify.return_value = type("C", (), {"reason": FailoverReason.billing})()
            assert get_retry_delay(err, 0, 5.0) == float("inf")


class TestOnRetryCallback:
    def test_callback_receives_each_attempt(self):
        err = _server_error()
        calls = []

        def create_func():
            raise err

        with patch(
            "app.core.workers.error_handler.retry_helper.get_retry_delay", return_value=0.01
        ):
            with pytest.raises(InternalServerError):
                create_api_call_with_retry(
                    client=None,
                    create_func=create_func,
                    max_retries=3,
                    retry_delay=5.0,
                    on_retry=lambda e, attempt, total, wait: calls.append((attempt, total, wait)),
                )

        # 前两次失败各通知一次；最后一次失败直接抛出，不再等待
        assert [(c[0], c[1]) for c in calls] == [(1, 3), (2, 3)]

    def test_callback_exception_does_not_break_retry(self):
        err = _server_error()
        attempts = []

        def create_func():
            attempts.append(1)
            if len(attempts) < 3:
                raise err
            return "ok"

        def boom_callback(*_args):
            raise RuntimeError("callback failed")

        with patch(
            "app.core.workers.error_handler.retry_helper.get_retry_delay", return_value=0.01
        ):
            result = create_api_call_with_retry(
                client=None,
                create_func=create_func,
                max_retries=3,
                retry_delay=5.0,
                on_retry=boom_callback,
            )

        assert result == "ok"
        assert len(attempts) == 3

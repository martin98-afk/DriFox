# -*- coding: utf-8 -*-
"""chat_worker 健康回写：错误分类纯函数 + config_id 现算。

不真跑 worker 线程：分类函数直接调用；_report_health monkeypatch service。
status_code 分支用带属性的假异常（分类逻辑只 getattr status_code）；
timeout/connection 分支用 object.__new__ 绕过 SDK 构造签名（isinstance 判定不受影响）。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class _FakeStatusErr(Exception):
    """带 status_code 属性的假异常（对齐 openai APIStatusError 形态）"""

    def __init__(self, code: int):
        super().__init__(f"fake-{code}")
        self.status_code = code


class TestClassifyErrorForHealth:
    def test_401_auth_failed(self):
        from app.core.workers.chat_worker import OpenAIChatWorker

        assert OpenAIChatWorker._classify_error_for_health(_FakeStatusErr(401)) == "auth_failed"

    def test_403_auth_failed(self):
        from app.core.workers.chat_worker import OpenAIChatWorker

        assert OpenAIChatWorker._classify_error_for_health(_FakeStatusErr(403)) == "auth_failed"

    def test_402_auth_failed(self):
        """402 余额不足属凭据层 → auth_failed"""
        from app.core.workers.chat_worker import OpenAIChatWorker

        assert OpenAIChatWorker._classify_error_for_health(_FakeStatusErr(402)) == "auth_failed"

    def test_429_skipped(self):
        """限流 = 端点通，瞬态不改状态"""
        from app.core.workers.chat_worker import OpenAIChatWorker

        assert OpenAIChatWorker._classify_error_for_health(_FakeStatusErr(429)) is None

    def test_502_unreachable(self):
        from app.core.workers.chat_worker import OpenAIChatWorker

        assert OpenAIChatWorker._classify_error_for_health(_FakeStatusErr(502)) == "unreachable"

    def test_timeout_unreachable(self):
        import httpx

        from app.core.workers.chat_worker import OpenAIChatWorker, _get_openai_errors

        err = _get_openai_errors()["timeout"](httpx.Request("POST", "http://x"))
        assert OpenAIChatWorker._classify_error_for_health(err) == "unreachable"

    def test_connection_error_unreachable(self):
        import httpx

        from app.core.workers.chat_worker import OpenAIChatWorker, _get_openai_errors

        err = _get_openai_errors()["api_connection"](request=httpx.Request("POST", "http://x"))
        assert OpenAIChatWorker._classify_error_for_health(err) == "unreachable"

    def test_plugin_code_error_skipped(self):
        """插件代码故障（AttributeError 等）与服务商无关 → 跳过"""
        from app.core.workers.chat_worker import OpenAIChatWorker

        for err in (KeyError("plugin bug"), AttributeError("x"), TypeError("y")):
            assert OpenAIChatWorker._classify_error_for_health(err) is None

    def test_unknown_exception_skipped(self):
        from app.core.workers.chat_worker import OpenAIChatWorker

        assert OpenAIChatWorker._classify_error_for_health(ValueError("unknown")) is None

    def test_no_status_code_attr_skipped(self):
        """无 status_code 属性且非超时/连接类 → 保守跳过"""
        from app.core.workers.chat_worker import OpenAIChatWorker

        assert OpenAIChatWorker._classify_error_for_health(Exception("plain")) is None


class TestReportHealth:
    def test_report_computes_config_id_and_calls_service(self, monkeypatch):
        """config_id 由 (API_URL, API_KEY) 现算，与 compute_provider_config_id 一致"""
        from app.core.modelmeta.model_refresh_service import ModelRefreshService
        from app.core.modelmeta.provider_profile import compute_provider_config_id
        import app.core.workers.chat_worker as cw_mod

        seen = {}
        svc = SimpleNamespace(report_result=lambda cid, s: seen.update(cid=cid, s=s))
        monkeypatch.setattr(ModelRefreshService, "get_instance", classmethod(lambda cls: svc))

        fake_self = SimpleNamespace(llm_config={"API_URL": "https://x/v1", "API_KEY": "k", "认证方式": "bearer"})
        cw_mod.OpenAIChatWorker._report_health(fake_self, "ok")
        assert seen["cid"] == compute_provider_config_id(fake_self.llm_config)
        assert seen["s"] == "ok"

    def test_report_swallows_exceptions(self, monkeypatch):
        from app.core.modelmeta.model_refresh_service import ModelRefreshService
        import app.core.workers.chat_worker as cw_mod

        def boom(cid, s):
            raise RuntimeError("x")

        monkeypatch.setattr(
            ModelRefreshService,
            "get_instance",
            classmethod(lambda cls: SimpleNamespace(report_result=boom)),
        )
        fake_self = SimpleNamespace(llm_config={})
        cw_mod.OpenAIChatWorker._report_health(fake_self, "ok")  # 不抛即可

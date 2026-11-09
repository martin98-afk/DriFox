# -*- coding: utf-8 -*-
"""模型列表定时刷新服务（P1-12 / 波7）回归测试

覆盖：
1. 到期判定（23h59m 跳过 / 24h01m 触发 / 时间戳缺失视为到期）
2. sync 注册集（双开关任一 True 才进 active）
3. 健康检查独立语义（不写模型列表）
4. 静默 merge 落盘（自动刷新 + 默认模型仍在）
5. 默认模型失效 → apply=False（不写列表）
6. 401 → auth_failed
7. 超时 → unreachable
8. hook 路径
9. REST 三元组解析
10. in_flight 去重

网络全部 mock；`_on_refresh_result` 直接调用（模拟主线程槽），不依赖 Qt 事件循环。
"""

import sys
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.modelmeta import model_refresh_service as mrs
from app.core.modelmeta.model_refresh_service import (
    HEALTH_INTERVAL_S,
    KEY_AUTO_REFRESH,
    KEY_HEALTH_CHECK,
    KEY_HEALTH_TS,
    KEY_LAST_REFRESH,
    KEY_MODELS,
    KEY_REFRESH_STATUS,
    KEY_STATUS_TS,
    REFRESH_INTERVAL_S,
    STATUS_AUTH_FAILED,
    STATUS_OK,
    STATUS_UNREACHABLE,
    ModelRefreshService,
    is_default_model_safe,
    parse_refresh_ts,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ══════════════════════════════════════════════════════════════════
# 1. 到期判定
# ══════════════════════════════════════════════════════════════════


class TestDue:
    def test_not_due_at_23h59m(self):
        now = 1_000_000_000.0
        info = {KEY_LAST_REFRESH: now - (REFRESH_INTERVAL_S - 60)}
        assert ModelRefreshService.is_due(info, now) is False

    def test_due_at_24h01m(self):
        now = 1_000_000_000.0
        info = {KEY_LAST_REFRESH: now - (REFRESH_INTERVAL_S + 60)}
        assert ModelRefreshService.is_due(info, now) is True

    def test_missing_timestamp_is_due(self):
        """从未刷新 → 到期（首次启用即抓一次）"""
        info = {KEY_AUTO_REFRESH: True}
        assert ModelRefreshService.is_due(info, 1_000_000_000.0) is True

    def test_unparsable_timestamp_is_due(self):
        assert ModelRefreshService.is_due({KEY_LAST_REFRESH: "垃圾数据"}, 1_000_000_000.0) is True

    def test_parse_various_formats(self):
        assert parse_refresh_ts("") == 0.0
        assert parse_refresh_ts(None) == 0.0
        assert parse_refresh_ts(12345) == 12345.0
        assert parse_refresh_ts("2026-10-09 12:00:00") > 0


class TestHealthDue:
    def test_health_due_independent_of_list_ts(self):
        """列表 23h59m 前刚刷过，但健康计时 2h 前到期 → 健康检查仍触发"""
        now = 1_000_000_000.0
        info = {
            KEY_LAST_REFRESH: now - (REFRESH_INTERVAL_S - 60),
            KEY_HEALTH_TS: now - (HEALTH_INTERVAL_S + 60),
        }
        assert ModelRefreshService.is_due(info, now) is False
        assert ModelRefreshService.is_health_due(info, now) is True

    def test_health_not_due_within_1h(self):
        now = 1_000_000_000.0
        info = {KEY_HEALTH_TS: now - (HEALTH_INTERVAL_S - 60)}
        assert ModelRefreshService.is_health_due(info, now) is False

    def test_health_missing_ts_is_due(self):
        assert ModelRefreshService.is_health_due({"健康检查": True}, 1_000_000_000.0) is True


class TestTickDispatch:
    def _catch_thread(self, monkeypatch, svc, calls):
        """把 _on_tick 派发的线程参数捕获到 calls 列表"""

        class _FakeThread:
            def __init__(self, target=None, args=(), daemon=None, name=None):
                calls.append(args)

            def start(self):
                pass

        monkeypatch.setattr(mrs.threading, "Thread", _FakeThread)
        svc._in_flight.clear()

    def test_dispatch_both_channels_when_both_due(self, service, monkeypatch):
        calls = []
        self._catch_thread(monkeypatch, service, calls)
        service.sync_from_config({"c1": {
            KEY_AUTO_REFRESH: True,
            KEY_HEALTH_CHECK: True,
            KEY_LAST_REFRESH: 0,  # 从未 → 双到期
        }})
        service._on_tick()
        assert len(calls) == 1
        config_id, info, do_models, do_health = calls[0]
        assert config_id == "c1" and do_models is True and do_health is True

    def test_dispatch_health_only_when_list_fresh(self, service, monkeypatch):
        """列表刚刷过（24h 内）但健康到期 → 只派健康探测"""
        calls = []
        self._catch_thread(monkeypatch, service, calls)
        service.sync_from_config({"c1": {
            KEY_AUTO_REFRESH: True,
            KEY_HEALTH_CHECK: True,
            KEY_LAST_REFRESH: time.time() - 3600,       # 24h 内
            KEY_HEALTH_TS: time.time() - 7200,          # 1h 前到期
        }})
        service._on_tick()
        assert len(calls) == 1
        assert calls[0][2] is False and calls[0][3] is True

    def test_no_dispatch_when_neither_due(self, service, monkeypatch):
        calls = []
        self._catch_thread(monkeypatch, service, calls)
        service.sync_from_config({"c1": {
            KEY_AUTO_REFRESH: True,
            KEY_LAST_REFRESH: time.time(),
            KEY_HEALTH_TS: time.time(),
        }})
        service._on_tick()
        assert calls == []


class TestProbeHealth:
    def _post_stub(self, monkeypatch, responder):
        """monkeypatch requests.post（_probe_health 内延迟 import）"""
        import requests as _requests

        monkeypatch.setattr(_requests, "post", responder)

    def test_bearer_200_is_ok(self, service, monkeypatch):
        self._post_stub(monkeypatch, lambda url, **k: SimpleNamespace(status_code=200, text="{}"))
        status = service._probe_health(
            {"API_URL": "https://api.x.com/v1", "API_KEY": "k", "模型名称": "m1"}, "c1"
        )
        assert status == STATUS_OK

    def test_401_is_auth_failed(self, service, monkeypatch):
        self._post_stub(monkeypatch, lambda url, **k: SimpleNamespace(status_code=401, text=""))
        status = service._probe_health(
            {"API_URL": "https://api.x.com/v1", "API_KEY": "bad", "模型名称": "m1"}, "c1"
        )
        assert status == STATUS_AUTH_FAILED

    def test_no_v1_url_tries_both_paths(self, service, monkeypatch):
        """URL 不含 /v1 → 依次试 /chat/completions 与 /v1/chat/completions"""
        seen = []

        def responder(url, **k):
            seen.append(url)
            if url.endswith("/v1/chat/completions"):
                return SimpleNamespace(status_code=200, text="{}")
            return SimpleNamespace(status_code=404, text="")

        self._post_stub(monkeypatch, responder)
        status = service._probe_health(
            {"API_URL": "https://api.x.com", "API_KEY": "k", "模型名称": "m1"}, "c1"
        )
        assert status == STATUS_OK
        assert seen[0].endswith("/chat/completions") and seen[1].endswith("/v1/chat/completions")

    def test_400_max_completion_tokens_retry(self, service, monkeypatch):
        """400 提示 max_completion_tokens → 换字段重试一次"""
        bodies = []

        def responder(url, **k):
            bodies.append(k.get("json") or {})
            if "max_tokens" in bodies[-1]:
                return SimpleNamespace(status_code=400, text="use 'max_completion_tokens'")
            return SimpleNamespace(status_code=200, text="{}")

        self._post_stub(monkeypatch, responder)
        status = service._probe_health(
            {"API_URL": "https://api.x.com/v1", "API_KEY": "k", "模型名称": "o1"}, "c1"
        )
        assert status == STATUS_OK
        assert "max_completion_tokens" in bodies[-1]

    def test_anthropic_endpoint_and_headers(self, service, monkeypatch):
        captured = {}

        def responder(url, **k):
            captured["url"] = url
            captured["headers"] = k.get("headers") or {}
            return SimpleNamespace(status_code=200, text="{}")

        self._post_stub(monkeypatch, responder)
        status = service._probe_health(
            {"API_URL": "https://api.anthropic.com", "API_KEY": "k", "认证方式": "anthropic", "模型名称": "claude-x"},
            "c1",
        )
        assert status == STATUS_OK
        assert captured["url"].endswith("/v1/messages")
        assert captured["headers"]["x-api-key"] == "k"
        assert captured["headers"]["anthropic-version"] == "2023-06-01"

    def test_empty_model_name_is_unreachable(self, service, monkeypatch):
        self._post_stub(monkeypatch, lambda url, **k: SimpleNamespace(status_code=200, text="{}"))
        status = service._probe_health(
            {"API_URL": "https://api.x.com/v1", "API_KEY": "k", "模型名称": ""}, "c1"
        )
        assert status == STATUS_UNREACHABLE

    def test_no_key_bearer_is_unreachable(self, service, monkeypatch):
        status = service._probe_health(
            {"API_URL": "https://api.x.com/v1", "API_KEY": "", "模型名称": "m1"}, "c1"
        )
        assert status == STATUS_UNREACHABLE

    def test_auth_none_needs_no_key(self, service, monkeypatch):
        self._post_stub(monkeypatch, lambda url, **k: SimpleNamespace(status_code=200, text="{}"))
        status = service._probe_health(
            {"API_URL": "http://localhost:11434/v1", "API_KEY": "", "认证方式": "none", "模型名称": "m1"},
            "c1",
        )
        assert status == STATUS_OK

    def test_bce_without_hook_is_unreachable(self, service, monkeypatch):
        """bce 签名无法本地构造 → 不发请求直接 unreachable"""
        status = service._probe_health(
            {"API_URL": "https://aip.baidubce.com", "API_KEY": "k", "认证方式": "bce", "模型名称": "m1"},
            "c1",
        )
        assert status == STATUS_UNREACHABLE

    def test_hook_provider_degrades_to_list_probe(self, service, monkeypatch):
        """OAuth 插件类（models_hook）→ hook 探测（降级列表探测）"""
        from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry

        reg = ProviderRegistry()
        reg.register(
            ProviderDef(name="钩子P", capabilities={"models_hook": lambda cfg: ["m1"]}),
            source="plugin:test",
        )
        monkeypatch.setattr(ProviderRegistry, "_instance", reg)
        monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))
        status = service._probe_health(
            {"provider_name": "钩子P", "API_URL": "", "API_KEY": "", "模型名称": "m1"}, "c1"
        )
        assert status == STATUS_OK

    def test_health_probe_hook_preferred_over_everything(self, service, monkeypatch):
        """capabilities.health_probe 优先于 models_hook 与 REST：OAuth 类真探测入口

        models_hook 返回空列表（旧语义 → STATUS_EMPTY），health_probe 返回 ok：
        断言 ok 即证明走的是 health_probe 分支。
        """
        import requests as _requests

        from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry

        posts = []
        monkeypatch.setattr(_requests, "post", lambda url, **k: posts.append(url))

        reg = ProviderRegistry()
        reg.register(
            ProviderDef(
                name="探测P",
                capabilities={
                    "health_probe": lambda cfg: STATUS_OK,
                    "models_hook": lambda cfg: [],
                },
            ),
            source="plugin:test",
        )
        monkeypatch.setattr(ProviderRegistry, "_instance", reg)
        monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))
        status = service._probe_health(
            {"provider_name": "探测P", "API_URL": "https://x/v1", "API_KEY": "", "模型名称": "m1"},
            "c1",
        )
        assert status == STATUS_OK, "models_hook 空列表语义是 empty，得 ok 只能来自 health_probe"
        assert posts == [], "有 health_probe 时不该发 REST 请求"

    def test_health_probe_hook_invalid_value_is_unreachable(self, service, monkeypatch):
        """钩子返回非法状态值 → unreachable（不透传垃圾进状态键）"""
        from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry

        hp_calls = []
        reg = ProviderRegistry()
        reg.register(
            ProviderDef(
                name="坏值P",
                capabilities={
                    "health_probe": lambda cfg: (hp_calls.append(1), "什么玩意")[1],
                    "models_hook": lambda cfg: ["m1"],
                },
            ),
            source="plugin:test",
        )
        monkeypatch.setattr(ProviderRegistry, "_instance", reg)
        monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))
        status = service._probe_health({"provider_name": "坏值P"}, "c1")
        assert hp_calls == [1], "health_probe 必须被调用（优先于 models_hook）"
        assert status == STATUS_UNREACHABLE, "models_hook 语义是 ok，得 unreachable 证明走了 health_probe 且坏值被拦截"

    def test_health_probe_hook_exception_is_unreachable(self, service, monkeypatch):
        """钩子抛异常（约定内不应抛，仍兜底）→ unreachable"""
        from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry

        hp_calls = []

        def _boom(cfg):
            hp_calls.append(1)
            raise RuntimeError("probe exploded")

        reg = ProviderRegistry()
        reg.register(
            ProviderDef(
                name="炸P",
                capabilities={"health_probe": _boom, "models_hook": lambda cfg: ["m1"]},
            ),
            source="plugin:test",
        )
        monkeypatch.setattr(ProviderRegistry, "_instance", reg)
        monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))
        status = service._probe_health({"provider_name": "炸P"}, "c1")
        assert hp_calls == [1], "health_probe 必须被调用（优先于 models_hook）"
        assert status == STATUS_UNREACHABLE, "models_hook 语义是 ok，得 unreachable 证明走了 health_probe 且异常被兜底"

    def test_health_probe_hook_auth_failed_passthrough(self, service, monkeypatch):
        """钩子返回 auth_failed → 原样透传（OAuth 凭据废是最高价值信号）"""
        from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry

        reg = ProviderRegistry()
        reg.register(
            ProviderDef(name="凭据废P", capabilities={"health_probe": lambda cfg: STATUS_AUTH_FAILED}),
            source="plugin:test",
        )
        monkeypatch.setattr(ProviderRegistry, "_instance", reg)
        monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))
        status = service._probe_health({"provider_name": "凭据废P"}, "c1")
        assert status == STATUS_AUTH_FAILED


class TestReportResult:
    def test_report_writes_status_keys_only(self, service):
        """实时校准只写状态两键，不碰「上次模型刷新」与「模型列表」"""
        service.sync_from_config({"c1": {
            KEY_AUTO_REFRESH: False,
            KEY_HEALTH_CHECK: True,
            KEY_LAST_REFRESH: "2026-01-01 00:00:00",
            KEY_MODELS: ["m1"],
        }})
        service._on_health_reported("c1", STATUS_AUTH_FAILED)
        entry = service._store["providers"]["c1"]
        assert entry[KEY_REFRESH_STATUS] == STATUS_AUTH_FAILED
        assert entry[KEY_STATUS_TS]
        assert entry[KEY_LAST_REFRESH] == "2026-01-01 00:00:00", "列表计时不得被实时校准触碰"
        assert entry[KEY_MODELS] == ["m1"]

    def test_report_skips_write_when_status_unchanged(self, service, monkeypatch):
        """状态未变不写盘（多轮对话多次成功回写幂等，防写放大）"""
        service.sync_from_config({"c1": {KEY_HEALTH_CHECK: True, "模型刷新状态": "ok"}})
        writes = []
        real_set = service._fake_cfg.set

        def counting_set(item, value, save=True):
            writes.append(1)
            real_set(item, value, save=save)

        monkeypatch.setattr(service._fake_cfg, "set", counting_set)
        service._on_health_reported("c1", STATUS_OK)  # 已是 ok → 跳过
        assert writes == []
        service._on_health_reported("c1", STATUS_AUTH_FAILED)  # 翻转 → 写
        assert writes == [1]

    def test_report_unknown_config_id_silently_ignored(self, service):
        service._on_health_reported("ghost", STATUS_OK)  # 不抛异常即可


# ══════════════════════════════════════════════════════════════════
# 2/3/4/5. sync 注册集 + 落盘语义
# ══════════════════════════════════════════════════════════════════


@pytest.fixture()
def service(monkeypatch):
    """隔离服务实例 + 假 Settings（避免污染真实 app.config）"""
    svc = ModelRefreshService()
    monkeypatch.setattr(ModelRefreshService, "_instance", svc)
    # 不启动真实定时器（_ensure_timer 会 start QTimer，测试进程无事件循环也安全，
    # 但为纯净起见直接打桩）
    monkeypatch.setattr(svc, "_ensure_timer", lambda: None)

    store = {"providers": {}}

    class _FakeItem:
        """value 读写都走 store（服务与断言共享同一份数据）"""

        @property
        def value(self):
            return store["providers"]

        @value.setter
        def value(self, v):
            store["providers"] = v

    class _FakeCfg:
        def __init__(self):
            self._item = _FakeItem()

        @property
        def llm_saved_providers(self):
            return self._item

        def set(self, item, value, save=True):
            item.value = value

        def save(self):
            pass

    fake_cfg = _FakeCfg()
    store["providers"] = {}
    import app.utils.config as cfg_mod

    monkeypatch.setattr(cfg_mod.Settings, "get_instance", classmethod(lambda cls: fake_cfg))
    svc._fake_cfg = fake_cfg
    svc._store = store

    # sync_from_config 的入参在真实场景里就是 Settings 里的同一份数据；
    # 测试里显式再造一份容易与 store 脱节（_on_refresh_result 会因
    # `config_id not in saved` 直接 return）→ 包一层同步写入。
    _orig_sync = svc.sync_from_config

    def _sync(saved_providers=None):
        if saved_providers is not None:
            store["providers"] = deepcopy(saved_providers)
        return _orig_sync(saved_providers)

    svc.sync_from_config = _sync
    return svc


class TestSyncRegistration:
    def test_only_switched_on_entries_are_active(self, service):
        """双开关任一 True 才进 active；都 False 不进"""
        service.sync_from_config(
            {
                "c1": {KEY_AUTO_REFRESH: True},
                "c2": {KEY_HEALTH_CHECK: True},
                "c3": {KEY_AUTO_REFRESH: False, KEY_HEALTH_CHECK: False},
                "c4": {},
            }
        )
        assert service._active == {"c1", "c2"}

    def test_resync_drops_disabled_entries(self, service):
        service.sync_from_config({"c1": {KEY_AUTO_REFRESH: True}, "c2": {KEY_HEALTH_CHECK: True}})
        assert service._active == {"c1", "c2"}
        service.sync_from_config({"c1": {KEY_AUTO_REFRESH: False}})
        assert service._active == set(), "关掉开关后应移出 active"
        assert "c2" not in service._configs

    def test_invalidate_drops_snapshot(self, service):
        service.sync_from_config({"c1": {KEY_AUTO_REFRESH: True}})
        assert "c1" in service._configs
        service.invalidate("c1")
        assert "c1" not in service._configs
        assert "c1" not in service._active


class TestSettleSemantics:
    def test_auto_refresh_merges_models(self, service):
        """自动刷新 + 默认模型仍在 → 静默合并模型列表 + 写状态与时间戳"""
        info = {
            "provider_name": "P",
            KEY_AUTO_REFRESH: True,
            "模型名称": "m1",
            "模型列表": ["m1"],
        }
        service.sync_from_config({"c1": info})
        service._on_refresh_result("c1", STATUS_OK, STATUS_OK, ["m1", "m2"])

        entry = service._store["providers"]["c1"]
        assert entry[KEY_MODELS] == ["m1", "m2"], "应静默合并新列表"
        assert entry[KEY_REFRESH_STATUS] == STATUS_OK
        assert entry[KEY_LAST_REFRESH], "应写上次刷新时间"

    def test_health_check_does_not_touch_models(self, service):
        """健康检查独立语义：只写状态，不碰模型列表"""
        info = {
            "provider_name": "P",
            KEY_HEALTH_CHECK: True,  # 只开健康检查
            "模型名称": "m1",
            "模型列表": ["m1"],
        }
        service.sync_from_config({"c1": info})
        service._on_refresh_result("c1", STATUS_OK, "", [])

        entry = service._store["providers"]["c1"]
        assert entry[KEY_MODELS] == ["m1"], "健康检查不得修改模型列表"
        assert entry[KEY_REFRESH_STATUS] == STATUS_OK
        assert not entry.get(KEY_LAST_REFRESH), "纯健康探测不得写「上次模型刷新」"
        assert entry.get(KEY_HEALTH_TS), "应写健康计时键"

    def test_default_model_missing_does_not_merge(self, service):
        """默认模型不在新列表 → 不写列表（防配置指向已下线模型）"""
        info = {
            "provider_name": "P",
            KEY_AUTO_REFRESH: True,
            "模型名称": "gone-model",
            "模型列表": ["gone-model"],
        }
        service.sync_from_config({"c1": info})
        service._on_refresh_result("c1", STATUS_OK, STATUS_OK, ["other-1", "other-2"])

        entry = service._store["providers"]["c1"]
        assert entry[KEY_MODELS] == ["gone-model"], "默认模型失效时不得合并"
        assert entry[KEY_REFRESH_STATUS] == STATUS_OK, "状态仍要更新"

    def test_default_model_safe_helper(self):
        assert is_default_model_safe("m1", ["m1", "m2"]) is True
        assert is_default_model_safe("M1", ["m1"]) is True, "大小写不敏感"
        assert is_default_model_safe("gone", ["m1"]) is False
        assert is_default_model_safe("", ["m1"]) is False

    def test_unknown_config_id_ignored(self, service):
        """快照里没有的 config_id（已被 invalidate）→ 静默忽略，不新建条目"""
        service.sync_from_config({"c1": {KEY_AUTO_REFRESH: True}})
        service.invalidate("c1")
        service._store["providers"] = {}  # 模拟条目已被删除（配置里也没有）
        service._on_refresh_result("c1", STATUS_OK, STATUS_OK, ["m"])
        assert service._store["providers"] == {}, "不应凭空创建条目"


# ══════════════════════════════════════════════════════════════════
# 6/7/8/9. 抓取与状态判定
# ══════════════════════════════════════════════════════════════════


class TestFetchStatus:
    def _run_fetch(self, service, config_id, info):
        """跑 _fetch_one（列表通道）并把信号结果收到局部（绕过 Qt 队列，直连收集）"""
        got = {}
        service.refresh_finished.connect(
            lambda cid, sfinal, smodels, models: got.update(
                cid=cid, status=sfinal, status_models=smodels, models=models
            )
        )
        service._fetch_one(config_id, info, do_models=True)
        return got

    def test_rest_401_maps_to_auth_failed(self, service, monkeypatch):
        """REST 返回 401 → auth_failed（三元组解析）"""
        from app.widgets.cards.settings import provider_edit_card as pec

        monkeypatch.setattr(
            pec, "fetch_provider_models", lambda *a, **k: ([], [], STATUS_AUTH_FAILED)
        )
        info = {"provider_name": "P", "API_URL": "https://x/v1", "API_KEY": "bad"}
        got = self._run_fetch(service, "c1", info)
        assert got["status"] == STATUS_AUTH_FAILED
        assert got["status_models"] == STATUS_AUTH_FAILED

    def test_rest_timeout_maps_to_unreachable(self, service, monkeypatch):
        from app.widgets.cards.settings import provider_edit_card as pec

        monkeypatch.setattr(pec, "fetch_provider_models", lambda *a, **k: ([], [], STATUS_UNREACHABLE))
        info = {"provider_name": "P", "API_URL": "https://x/v1", "API_KEY": "k"}
        got = self._run_fetch(service, "c1", info)
        assert got["status"] == STATUS_UNREACHABLE
        assert got["status_models"] == STATUS_UNREACHABLE

    def test_rest_ok_returns_models(self, service, monkeypatch):
        from app.widgets.cards.settings import provider_edit_card as pec

        monkeypatch.setattr(pec, "fetch_provider_models", lambda *a, **k: (["m1", "m2"], [], STATUS_OK))
        info = {"provider_name": "P", "API_URL": "https://x/v1", "API_KEY": "k"}
        got = self._run_fetch(service, "c1", info)
        assert got["status"] == STATUS_OK
        assert got["models"] == ["m1", "m2"]

    def test_hook_path_preferred(self, service, monkeypatch):
        """有 models_hook 时走 hook，不走 REST"""
        from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry

        reg = ProviderRegistry()
        reg.register(
            ProviderDef(name="HookProvider", capabilities={"models_hook": lambda cfg: ["h1", "h2"]}),
            source="plugin:test",
        )
        monkeypatch.setattr(ProviderRegistry, "_instance", reg)
        monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))

        called = {"rest": False}
        from app.widgets.cards.settings import provider_edit_card as pec

        def _boom(*a, **k):
            called["rest"] = True
            return [], [], STATUS_UNREACHABLE

        monkeypatch.setattr(pec, "fetch_provider_models", _boom)

        info = {"provider_name": "HookProvider", "API_URL": "https://x/v1", "API_KEY": "k"}
        got = self._run_fetch(service, "c1", info)
        assert got["models"] == ["h1", "h2"]
        assert got["status"] == STATUS_OK
        assert called["rest"] is False, "有 hook 时不该走 REST"

    def test_hook_exception_maps_to_unreachable(self, service, monkeypatch):
        from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry

        def _raise(cfg):
            raise RuntimeError("boom")

        reg = ProviderRegistry()
        reg.register(ProviderDef(name="BadHook", capabilities={"models_hook": _raise}), source="plugin:test")
        monkeypatch.setattr(ProviderRegistry, "_instance", reg)
        monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))

        got = self._run_fetch(service, "c1", {"provider_name": "BadHook", "API_URL": "https://x", "API_KEY": "k"})
        assert got["status"] == STATUS_UNREACHABLE

    def test_missing_api_url_unreachable(self, service):
        got = self._run_fetch(service, "c1", {"provider_name": "P", "API_KEY": "k"})
        assert got["status"] == STATUS_UNREACHABLE


# ══════════════════════════════════════════════════════════════════
# 10. in_flight 去重
# ══════════════════════════════════════════════════════════════════


class TestInFlightDedup:
    def test_same_config_not_dispatched_twice(self, service, monkeypatch):
        """同一 config_id 已在抓取中 → tick 不再派新线程"""
        service.sync_from_config({"c1": {KEY_AUTO_REFRESH: True}})
        service._in_flight.add("c1")  # 模拟上一轮未归

        dispatched = []
        import threading as _threading

        real_thread = _threading.Thread

        class _SpyThread:
            def __init__(self, *a, **k):
                dispatched.append(k.get("name", ""))

            def start(self):
                dispatched.append("STARTED")

        monkeypatch.setattr(_threading, "Thread", _SpyThread)
        try:
            service._on_tick()
        finally:
            monkeypatch.setattr(_threading, "Thread", real_thread)

        assert dispatched == [], "in_flight 中的条目不得重复派发"

    def test_in_flight_cleared_after_fetch(self, service, monkeypatch):
        """抓取结束（含失败）必须清 in_flight，否则该条目永久不再刷新"""
        from app.widgets.cards.settings import provider_edit_card as pec

        monkeypatch.setattr(pec, "fetch_provider_models", lambda *a, **k: ([], [], STATUS_UNREACHABLE))
        service._in_flight.add("c1")
        service._fetch_one("c1", {"provider_name": "P", "API_URL": "https://x", "API_KEY": "k"})
        assert "c1" not in service._in_flight


# ══════════════════════════════════════════════════════════════════
# 线程红线：后台方法内不得直调 cfg.set / save
# ══════════════════════════════════════════════════════════════════


def test_fetch_one_has_no_config_writes():
    """⚠ 线程红线自查：_fetch_one（后台线程）内不得出现 cfg.set / cfg.save 直调"""
    import inspect
    import re

    src = inspect.getsource(ModelRefreshService._fetch_one)
    assert not re.search(r"cfg\.set\s*\(", src), "_fetch_one 是后台线程，禁碰 cfg.set"
    assert not re.search(r"cfg\.save\s*\(", src), "_fetch_one 是后台线程，禁碰 cfg.save"
    assert "QTimer" not in src, "_fetch_one 不得创建/启动 QTimer"

    # 落盘必须只在主线程槽里
    settle_src = inspect.getsource(ModelRefreshService._on_refresh_result)
    assert "cfg.set(" in settle_src, "落盘应在主线程槽 _on_refresh_result 内"

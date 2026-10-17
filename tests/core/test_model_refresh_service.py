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
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.modelmeta import model_refresh_service as mrs
from app.core.modelmeta.model_refresh_service import (
    KEY_AUTO_REFRESH,
    KEY_HEALTH_CHECK,
    KEY_LAST_REFRESH,
    KEY_MODELS,
    KEY_REFRESH_STATUS,
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
        service._on_refresh_result("c1", True, STATUS_OK, ["m1", "m2"])

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
        service._on_refresh_result("c1", True, STATUS_OK, ["m1", "m2", "m3"])

        entry = service._store["providers"]["c1"]
        assert entry[KEY_MODELS] == ["m1"], "健康检查不得修改模型列表"
        assert entry[KEY_REFRESH_STATUS] == STATUS_OK
        assert entry[KEY_LAST_REFRESH]

    def test_default_model_missing_does_not_merge(self, service):
        """默认模型不在新列表 → 不写列表（防配置指向已下线模型）"""
        info = {
            "provider_name": "P",
            KEY_AUTO_REFRESH: True,
            "模型名称": "gone-model",
            "模型列表": ["gone-model"],
        }
        service.sync_from_config({"c1": info})
        service._on_refresh_result("c1", True, STATUS_OK, ["other-1", "other-2"])

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
        service._on_refresh_result("c1", True, STATUS_OK, ["m"])
        assert service._store["providers"] == {}, "不应凭空创建条目"


# ══════════════════════════════════════════════════════════════════
# 6/7/8/9. 抓取与状态判定
# ══════════════════════════════════════════════════════════════════


class TestFetchStatus:
    def _run_fetch(self, service, config_id, info):
        """跑 _fetch_one 并把信号结果收到局部（绕过 Qt 队列，直连收集）"""
        got = {}
        service.refresh_finished.connect(
            lambda cid, ok, st, models: got.update(cid=cid, ok=ok, status=st, models=models)
        )
        service._fetch_one(config_id, info)
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
        assert got["ok"] is False

    def test_rest_timeout_maps_to_unreachable(self, service, monkeypatch):
        from app.widgets.cards.settings import provider_edit_card as pec

        monkeypatch.setattr(pec, "fetch_provider_models", lambda *a, **k: ([], [], STATUS_UNREACHABLE))
        info = {"provider_name": "P", "API_URL": "https://x/v1", "API_KEY": "k"}
        got = self._run_fetch(service, "c1", info)
        assert got["status"] == STATUS_UNREACHABLE
        assert got["ok"] is False

    def test_rest_ok_returns_models(self, service, monkeypatch):
        from app.widgets.cards.settings import provider_edit_card as pec

        monkeypatch.setattr(pec, "fetch_provider_models", lambda *a, **k: (["m1", "m2"], [], STATUS_OK))
        info = {"provider_name": "P", "API_URL": "https://x/v1", "API_KEY": "k"}
        got = self._run_fetch(service, "c1", info)
        assert got["ok"] is True
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
        assert got["ok"] is True
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
        assert got["ok"] is False

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

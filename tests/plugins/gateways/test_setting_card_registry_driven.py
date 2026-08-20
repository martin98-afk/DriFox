# -*- coding: utf-8 -*-
"""E2 Task 6 主仓：gateway 设置卡 registry 驱动 — 模块级三纯函数直测。

不启动 QWidget（纯函数测试）：
- _build_platform_defs_from_registry：registry 注册 fake def 后输出包含 fake；
- _save_platform_values：fake def.build_config_values 被调用且结果经
  GatewayConfigHelper.set_platform_config 落地；
- _save_platform_values：def 缺失 build_config_values 时返回 None。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import pytest


# ── helper：构造 fake GatewayPlatformDef ─────────────────────────
# 契约中 GatewayPlatformDef 为 frozen dataclass 且字段固定，直接构造。
# platform_id / display_name / adapter_factory 必填，其余走默认；本测试
# 关注字段：build_config_values / config_writer / icon_hint / ui_order。
def _make_fake_def(
    platform_id: str = "fakeplat",
    display_name: str = "Fake平台",
    build_config_values=None,
    icon_hint: str = "fakeplat",
    ui_order: int = 99,
):
    from app.plugins.contracts.gateway_platform import GatewayPlatformDef

    def _adapter(_cfg):
        return None

    return GatewayPlatformDef(
        platform_id=platform_id,
        display_name=display_name,
        adapter_factory=_adapter,
        build_config_values=build_config_values,
        icon_hint=icon_hint,
        ui_order=ui_order,
    )


@pytest.fixture
def fresh_registry():
    """每个用例独立的 fresh registry（避免污染全局单例）"""
    from app.plugins.registries.gateway_platform_registry import (
        GatewayPlatformRegistry,
    )

    # 全局单例在并发测试时共享；这里临时替换 get_instance 返回新实例。
    import app.plugins.registries.gateway_platform_registry as mod

    real_get_instance = mod.GatewayPlatformRegistry.get_instance
    new_instance = mod.GatewayPlatformRegistry()
    mod.GatewayPlatformRegistry.get_instance = staticmethod(lambda: new_instance)
    yield new_instance
    mod.GatewayPlatformRegistry.get_instance = real_get_instance


class TestBuildPlatformDefsFromRegistry:
    """_build_platform_defs_from_registry 输出包含已注册 fake def"""

    def test_includes_registered_fake_def(self, fresh_registry):
        from app.widgets.cards.settings.gateway_setting_card import (
            _build_platform_defs_from_registry,
        )

        def _build_values(values, old):
            return None

        fake = _make_fake_def(build_config_values=_build_values, icon_hint="fakeplat")
        fresh_registry.register(fake, source="test:fake")

        defs = _build_platform_defs_from_registry()

        assert "fakeplat" in defs, f"fake 未出现在 defs: {list(defs.keys())}"
        entry = defs["fakeplat"]
        # name / icon / fields / hint 四键与旧 PLATFORM_DEFS 形态一致
        assert entry["name"] == "Fake平台"
        assert entry["icon"] == "fakeplat"
        # fields 空（fake 无 config_schema 也无内置 fallback）
        assert entry["fields"] == []
        assert isinstance(entry["hint"], str)

    def test_fallback_fields_used_for_builtin(self, fresh_registry):
        """未声明 config_schema 的内置平台走 _FALLBACK_FIELDS"""
        # 直接注册一个 platform_id='wecom' 的 fake def（无 config_schema），
        # 验证 _build_platform_defs_from_registry 走 fallback。
        from app.widgets.cards.settings.gateway_setting_card import (
            _build_platform_defs_from_registry,
        )

        fake = _make_fake_def(platform_id="wecom", icon_hint="")
        fresh_registry.register(fake, source="test:wecom")

        defs = _build_platform_defs_from_registry()
        assert "wecom" in defs
        assert defs["wecom"]["fields"], "fields 应来自 _FALLBACK_FIELDS['wecom']"
        # fallback 元组三段：bot_id / secret / websocket_url
        keys = [f[0] for f in defs["wecom"]["fields"]]
        assert "bot_id" in keys and "secret" in keys and "websocket_url" in keys


class TestSavePlatformValues:
    """_save_platform_values 行为：def.build_config_values 被调，set_platform_config 落盘"""

    def test_dispatches_to_def_and_persists(self, fresh_registry, monkeypatch):
        from app.gateway.base import Platform
        from app.widgets.cards.settings.gateway_setting_card import (
            _save_platform_values,
        )

        # fake def 接收 values / old 返回特定 PlatformConfig
        sentinel = {"called_with": None}

        def _build_values(values, old):
            sentinel["called_with"] = (dict(values), old)
            return PlatformConfig(
                enabled=True,
                platform=Platform.WECOM,
                token=values.get("token", "tok-from-def"),
            )

        fake = _make_fake_def(build_config_values=_build_values, icon_hint="")
        fresh_registry.register(fake, source="test:save")

        # monkeypatch GatewayConfigHelper.set_platform_config，断言被调用且参数对得上
        from app.gateway import config as gw_config

        captured = {"args": None}

        def fake_set_platform_config(platform, cfg):
            captured["args"] = (platform, cfg)

        monkeypatch.setattr(
            gw_config.GatewayConfigHelper,
            "set_platform_config",
            staticmethod(fake_set_platform_config),
        )

        values = {"token": "abc123", "enabled": True}
        old = PlatformConfig(enabled=False, platform=Platform.WECOM, token="oldtok")
        result = _save_platform_values("fakeplat", values, old)

        # def 被调用，values / old 都正确传入
        assert sentinel["called_with"] is not None
        called_values, called_old = sentinel["called_with"]
        assert called_values["token"] == "abc123"
        assert called_old is old
        # config 落地：set_platform_config(platform_id, config)
        assert captured["args"] is not None
        platform_arg, cfg_arg = captured["args"]
        assert platform_arg == "fakeplat", "str 平台 id 直传，str-mixin 已兼容"
        assert cfg_arg.token == "abc123"
        # 函数返回值即写入的 config
        assert result is not None
        assert result.token == "abc123"

    def test_returns_none_when_def_missing(self, fresh_registry, monkeypatch):
        """registry 内 def 缺失时 _save_platform_values 返 None（UI 据此报提示）"""
        from app.widgets.cards.settings.gateway_setting_card import (
            _save_platform_values,
        )

        # 不向 fresh_registry 注册任何 def
        called = {"count": 0}

        def fake_set_platform_config(platform, cfg):
            called["count"] += 1

        from app.gateway import config as gw_config

        monkeypatch.setattr(
            gw_config.GatewayConfigHelper,
            "set_platform_config",
            staticmethod(fake_set_platform_config),
        )

        result = _save_platform_values("nonexistent", {"token": "x"}, None)
        assert result is None
        assert called["count"] == 0, "def 缺失时不应触发 set_platform_config"

    def test_returns_none_when_build_config_values_absent(self, fresh_registry, monkeypatch):
        """def 存在但 build_config_values=None 时返 None"""
        from app.widgets.cards.settings.gateway_setting_card import (
            _save_platform_values,
        )

        fake = _make_fake_def(build_config_values=None)
        fresh_registry.register(fake, source="test:save-noop")

        called = {"count": 0}

        def fake_set_platform_config(platform, cfg):
            called["count"] += 1

        from app.gateway import config as gw_config

        monkeypatch.setattr(
            gw_config.GatewayConfigHelper,
            "set_platform_config",
            staticmethod(fake_set_platform_config),
        )

        result = _save_platform_values("fakeplat", {"token": "x"}, None)
        assert result is None
        assert called["count"] == 0


# 末端：导入放在文件顶部会触发 gateway_setting_card 顶层 import GatewayPlatformDef；
# 真正的 PlatformConfig 用例里再延迟导入（避免 PyQt5 / Settings 副作用拉起整个 UI）。
from app.gateway.base import PlatformConfig  # noqa: E402
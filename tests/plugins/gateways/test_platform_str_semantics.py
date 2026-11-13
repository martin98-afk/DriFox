# -*- coding: utf-8 -*-
"""平台 id 语义验证：平台不设闭集，一律以 str 作 registry 键。

历史背景：``Platform(str, Enum)`` 曾把内置 7 平台写死，第三方 id 传给
``Platform(...)`` 直接 ValueError，且 ``str(Platform.WECOM)`` 得到
``"Platform.WECOM"`` 而非 ``"wecom"``。枚举删除后，宿主契约只剩
``MessageEvent.platform: str``，第三方平台与内置平台同构。
"""
from app.gateway.base import MessageEvent
from app.plugins.contracts.gateway_platform import GatewayPlatformDef


class TestPlatformIdSemantics:
    def test_message_event_platform_is_str(self):
        event = MessageEvent(platform="wecom")
        assert event.platform == "wecom"
        assert f"{event.platform}:chat" == "wecom:chat"

    def test_third_party_id_accepted(self):
        # 第三方平台 id 与内置平台无差别
        event = MessageEvent(platform="pt-awesome")
        assert event.platform == "pt-awesome"

    def test_registry_key_is_plain_str(self):
        # registry 以 platform_id 字符串为键：内置与第三方同构
        builtin = GatewayPlatformDef(platform_id="feishu", display_name="飞书", adapter_factory=lambda: None)
        third = GatewayPlatformDef(platform_id="teams", display_name="Teams", adapter_factory=lambda: None)
        assert builtin.platform_id == "feishu"
        assert third.platform_id == "teams"
        assert builtin.id == third.id.replace("teams", "feishu")

    def test_def_has_no_config_structure(self):
        """契约只留行为回调，不再承载任何配置字段。"""
        fields = set(GatewayPlatformDef.__dataclass_fields__)
        assert not fields & {"config_builder", "config_writer", "build_config_values", "validate_config"}
        assert {"is_enabled", "set_enabled", "validate", "adapter_factory"} <= fields

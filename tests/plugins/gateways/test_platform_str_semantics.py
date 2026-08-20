# -*- coding: utf-8 -*-
"""Platform str-mixin 语义验证：第三方平台 id 打通的前提。"""

from app.gateway.base import Platform


class TestStrMixinSemantics:
    def test_value_equality(self):
        assert Platform.WECOM == "wecom"
        assert "wecom" == Platform.WECOM
        assert Platform.TELEGRAM != "telegram "  # 无空格容错

    def test_value_attribute_unchanged(self):
        assert Platform.FEISHU.value == "feishu"

    def test_dict_key_interop(self):
        # manager._adapters 等内部 dict 的 key 混用安全前提
        d = {Platform.WECOM: 1}
        assert d["wecom"] == 1
        d2 = {"slack": 2}
        assert d2[Platform.SLACK] == 2

    def test_membership(self):
        assert Platform.DINGTALK in {"dingtalk"}
        assert "feishu" in {Platform.FEISHU}

    def test_unknown_id_still_plain_str(self):
        # 第三方平台 id 无枚举成员，消费方直接以 str 使用
        third_party = "teams"
        assert third_party not in {p.value for p in Platform}
        assert Platform.WECOM != third_party
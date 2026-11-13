# -*- coding: utf-8 -*-
"""GatewayService（应用级 Gateway 服务）单测。

不依赖 Qt 事件循环的部分：模型配置读取、组件构造、平台解析。
"""

import io
import sys

from system_im_service import GatewayService, _gw_str_platform


def test_gw_str_platform():

    # 平台入参一律归一成 str（平台 id 即 registry 键，不设闭集）
    assert _gw_str_platform("feishu") == "feishu"
    assert _gw_str_platform("pt-awesome") == "pt-awesome"


def test_model_config_reads_global_settings():
    """模型配置来自全局 Settings，不依赖任何窗口"""
    from app.utils.config import Settings

    cfg = Settings.get_instance()
    saved = cfg.llm_saved_providers.value or {}

    try:
        cfg.llm_saved_providers.value = {
            "prov_a": {"模型名称": "model-a", "API密钥": "k", "备注": "x", "模型列表": ["model-a"]}
        }
        cfg.llm_selected_model.value = "prov_a"

        config = GatewayService._get_model_config()
        assert config.get("模型名称") == "model-a"
        # 展示用字段应被剥离
        assert "备注" not in config
        assert "模型列表" not in config
    finally:
        cfg.llm_saved_providers.value = saved
        cfg.llm_selected_model.value = None


def test_model_config_empty_when_no_providers():
    from app.utils.config import Settings

    cfg = Settings.get_instance()
    saved = cfg.llm_saved_providers.value or {}
    try:
        cfg.llm_saved_providers.value = {}
        cfg.llm_selected_model.value = ""
        assert GatewayService._get_model_config() == {}
    finally:
        cfg.llm_saved_providers.value = saved
        cfg.llm_selected_model.value = None


def test_singleton():
    """同进程重复 get_instance 返回同一实例"""
    assert GatewayService.get_instance() is GatewayService.get_instance()


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"OK   {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)

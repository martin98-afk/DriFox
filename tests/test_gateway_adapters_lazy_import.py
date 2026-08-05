# -*- coding: utf-8 -*-
"""
回归测试：Gateway 适配器延迟导入

背景（2026-06-16）：
- bug: app/gateway/adapters/dingtalk.py 在模块顶层调用
  `_ensure_dingtalk_imports()` 和 `_patch_dingtalk_stream_logging()`，
  这两个调用都是无 try/except 的 eager import。
- 现象：环境里若未安装 dingtalk-stream（gateway extra 未装），
  `import app.gateway.adapters.dingtalk` 抛 ModuleNotFoundError，
  进而让 adapters 包、manager、config、整个 ChatBackend._init_gateway_async
  链路全部失败。
- 修复：
  1. dingtalk.py 改为真延迟导入（占位全局变量 + check_requirements 构建），
     `class _IncomingHandler` 改为动态构建以避免基类 None 时的 TypeError；
  2. adapters/__init__.py 每个平台用 importlib + try/except 包裹。

本测试锁住「缺包时模块仍可加载」这个不变量。

注意：
- 本测试**不依赖** dingtalk-stream 已安装——它在缺包环境下也应通过。
- 若环境里 dingtalk-stream 已安装，则 check_requirements() 应返回 True；
  此时测试通过 check_dingtalk_requirements() 来断言运行时仍可用。
"""
import importlib
import sys

import pytest


class TestDingTalkAdapterLazyImport:
    """钉钉适配器模块加载不应因 dingtalk-stream 缺失而失败"""

    def test_dingtalk_module_importable_without_sdk(self):
        """import app.gateway.adapters.dingtalk 不抛错（即使缺 dingtalk-stream）"""
        # 如果已加载，强制重新加载以确保走的是"裸"模块加载路径
        sys.modules.pop("app.gateway.adapters.dingtalk", None)
        module = importlib.import_module("app.gateway.adapters.dingtalk")
        assert module is not None

    def test_dingtalk_adapter_class_exposed(self):
        """DingTalkAdapter 类始终可访问（缺包时也应为类对象而非 None）"""
        from app.gateway.adapters.dingtalk import DingTalkAdapter
        assert DingTalkAdapter is not None
        assert isinstance(DingTalkAdapter, type)

    def test_check_requirements_returns_bool(self):
        """check_dingtalk_requirements() 必须返回 bool，不抛异常"""
        from app.gateway.adapters.dingtalk import check_dingtalk_requirements
        result = check_dingtalk_requirements()
        assert isinstance(result, bool)


class TestAdaptersPackageImport:
    """adapters 包加载不应被任何单一平台的依赖缺失影响"""

    def test_adapters_package_importable(self):
        """整个 adapters 包可加载"""
        # 强制重载以隔离缓存
        sys.modules.pop("app.gateway.adapters", None)
        pkg = importlib.import_module("app.gateway.adapters")
        assert pkg is not None

    def test_all_adapters_exposed_in_all(self):
        """__all__ 包含所有平台符号，即使缺包也不抛错"""
        from app.gateway import adapters
        expected = {
            "WeComAdapter", "check_wecom_requirements",
            "DingTalkAdapter", "check_dingtalk_requirements",
            "TelegramAdapter", "check_telegram_requirements",
            "DiscordAdapter", "check_discord_requirements",
            "FeishuAdapter", "check_feishu_requirements",
            "SlackAdapter",
        }
        assert expected.issubset(set(adapters.__all__))

    def test_try_import_helper_handles_import_error(self, monkeypatch):
        """纵深防御：_try_import 在模块 ImportError 时返回 None 列表而非抛错"""
        import importlib
        from app.gateway.adapters import _try_import

        def fake_import_module(name):
            if name == "app.gateway.adapters.dingtalk":
                raise ImportError("simulated: dingtalk-stream not installed")
            return importlib.import_module(name)

        monkeypatch.setattr(importlib, "import_module", fake_import_module)

        # 应返回 [None, None] 而不抛错
        adapter, check_fn = _try_import(
            "DingTalk", "app.gateway.adapters.dingtalk",
            ["DingTalkAdapter", "check_dingtalk_requirements"],
        )
        assert adapter is None
        assert check_fn is None


class TestGatewaySubpackageLoading:
    """整个 gateway 子包加载链路在缺包时也应工作"""

    def test_gateway_config_importable(self):
        """app.gateway.config 可加载（避免被 dingtalk 缺包牵连）"""
        sys.modules.pop("app.gateway.config", None)
        config = importlib.import_module("app.gateway.config")
        assert hasattr(config, "get_gateway_config")

    def test_gateway_manager_importable(self):
        """app.gateway.manager 可加载（避免被 dingtalk 缺包牵连）"""
        sys.modules.pop("app.gateway.manager", None)
        manager = importlib.import_module("app.gateway.manager")
        assert hasattr(manager, "create_platform_manager")


class TestDingTalkRuntimeLazyInit:
    """运行时调用 check_dingtalk_requirements() 不应引入模块顶层副作用"""

    def test_repeated_check_is_idempotent(self):
        """重复调用 check_dingtalk_requirements() 不会改变返回值"""
        from app.gateway.adapters.dingtalk import check_dingtalk_requirements
        first = check_dingtalk_requirements()
        second = check_dingtalk_requirements()
        third = check_dingtalk_requirements()
        assert first == second == third

    def test_placeholder_globals_are_consistent(self):
        """占位全局变量与 check_dingtalk_requirements() 状态一致"""
        from app.gateway.adapters import dingtalk as dt
        available = dt.check_dingtalk_requirements()
        if available:
            # 包已装：占位应被设置成真类
            assert dt.ChatbotHandler is not None
            assert dt.IncomingHandler is not None
        else:
            # 包未装：占位保持 None（不抛错）
            assert dt.ChatbotHandler is None
            assert dt.IncomingHandler is None


class TestDingTalkSendImageNoDeadCode:
    """
    锁住 review 报告（2026-06-16）Bug #2 的修复：

    send_image() 当前是降级实现：直接发文本消息显示文件名。
    历史上曾读取 image_data 文件后从未使用，造成死代码 + FileNotFoundError 误触发。
    本测试防止 image_data 死代码被重新引入。
    """

    def test_send_image_does_not_read_image_data(self):
        """send_image 源码不应包含 image_data 读取逻辑"""
        import inspect
        from app.gateway.adapters.dingtalk import DingTalkAdapter
        source = inspect.getsource(DingTalkAdapter.send_image)
        assert "image_data" not in source, (
            "send_image 不应再读 image_data——当前是降级为文本消息，"
            "读 image_data 是死代码（review Bug #2）"
        )
        # 同时锁住不再有 "with open(image_path" 模式（可能误读文件）
        assert "with open(image_path" not in source, (
            "send_image 不应再用 with open(image_path, ...) 读图片文件"
        )

    def test_send_image_degraded_text_payload(self):
        """send_image 降级为 text msgtype（msgtype == 'text'）"""
        import inspect
        from app.gateway.adapters.dingtalk import DingTalkAdapter
        source = inspect.getsource(DingTalkAdapter.send_image)
        # 降级实现的 contract：msgtype 必须是 text
        assert '"msgtype": "text"' in source or "'msgtype': 'text'" in source
        # 缩略图 emoji + filename 的展示格式
        assert "🖼️" in source
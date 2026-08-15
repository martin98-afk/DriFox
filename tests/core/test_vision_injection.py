# -*- coding: utf-8 -*-
"""
视觉注入端到端测试（_try_inject_vision_content）

覆盖（T25）：
1. 视觉模型注入：read 工具结果 image_data（协议 B）→ gpt-4o/minimax-m3 → data_uris 非空注入
2. 非视觉模型不注入：minimax-m2.5 → 返回 False、不注入（models.dev 权威数据）
3. 异常回退：provides_image_tools 抛异常 → 保守 frozenset({"screenshot","read"}) → 仍注入
4. 协议 A 路径：screenshot 结果 content 含 absolute_path → 本地图片路径提取注入
5. 非视觉提示：非视觉模型 + 视觉工具成功 → tool 消息追加"不支持视觉"提示

运行: python -m pytest tests/core/test_vision_injection.py -v
"""
import base64
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _make_vision_worker(model: str):
    """轻量构造 OpenAIChatWorker（__new__ 绕过 __init__，仅初始化视觉注入路径依赖）。"""
    from app.core.workers.chat_worker import OpenAIChatWorker

    w = OpenAIChatWorker.__new__(OpenAIChatWorker)
    w.llm_config = {"模型名称": model}
    w._current_session_messages = []
    w._api_messages_cache = None
    w._api_messages_built = False
    w._supports_vision = True
    w._append_to_api_cache = lambda msgs: None  # stub（不引入 messages_to_api 依赖）
    w._is_gemini_model = lambda: False
    w._requires_reasoning_content = lambda: False
    return w


def _read_result_with_image(mime="image/png", data="aGVsbG8="):
    """read 图片工具结果（协议 B：image_data 已编码）"""
    return {
        "name": "read",
        "success": True,
        "content": "[图片: x.png (1.0 KB, PNG)]",
        "image_data": {"mime": mime, "data": data},
    }


def _screenshot_result_with_path(path: str):
    """screenshot 工具结果（协议 A：content 含 absolute_path）"""
    return {
        "name": "screenshot",
        "success": True,
        "content": "截图完成",
        "raw_content": {"absolute_path": path},
    }


class TestVisionModelInjection:
    """T25-1：视觉模型注入（协议 B image_data）"""

    @pytest.mark.parametrize("model", ["gpt-4o", "minimax-m3"])
    def test_vision_model_injects_image_data(self, model):
        """支持视觉模型 → read image_data 注入（data_uris 非空、user 消息含 image_url）"""
        w = _make_vision_worker(model)
        current_messages = [{"role": "user", "content": "看图"}]
        tool_results = [_read_result_with_image()]

        ok = w._try_inject_vision_content(tool_results, current_messages, session_messages=[])
        assert ok, "视觉模型应注入图片"
        # 注入的 user 消息（multimodal list）追加到 current_messages 末尾
        vision_msg = current_messages[-1]
        assert vision_msg["role"] == "user"
        assert vision_msg.get("_hook_event") == "vision_inject"
        # content 为 list，含 image_url 块
        content = vision_msg["content"]
        assert isinstance(content, list)
        image_blocks = [b for b in content if b.get("type") == "image_url"]
        assert len(image_blocks) == 1
        assert image_blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")
        assert image_blocks[0]["image_url"]["url"].endswith("aGVsbG8=")

    def test_no_image_data_no_injection(self):
        """read 结果无 image_data 且无路径 → 不注入（返回 False）"""
        w = _make_vision_worker("gpt-4o")
        current_messages = [{"role": "user", "content": "hi"}]
        tool_results = [{"name": "read", "success": True, "content": "文本内容"}]

        ok = w._try_inject_vision_content(tool_results, current_messages, session_messages=[])
        assert not ok
        assert len(current_messages) == 1, "未注入时消息列表不变"


class TestNonVisionModel:
    """T25-2：非视觉模型不注入"""

    def test_non_vision_model_no_injection(self):
        """minimax-m2.5（supports_vision=False）→ 不注入"""
        w = _make_vision_worker("minimax-m2.5")
        current_messages = [{"role": "user", "content": "看图"}]
        tool_results = [_read_result_with_image()]

        ok = w._try_inject_vision_content(tool_results, current_messages, session_messages=[])
        assert not ok
        # 无 vision_inject 消息追加
        assert not any(m.get("_hook_event") == "vision_inject" for m in current_messages)


class TestRegistryExceptionFallback:
    """T25-3：provides_image_tools 抛异常 → 保守回退 frozenset({"screenshot","read"})"""

    def test_fallback_set_still_injects(self, monkeypatch):
        """registry 异常 → 保守回退生效 → read image_data 仍注入"""
        from app.core.workers import chat_worker as cw_mod

        def _boom():
            raise RuntimeError("registry down")

        monkeypatch.setattr(
            "app.tools.registry.ToolRegistry.provides_image_tools", _boom
        )
        # 同时保证 _try_inject_vision_content 内部从 registry 读取时也抛
        monkeypatch.setattr(
            "app.tools.registry.ToolRegistry.get_instance",
            lambda: type("FakeReg", (), {"provides_image_tools": _boom})(),
        )

        w = _make_vision_worker("gpt-4o")
        current_messages = [{"role": "user", "content": "看图"}]
        tool_results = [_read_result_with_image()]

        ok = w._try_inject_vision_content(tool_results, current_messages, session_messages=[])
        assert ok, "registry 异常应保守回退（screenshot/read）并继续注入"
        vision_msg = current_messages[-1]
        image_blocks = [b for b in vision_msg["content"] if b.get("type") == "image_url"]
        assert len(image_blocks) == 1

    def test_fallback_covers_screenshot_tool(self, monkeypatch):
        """保守回退集含 screenshot → screenshot 结果也可注入"""
        monkeypatch.setattr(
            "app.tools.registry.ToolRegistry.get_instance",
            lambda: type("FakeReg", (), {
                "provides_image_tools": lambda self: (_ for _ in ()).throw(RuntimeError("down"))
            })(),
        )

        w = _make_vision_worker("gpt-4o")
        current_messages = [{"role": "user", "content": "看图"}]
        # 构造临时图片文件供协议 A 提取
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(b"\x89PNG\r\n\x1a\nfake-png")
            tmp_path = tmp.name
        try:
            tool_results = [_screenshot_result_with_path(tmp_path)]
            ok = w._try_inject_vision_content(tool_results, current_messages, session_messages=[])
            assert ok, "回退集应覆盖 screenshot 工具"
        finally:
            os.unlink(tmp_path)


class TestProtocolAPathExtraction:
    """T25-4：协议 A（screenshot 本地路径提取）"""

    def test_screenshot_absolute_path_injected(self, tmp_path):
        """screenshot content 含 absolute_path → 读取文件注入 base64"""
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake-png-content")

        w = _make_vision_worker("gpt-4o")
        current_messages = [{"role": "user", "content": "看截图"}]
        tool_results = [_screenshot_result_with_path(str(img))]

        ok = w._try_inject_vision_content(tool_results, current_messages, session_messages=[])
        assert ok
        vision_msg = current_messages[-1]
        image_blocks = [b for b in vision_msg["content"] if b.get("type") == "image_url"]
        assert len(image_blocks) == 1
        # base64 内容为文件字节的编码
        url = image_blocks[0]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        expected = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-png-content").decode()
        assert url.endswith(expected)

    def test_path_from_raw_content(self, tmp_path):
        """raw_content.absolute_path 提取（screenshot 常见结构）"""
        img = tmp_path / "shot2.png"
        img.write_bytes(b"png-bytes-2")
        w = _make_vision_worker("gpt-4o")
        current_messages = [{"role": "user", "content": "x"}]
        tool_results = [{
            "name": "screenshot", "success": True,
            "content": "已截图",
            "raw_content": {"path": str(img)},  # path 字段也可提取
        }]
        ok = w._try_inject_vision_content(tool_results, current_messages, session_messages=[])
        assert ok


class TestNonVisionHint:
    """T25-5：非视觉模型 + 视觉工具成功 → 追加"不支持视觉"提示"""

    def test_non_vision_hint_appended(self):
        """minimax-m2.5 + screenshot 成功 → tool 消息追加不支持视觉提示"""
        w = _make_vision_worker("minimax-m2.5")
        tool_msg = {"role": "tool", "name": "screenshot", "content": "截图完成"}
        current_messages = [
            {"role": "user", "content": "截图给我看"},
            {"role": "assistant", "content": "好", "tool_calls": []},
            tool_msg,
        ]

        ok = w._try_inject_vision_content(tool_results=[{
            "name": "screenshot", "success": True, "content": "截图完成",
        }], current_messages=current_messages, session_messages=[])

        assert not ok
        assert "does not support vision" in tool_msg["content"], "应追加不支持视觉提示"
        assert "<system-reminder>" in tool_msg["content"]

    def test_non_vision_read_image_hint(self):
        """非视觉 + read 图片（image_data）→ 同样提示"""
        w = _make_vision_worker("minimax-m2.5")
        tool_msg = {"role": "tool", "name": "read", "content": "[图片: x.png]"}
        current_messages = [{"role": "user", "content": "读图"}, tool_msg]

        w._try_inject_vision_content(
            [_read_result_with_image()], current_messages, session_messages=[]
        )
        assert "does not support vision" in tool_msg["content"]

    def test_non_vision_hint_idempotent(self):
        """重复调用不重复追加提示"""
        w = _make_vision_worker("minimax-m2.5")
        tool_msg = {"role": "tool", "name": "screenshot", "content": "截图完成"}
        current_messages = [{"role": "user", "content": "x"}, tool_msg]
        tr = [{"name": "screenshot", "success": True, "content": "截图完成"}]

        w._try_inject_vision_content(tr, current_messages, session_messages=[])
        w._try_inject_vision_content(tr, current_messages, session_messages=[])
        assert tool_msg["content"].count("does not support vision") == 1

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


@pytest.fixture(autouse=True)
def _stub_tool_registry(monkeypatch):
    """测试环境不加载插件，真实 registry 里没有 read → provides_image_tools 为空。

    stub 成含 read 的集合，保证本文件单独跑与全量跑行为一致。
    （异常回退用例内部会再覆盖此 stub。）
    """
    monkeypatch.setattr(
        "app.tools.registry.ToolRegistry.get_instance",
        lambda: type("FakeReg", (), {
            "provides_image_tools": lambda self: frozenset({"read"})
        })(),
    )


def _make_vision_worker(model: str):
    """轻量构造 OpenAIChatWorker（__new__ 绕过 __init__，仅初始化视觉注入路径依赖）。"""
    from app.core.workers.chat_worker import OpenAIChatWorker

    w = OpenAIChatWorker.__new__(OpenAIChatWorker)
    w.llm_config = {"模型名称": model}
    w._current_session_messages = []
    w._api_messages_cache = None
    w._api_messages_built = False
    # P1-9 批 e：_supports_vision 手工属性已随实现退役（视觉判定改走 resolve 链）
    w._append_to_api_cache = lambda msgs: None  # stub（不引入 messages_to_api 依赖）
    w._is_gemini_model = lambda: False
    w._requires_reasoning_content = lambda: False
    # __new__ 绕过 __init__ 后 QObject.__getattr__ 对缺失属性抛 RuntimeError，
    # getattr 默认值失效 → 显式补 persist/注入路径读取的实例属性
    w._current_session_id = None
    w.session_id = None
    return w


def _read_result_with_image(mime="image/png", data="aGVsbG8="):
    """read 图片工具结果（协议 B：image_data 已编码）"""
    return {
        "name": "read",
        "success": True,
        "content": "[图片: x.png (1.0 KB, PNG)]",
        "image_data": {"mime": mime, "data": data},
    }


def _read_result_with_path(path: str):
    """read 工具结果（协议 A：content 含 absolute_path）"""
    return {
        "name": "read",
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
            tool_results = [_read_result_with_path(tmp_path)]
            ok = w._try_inject_vision_content(tool_results, current_messages, session_messages=[])
            assert ok, "回退集应覆盖 screenshot 工具"
        finally:
            os.unlink(tmp_path)


class TestProtocolAPathExtraction:
    """T25-4：协议 A（screenshot 本地路径提取）"""

    def test_absolute_path_injected(self, tmp_path):
        """read content 含 absolute_path → 读取文件注入 base64（协议 A）"""
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake-png-content")

        w = _make_vision_worker("gpt-4o")
        current_messages = [{"role": "user", "content": "看截图"}]
        tool_results = [_read_result_with_path(str(img))]

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
        """raw_content.absolute_path 提取（视觉工具常见结构）"""
        img = tmp_path / "shot2.png"
        img.write_bytes(b"png-bytes-2")
        w = _make_vision_worker("gpt-4o")
        current_messages = [{"role": "user", "content": "x"}]
        tool_results = [{
            "name": "read", "success": True,
            "content": "已截图",
            "raw_content": {"path": str(img)},  # path 字段也可提取
        }]
        ok = w._try_inject_vision_content(tool_results, current_messages, session_messages=[])
        assert ok


class TestNonVisionHint:
    """T25-5：非视觉模型 + 视觉工具成功 → 追加"不支持视觉"提示"""

    def test_non_vision_text_result_no_hint(self):
        """非视觉 + read 普通文本（无图像载荷）→ 不追加不支持视觉提示"""
        w = _make_vision_worker("minimax-m2.5")
        tool_msg = {"role": "tool", "name": "read", "content": "普通文本内容"}
        current_messages = [
            {"role": "user", "content": "读下这个文件"},
            {"role": "assistant", "content": "好", "tool_calls": []},
            tool_msg,
        ]

        ok = w._try_inject_vision_content(tool_results=[{
            "name": "read", "success": True, "content": "普通文本内容",
        }], current_messages=current_messages, session_messages=[])

        assert not ok
        assert "does not support vision" not in tool_msg["content"], "无图像载荷不应追加提示"

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
        """重复调用不重复追加提示（image_data 结果）"""
        w = _make_vision_worker("minimax-m2.5")
        tool_msg = {"role": "tool", "name": "read", "content": "[图片: x.png]"}
        current_messages = [{"role": "user", "content": "x"}, tool_msg]
        tr = [_read_result_with_image()]

        w._try_inject_vision_content(tr, current_messages, session_messages=[])
        w._try_inject_vision_content(tr, current_messages, session_messages=[])
        assert tool_msg["content"].count("does not support vision") == 1


class TestVisionNoticeSuppression:
    """read 文本结果不应追加 Vision Notice（_build_result_dict 注入判定）"""

    @staticmethod
    def _make_worker(model: str):
        from app.core.workers.chat_worker import OpenAIChatWorker

        w = OpenAIChatWorker.__new__(OpenAIChatWorker)
        w.llm_config = {"模型名称": model}
        w._take_tool_phases = lambda call_id: {}
        return w

    @staticmethod
    def _fake_registry(monkeypatch):
        monkeypatch.setattr(
            "app.tools.registry.ToolRegistry.get_instance",
            lambda: type("FakeReg", (), {
                "provides_image_tools": lambda self: frozenset({"read"})
            })(),
        )

    @staticmethod
    def _set_caps(monkeypatch, supports_vision: bool):
        import app.core.modelmeta.model_capabilities as mc

        # P1-9 批 d 后视觉判定走 resolve_model_capabilities（内部两参调 caps）
        monkeypatch.setattr(
            mc,
            "resolve_model_capabilities",
            lambda llm_config, provider_name="", sources=None: {
                "supports_vision": supports_vision
            },
        )

    def _build(self, monkeypatch, result_obj, model="gpt-4o", vision=True):
        self._fake_registry(monkeypatch)
        self._set_caps(monkeypatch, vision)
        w = self._make_worker(model)
        return w._build_result_dict("call-1", "read", {"path": "x.txt"}, str(result_obj), True, 1, result_obj)

    def test_read_text_no_vision_notice(self, monkeypatch):
        """视觉模型 + read 文本（无图像载荷）→ 不追加 Vision Notice"""
        from app.tools.result import ToolResult

        result = self._build(monkeypatch, ToolResult(True, content="#File: x.txt\nhello"))
        assert "Vision Notice" not in result["content"]
        assert "does not support vision" not in result["content"]

    def test_read_image_has_vision_notice(self, monkeypatch):
        """视觉模型 + read 图片（image_data 协议 B）→ 追加 Vision Notice"""
        from app.tools.result import ToolResult

        obj = ToolResult(True, content="[图片: a.png]", image_data={"mime": "image/png", "data": "aGk="})
        result = self._build(monkeypatch, obj)
        assert "Vision Notice" in result["content"]

    def test_protocol_a_path_has_vision_notice(self, monkeypatch):
        """视觉模型 + 协议 A（content dict 带 absolute_path）→ 追加 Vision Notice"""
        from app.tools.result import ToolResult

        obj = ToolResult(True, content={"absolute_path": "C:/tmp/shot.png"})
        result = self._build(monkeypatch, obj)
        assert "Vision Notice" in result["content"]

    def test_non_vision_model_image_notice(self, monkeypatch):
        """非视觉模型 + read 图片 → 追加不支持视觉提示（而非 Vision Notice）"""
        from app.tools.result import ToolResult

        obj = ToolResult(True, content="[图片: a.png]", image_data={"mime": "image/png", "data": "aGk="})
        result = self._build(monkeypatch, obj, model="minimax-m2.5", vision=False)
        assert "Vision Notice" not in result["content"]
        assert "不支持视觉" in result["content"]

    def test_result_obj_none_no_crash(self, monkeypatch):
        """result_obj 为 None（异常路径）→ 不追加提示且不崩溃"""
        self._fake_registry(monkeypatch)
        self._set_caps(monkeypatch, True)
        w = self._make_worker("gpt-4o")
        result = w._build_result_dict("call-1", "read", {}, "Tool execution error: x", False, 1, None)
        assert "Vision Notice" not in result["content"]


class TestMcpImageInjection:
    """MCP 工具（不在 provides_image 白名单）携带 image_data 也应注入。"""

    def _mcp_result(self, count=2):
        return {
            "name": "mcp__bili__bili_frames",
            "success": True,
            "content": "2 帧已取得",
            "image_data": [
                {"mime": "image/jpeg", "data": "aGk="},
                {"mime": "image/jpeg", "data": "eA=="},
            ][:count],
        }

    def test_mcp_tool_with_image_list_injects_all(self):
        w = _make_vision_worker("gpt-4o")
        current_messages = [{"role": "user", "content": "看画面"}]
        ok = w._try_inject_vision_content([self._mcp_result()], current_messages, session_messages=[])
        assert ok
        blocks = [b for b in current_messages[-1]["content"] if b.get("type") == "image_url"]
        assert len(blocks) == 2
        assert blocks[0]["image_url"]["url"] == "data:image/jpeg;base64,aGk="
        assert blocks[1]["image_url"]["url"] == "data:image/jpeg;base64,eA=="

    def test_mcp_non_vision_gets_hint(self):
        w = _make_vision_worker("minimax-m2.5")
        # 名称必须与 tool_results 里的 name 一致：提示按工具名回写到 tool 消息
        tool_msg = {"role": "tool", "name": "mcp__bili__bili_frames", "content": "封面已取"}
        current_messages = [{"role": "user", "content": "看封面"}, tool_msg]
        w._try_inject_vision_content([self._mcp_result(1)], current_messages, session_messages=[])
        assert "does not support vision" in tool_msg["content"]

    def test_protocol_a_still_requires_whitelist(self):
        """普通工具 content 里出现图片路径，不得被误读成本地图注入。"""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(b"\x89PNG\r\n\x1a\nfake")
            path = tmp.name
        try:
            w = _make_vision_worker("gpt-4o")
            current_messages = [{"role": "user", "content": "x"}]
            result = {
                "name": "mcp__other__fetch",  # 不在白名单
                "success": True,
                "content": f"路径：{path}",
            }
            ok = w._try_inject_vision_content([result], current_messages, session_messages=[])
            assert not ok
        finally:
            os.unlink(path)

    def test_vision_notice_for_mcp_image(self, monkeypatch):
        """_build_result_dict 对非白名单 MCP 工具的图片结果也追加 Vision Notice。"""
        from app.tools.result import ToolResult

        from app.core.workers.chat_worker import OpenAIChatWorker

        w = OpenAIChatWorker.__new__(OpenAIChatWorker)
        w.llm_config = {"模型名称": "gpt-4o"}
        w._take_tool_phases = lambda call_id: {}
        monkeypatch.setattr(
            "app.tools.registry.ToolRegistry.get_instance",
            lambda: type("FakeReg", (), {"provides_image_tools": lambda self: frozenset({"read"})})(),
        )
        import app.core.modelmeta.model_capabilities as mc

        monkeypatch.setattr(mc, "resolve_model_capabilities",
                            lambda cfg, provider_name="", sources=None: {"supports_vision": True})
        obj = ToolResult(True, content="封面", image_data={"mime": "image/jpeg", "data": "aGk="})
        result = w._build_result_dict("c1", "mcp__bili__bili_cover", {}, "封面", True, 1, obj)
        assert "Vision Notice" in result["content"]


class TestVisionTotalBudget:
    """多图总量超 8MB 时压缩后丢末尾，文本写明省略数。"""

    def test_budget_drops_tail_and_annotates(self, monkeypatch):
        import app.core.workers.chat_worker as cw

        # 压缩体返回原 URI（模拟压不动），触发丢末尾分支
        monkeypatch.setattr(cw, "compress_data_uri", lambda du: du)
        big = "A" * (5 * 1024 * 1024)  # 单张 5MB base64
        w = _make_vision_worker("gpt-4o")
        current_messages = [{"role": "user", "content": "看画面"}]
        tool_results = [{
            "name": "mcp__bili__bili_frames", "success": True, "content": "3 帧",
            "image_data": [{"mime": "image/jpeg", "data": big} for _ in range(3)],
        }]
        ok = w._try_inject_vision_content(tool_results, current_messages, session_messages=[])
        assert ok
        content = current_messages[-1]["content"]
        blocks = [b for b in content if b.get("type") == "image_url"]
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        assert len(blocks) == 1  # 8MB 预算只容 1 张 5MB
        assert "已省略 2 张" in text


class TestVisionPersistToImageRef:
    """T8：vision 注入先落盘再挂 image_ref（消除 data URI 在 API 缓存副本滞留）"""

    def test_vision_inject_persists_to_image_ref(self, monkeypatch, tmp_path):
        """注入后 content 块为 image_ref，磁盘出现 <sha1[:16]>.png，UI 副本同步"""
        import base64
        import hashlib

        monkeypatch.setattr("app.utils.app_paths.get_app_data_dir", lambda: tmp_path)
        w = _make_vision_worker("gpt-4o")
        w.session_id = "sess_t8"
        raw = b"\x89PNG" + b"\x00" * 256
        b64 = base64.b64encode(raw).decode()
        current_messages = [{"role": "user", "content": "看图"}]
        session_messages: list = []
        tool_results = [_read_result_with_image(data=b64)]
        ok = w._try_inject_vision_content(tool_results, current_messages, session_messages=session_messages)
        assert ok
        digest = hashlib.sha1(raw).hexdigest()[:16]
        # API 缓存消息（current_messages）：image_ref 引用而非 data URI
        vision_msg = current_messages[-1]
        blocks = [b for b in vision_msg["content"] if b.get("type") == "image_ref"]
        assert len(blocks) == 1, f"期望 image_ref 块，实际: {vision_msg['content']}"
        ref = blocks[0]["image_ref"]
        assert ref["file"] == f"{digest}.png"
        assert ref["session_id"] == "sess_t8"
        # 磁盘：sha1[:16].png 落在 persisted/<sid>/
        fpath = tmp_path / "screenshots" / "persisted" / "sess_t8" / f"{digest}.png"
        assert fpath.exists(), f"落盘缺失: {fpath}"
        assert fpath.read_bytes() == raw
        # session_messages（UI 副本）与 API 副本共享 content list，同样见 image_ref
        ui_blocks = [b for b in session_messages[-1]["content"] if b.get("type") == "image_ref"]
        assert len(ui_blocks) == 1
        assert not any(
            b.get("type") == "image_url" and str(b.get("image_url", {}).get("url", "")).startswith("data:")
            for b in vision_msg["content"]
        ), "API 缓存副本不应残留 data URI"

    def test_vision_inject_persist_failure_fallback(self, monkeypatch, tmp_path):
        """persist 抛异常 → 降级保留 image_url 块，返回 True 不抛"""
        monkeypatch.setattr("app.utils.app_paths.get_app_data_dir", lambda: tmp_path)

        def _boom(*_a, **_k):
            raise RuntimeError("disk full")

        monkeypatch.setattr("app.core.store.session_repository._persist_vision_image_blocks_impl", _boom)
        w = _make_vision_worker("gpt-4o")
        w.session_id = "sess_t8f"
        raw = b"\x89PNG" + b"\x01" * 64
        b64 = base64.b64encode(raw).decode()
        current_messages = [{"role": "user", "content": "看图"}]
        tool_results = [_read_result_with_image(data=b64)]
        ok = w._try_inject_vision_content(tool_results, current_messages, session_messages=[])
        assert ok, "persist 失败应降级不阻断"
        blocks = [b for b in current_messages[-1]["content"] if b.get("type") == "image_url"]
        assert len(blocks) == 1
        assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_budget_omitted_images_not_persisted(self, monkeypatch, tmp_path):
        """T17 minor-3：8MB 预算省略的图不落盘（守护「预算判定先于 persist」时序契约）"""
        monkeypatch.setattr("app.utils.app_paths.get_app_data_dir", lambda: tmp_path)
        import app.core.workers.chat_worker as cw
        # 压缩恒等（模拟压不动），132B + 5MB 虽 <8MB 预算不省略；改用两张 5MB
        monkeypatch.setattr(cw, "compress_data_uri", lambda du: du)
        w = _make_vision_worker("gpt-4o")
        w.session_id = "sess_t17"
        raw = b"\x89PNG" + b"\x02" * 128
        b64 = base64.b64encode(raw).decode()
        # 两张 5MB base64 = 10MB > 8MB 预算 → pop 末尾 1 张
        current_messages = [{"role": "user", "content": "看两张图"}]
        tool_results = [{
            "name": "read", "success": True, "content": "2 图",
            "image_data": [
                {"mime": "image/png", "data": "A" * (5 * 1024 * 1024)},
                {"mime": "image/png", "data": "B" * (5 * 1024 * 1024)},
            ],
        }]
        ok = w._try_inject_vision_content(tool_results, current_messages, session_messages=[])
        assert ok
        persisted_dir = tmp_path / "screenshots" / "persisted" / "sess_t17"
        files = list(persisted_dir.glob("*")) if persisted_dir.exists() else []
        assert len(files) == 1, f"仅未省略那张应落盘，实际 {files}"
        import hashlib
        kept_raw = base64.b64decode("A" * (5 * 1024 * 1024))
        digest = hashlib.sha1(kept_raw).hexdigest()[:16]
        assert files[0].name == f"{digest}.png", "落盘的应是保留的第一张（非省略图）"
        content = current_messages[-1]["content"]
        assert sum(1 for b in content if b.get("type") == "image_ref") == 1
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        assert "已省略 1 张" in text

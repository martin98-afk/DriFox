# -*- coding: utf-8 -*-
"""gateway-feishu CardKit 流式 + 宿主新命令逻辑单测（不依赖飞书网络）。"""

import asyncio
import io
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PLUGIN_DIR = Path(r"D:\work\DriFox\.drifox\plugins\gateway-feishu\gateways")


def load_adapter_cls():
    import importlib.util
    import enum
    from dataclasses import dataclass, field
    from datetime import datetime
    from typing import Any, Dict, List, Optional

    # 最小宿主依赖 stub（真类，供继承/实例化）
    import types

    base_mod = types.ModuleType("app.gateway.base")

    class Platform(enum.Enum):
        FEISHU = "feishu"

    class MessageType(enum.Enum):
        TEXT = "text"
        FILE = "file"
        COMMAND = "command"

    @dataclass
    class MessageEvent:
        text: str = ""
        message_type: MessageType = MessageType.TEXT
        message_id: str = ""
        chat_id: str = ""
        user_id: str = ""
        user_name: str = ""
        platform: Platform = Platform.FEISHU
        chat_type: str = "dm"
        media_urls: List[str] = field(default_factory=list)
        media_types: List[str] = field(default_factory=list)
        timestamp: datetime = field(default_factory=datetime.now)
        metadata: Dict[str, Any] = field(default_factory=dict)

    @dataclass
    class SendResult:
        success: bool
        message_id: Optional[str] = None
        error: Optional[str] = None
        retryable: bool = False

    @dataclass
    class PlatformConfig:
        enabled: bool = False
        platform: Platform = Platform.FEISHU
        extra: Dict[str, Any] = field(default_factory=dict)

    class BasePlatformAdapter:
        def __init__(self, config, platform):
            self.config = config
            self.platform = platform

    for n in ("Platform", "MessageType", "MessageEvent", "SendResult", "PlatformConfig", "BasePlatformAdapter"):
        setattr(base_mod, n, locals()[n])

    sys.modules.setdefault("app", types.ModuleType("app"))
    sys.modules.setdefault("app.gateway", types.ModuleType("app.gateway"))
    sys.modules["app.gateway.base"] = base_mod
    loguru_mod = types.ModuleType("loguru")
    loguru_mod.logger = MagicMock()
    sys.modules["loguru"] = loguru_mod

    spec = importlib.util.spec_from_file_location("feishu_mod", PLUGIN_DIR / "feishu.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.FeishuAdapter


def make_adapter():
    cls = load_adapter_cls()
    cfg = MagicMock()
    cfg.extra = {"app_id": "x", "app_secret": "y"}
    adapter = cls.__new__(cls)  # 跳过 BasePlatformAdapter.__init__
    adapter._stream_states = {}
    adapter._token = "tok"
    adapter._token_expiry = float("inf")
    adapter._http_client = None
    return adapter


def test_supports_streaming():
    a = make_adapter()
    assert a.supports_streaming("c1") is True


def test_start_stream_happy_path():
    a = make_adapter()
    calls = []

    async def fake_req(method, endpoint, payload):
        calls.append((method, endpoint.split("?")[0], payload))
        if endpoint.startswith("cardkit/v1/cards") and method == "POST":
            return {"code": 0, "data": {"card_id": "card123"}}
        return {"code": 0}

    a._api_request = fake_req
    key = asyncio.run(a.start_stream("c1"))
    assert key == "cardkit:card123", key
    assert a._stream_states["c1"]["sequence"] == 1  # 开流消耗 1 次
    assert calls[0][1] == "cardkit/v1/cards"
    assert calls[1][1] == "im/v1/messages"
    sent = calls[1][2]
    assert sent["msg_type"] == "interactive"
    assert json.loads(sent["content"]) == {"type": "card", "data": {"card_id": "card123"}}
    assert calls[2][2]["settings"] == {"streaming_mode": True}


def test_start_stream_failure_returns_none():
    a = make_adapter()

    async def fake_req(method, endpoint, payload):
        raise RuntimeError("perm denied")

    a._api_request = fake_req
    assert asyncio.run(a.start_stream("c1")) is None
    assert "c1" not in a._stream_states


def test_update_and_finish_stream():
    a = make_adapter()
    a._stream_states["c1"] = {"card_id": "card123", "sequence": 1}
    bodies = []

    async def fake_req(method, endpoint, payload):
        bodies.append((endpoint, payload))
        return {"code": 0}

    a._api_request = fake_req
    assert asyncio.run(a.update_stream("c1", "部分内容")) is True
    assert bodies[-1][0].endswith("/elements/stream_md/content")
    assert bodies[-1][1]["content"] == "部分内容"
    assert bodies[-1][1]["sequence"] == 2

    result = asyncio.run(a.finish_stream("c1", "# 标题\n最终全文"))
    assert result.success is True
    assert bodies[-2][1]["content"].startswith("# 标题")
    assert bodies[-1][0].endswith("/settings")
    assert bodies[-1][1]["settings"] == {"streaming_mode": False}
    assert bodies[-1][1]["sequence"] == 4
    assert "c1" not in a._stream_states  # 状态已清理


def test_update_stream_circuit_breaker():
    a = make_adapter()
    a._stream_states["c1"] = {"card_id": "card123", "sequence": 0, "failed": True}
    assert asyncio.run(a.update_stream("c1", "x")) is False


def test_finish_stream_unknown_key():
    a = make_adapter()
    r = asyncio.run(a.finish_stream("nope", "x"))
    assert r.success is False


def test_sequence_strictly_increasing():
    a = make_adapter()
    seqs = []

    async def fake_req(method, endpoint, payload):
        if "sequence" in payload:
            seqs.append(payload.get("sequence"))
        return {"code": 0, "data": {"card_id": "c"}} if method == "POST" else {"code": 0}

    a._api_request = fake_req
    asyncio.run(a.start_stream("c1"))
    for i in range(3):
        asyncio.run(a.update_stream("c1", f"s{i}"))
    asyncio.run(a.finish_stream("c1", "done"))
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), seqs


if __name__ == "__main__":
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

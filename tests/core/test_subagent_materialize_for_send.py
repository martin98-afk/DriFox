# -*- coding: utf-8 -*-
"""子代理发送前物化（回归：含图请求 400/丢图）

## 背景
subagent 出口回收（_serialize_for_api revoke_after=True）把 api_messages 的
图片块还原为 image_ref；_make_api_call 此前直接把该产物传给 transport：
- chat 路径：openai_chat 原样外发 type=image_ref（OpenAI 协议不识别）→ 400
- responses 路径：transport 从内部格式 messages 重新序列化，不识别 image_ref → 丢图

## 修复
_make_api_call 在序列化后、传 transport 前做浅拷贝骨架 + revive 物化
（对称 chat_worker._materialize_for_send），messages 与 api_messages 两路
统一改传物化副本。

## 断言
1. 发送体图片块必须是 image_url（data URI），image_ref 不外泄
2. 已回收的原 messages 不被物化二次污染（图块保持 image_ref）
3. persist 文件缺失时降级 text 占位块，不抛错、不外泄 image_ref
"""

import base64
import copy
import hashlib
from unittest.mock import MagicMock

import pytest

from app.core.workers.subagent_worker import SubAgentExecutor

_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# persist 文件名必须是 digest 命名（revoke 按 sha1(raw)[:16] 反推文件名做特征匹配）
_PERSIST_FNAME = hashlib.sha1(_PNG_1PX).hexdigest()[:16] + ".png"

_IMAGE_REF_BLOCK = {
    "type": "image_ref",
    "image_ref": {"file": _PERSIST_FNAME, "session_id": "sid_mat", "meta": "image/png"},
}

_MESSAGES_WITH_IMAGE = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": [{"type": "text", "text": "看这张图"}, dict(_IMAGE_REF_BLOCK)]},
    {"role": "user", "content": "描述一下"},
]


def _fresh_messages() -> list:
    """深拷贝模板：revive/revoke 会就地改写 content 列表，测试间不得共享"""
    return copy.deepcopy(_MESSAGES_WITH_IMAGE)


def _build_executor() -> SubAgentExecutor:
    """最小构造：不 start 线程，只调 _make_api_call"""
    return SubAgentExecutor(
        task_id="t_materialize",
        agent_name="worker",
        task_description="test",
        llm_config={"API_KEY": "sk-test", "模型名称": "gpt-4o", "API_URL": "https://api.openai.com/v1"},
        agent_manager=None,
        tool_executor=None,
    )


@pytest.fixture()
def capture_client(monkeypatch):
    """替换 build_openai_client，捕获 chat.completions.create 的 kwargs"""
    captured = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return iter([])

    client = MagicMock()
    client.chat.completions.create.side_effect = _fake_create
    monkeypatch.setattr("app.utils.http_client.build_openai_client", lambda **kw: client)
    return captured


@pytest.fixture()
def persist_env(monkeypatch, tmp_path):
    """app_data_dir 指向 tmp，并落一张真实 persist 图（revive 读得到 data URI）"""
    from app.utils.utils import get_app_data_dir as _orig

    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: str(tmp_path))
    persisted = tmp_path / "screenshots" / "persisted" / "sid_mat"
    persisted.mkdir(parents=True)
    (persisted / _PERSIST_FNAME).write_bytes(_PNG_1PX)
    yield tmp_path
    # 占位引用避免未使用告警
    _ = _orig


def _ensure_plugins_loaded():
    """确保系统插件（openai adapter/serializer + transports）已注册（幂等）"""
    from app.plugins.loaders.runtime_component_loader import warmup_runtime_components

    warmup_runtime_components()


def test_sent_body_never_leaks_image_ref(capture_client, persist_env):
    """含图历史的子代理请求：发送体必须是 image_url 形态，image_ref 不外泄"""
    _ensure_plugins_loaded()
    executor = _build_executor()
    executor._vision_session_ids = ["sid_mat"]  # 真实路径由 _execute_agent_loop 首行收集，直呼需手动设
    executor._make_api_call(_fresh_messages(), llm_config=dict(executor.llm_config))

    sent = capture_client["messages"]
    assert sent, "请求必须包含 messages"
    for msg in sent:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            assert isinstance(block, dict)
            assert block.get("type") != "image_ref", f"image_ref 泄漏进 API 请求: {block}"

    # 图片必须以可发送形态出现（data URI）
    image_blocks = [
        b
        for msg in sent
        for b in (msg.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "image_url"
    ]
    assert image_blocks, "图片块应还原为 image_url 形态（不得丢失）"
    url = image_blocks[0].get("image_url", {}).get("url", "")
    assert url.startswith("data:image/png;base64,"), f"data URI 形态异常: {url[:60]}"


def test_original_messages_stay_revoked(capture_client, persist_env):
    """物化只作用于副本：原 messages 图块保持 image_ref（出口回收行为不变）"""
    _ensure_plugins_loaded()
    executor = _build_executor()
    executor._vision_session_ids = ["sid_mat"]
    original = _fresh_messages()
    executor._make_api_call(original, llm_config=dict(executor.llm_config))

    user_blocks = [b for m in original if m["role"] == "user" for b in (m.get("content") or []) if isinstance(b, dict)]
    ref_blocks = [b for b in user_blocks if b.get("type") == "image_ref"]
    assert ref_blocks, "原 messages 的图块应保持 image_ref（回收状态不被物化污染）"
    assert ref_blocks[0].get("image_ref", {}).get("file") == _PERSIST_FNAME
    # 原列表中不得出现 data URI（revive 未就地改写原件）
    for b in user_blocks:
        url = (b.get("image_url") or {}).get("url", "") if isinstance(b.get("image_url"), dict) else ""
        assert not url.startswith("data:"), "原 messages 图块被物化就地升级，出口回收被破坏"


def test_missing_persist_file_degrades_to_text(capture_client, monkeypatch, tmp_path):
    """persist 文件缺失：物化降级为 text 占位块，不抛错、不外泄 image_ref"""
    _ensure_plugins_loaded()
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: str(tmp_path))
    # 不创建 persisted 文件
    executor = _build_executor()
    executor._vision_session_ids = ["sid_mat"]
    executor._make_api_call(_fresh_messages(), llm_config=dict(executor.llm_config))

    sent = capture_client["messages"]
    for msg in sent:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            assert isinstance(block, dict)
            assert block.get("type") != "image_ref", f"image_ref 泄漏进 API 请求: {block}"
    all_text = " ".join(
        (
            str(b.get("text", ""))
            if isinstance(b, dict)
            else str(b)
        )
        for msg in sent
        for b in ([msg.get("content")] if not isinstance(msg.get("content"), list) else msg.get("content"))
    )
    assert "图片已失效" in all_text, "缺失 persist 文件应降级为 text 占位（字符串或块形态均可）"


# ═══════════════════════════════════════════════════════════════
# responses 路径回归（P1 修复第二路：transport 忽略 api_messages，
# 从内部格式 messages 重新序列化 —— image_ref 在该路会静默丢图）
# ═══════════════════════════════════════════════════════════════

from app.plugins.sdk import messages_to_responses_input  # noqa: E402 — 与 openai_responses transport 同源


class _FakeResponsesTransport:
    """responses transport 桩：create_stream 内执行真实 messages_to_responses_input，
    断言打在真实转换链（serializer → _extract_responses_content）上。"""

    id = "openai_responses"

    def __init__(self):
        self.calls = []  # 每次 create_stream 的 (messages, api_messages)
        self.input_items = None

    def create_stream(
        self, llm_config, messages, tools=None, auth_headers=None, api_messages=None, client=None, cap_max_tokens=None
    ):
        self.calls.append({"messages": messages, "api_messages": api_messages})
        self.input_items, _instructions = messages_to_responses_input(messages, True)
        return iter([])


def _with_responses_transport(monkeypatch, transport: _FakeResponsesTransport):
    """让 _make_api_call 解析到 fake responses transport（函数内 import 拿模块属性）"""
    monkeypatch.setattr(
        "app.core.workers.protocol_dispatch.resolve_transport_target",
        lambda cfg, flags: transport,
    )


def _image_parts_in_input_items(input_items):
    """从 responses input_items 收集 input_image part"""
    parts = []
    for item in input_items or []:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, list):
            continue
        for p in content:
            if isinstance(p, dict) and p.get("type") == "input_image":
                parts.append(p)
    return parts


def test_responses_path_input_items_carry_data_uri(monkeypatch, persist_env):
    """responses 主用例：input_items 必须含 input_image+data URI，image_ref 零外泄。

    修复前：transport 收到的内部 messages 已被出口回收为 image_ref，
    messages_to_responses_input 只认 image_url/input_image/image → 静默丢图。
    """
    _ensure_plugins_loaded()
    transport = _FakeResponsesTransport()
    _with_responses_transport(monkeypatch, transport)
    executor = _build_executor()
    executor._vision_session_ids = ["sid_mat"]
    executor._make_api_call(_fresh_messages(), llm_config=dict(executor.llm_config))

    assert transport.calls, "responses transport 必须收到 create_stream 调用"
    # messages 与 api_messages 两路同源（物化副本），均不得外泄 image_ref
    for call in transport.calls:
        for key in ("messages", "api_messages"):
            for msg in call[key]:
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for b in content:
                    assert not (isinstance(b, dict) and b.get("type") == "image_ref"), (
                        f"responses 路径 {key} 外泄 image_ref: {b}"
                    )
    # 真实转换链产物：input_image + data URI
    parts = _image_parts_in_input_items(transport.input_items)
    assert parts, "responses input_items 应含 input_image part（image_ref 形态会被静默丢图）"
    url = parts[0].get("image_url", "")
    assert url.startswith("data:image/png;base64,"), f"input_image 形态异常: {str(url)[:60]}"


def test_responses_path_same_image_two_blocks_restored(monkeypatch, persist_env):
    """同图多块：两块引用同一 persist 文件，物化后均还原，互不丢。"""
    _ensure_plugins_loaded()
    transport = _FakeResponsesTransport()
    _with_responses_transport(monkeypatch, transport)
    executor = _build_executor()
    executor._vision_session_ids = ["sid_mat"]

    ref = dict(_IMAGE_REF_BLOCK)
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "两张同图"}, dict(ref), dict(ref)]},
    ]
    executor._make_api_call(messages, llm_config=dict(executor.llm_config))

    parts = _image_parts_in_input_items(transport.input_items)
    assert len(parts) == 2, f"两块同图必须都出现在 input_items（实际 {len(parts)} 块）"
    assert parts[0].get("image_url") == parts[1].get("image_url"), "同源两块的 data URI 应一致"
    # 原件两块都保持 image_ref（出口回收不被破坏）
    kept = [b for b in messages[0]["content"] if isinstance(b, dict) and b.get("type") == "image_ref"]
    assert len(kept) == 2, "原件两块 image_ref 应原样保留"


def test_responses_path_retry_keeps_materialized_copy(monkeypatch, persist_env):
    """重试链：首次请求失败重试后，闭包捕获的物化副本仍含 data URI（不重建不退化）。"""
    _ensure_plugins_loaded()
    transport = _FakeResponsesTransport()
    _with_responses_transport(monkeypatch, transport)
    executor = _build_executor()
    executor._vision_session_ids = ["sid_mat"]

    def _fake_retry(client, create_stream, cancel_check=None):
        # 模拟重试语义：第一次调用后请求失败，第二次成功
        first = create_stream()
        _ = list(first)  # 消费首试事件流（惰性迭代器）
        second = create_stream()
        return second

    monkeypatch.setattr("app.core.workers.error_handler.create_api_call_with_retry", _fake_retry)
    executor._make_api_call(_fresh_messages(), llm_config=dict(executor.llm_config))

    assert len(transport.calls) == 2, f"应恰好两次尝试（实际 {len(transport.calls)}）"
    first_msgs = transport.calls[0]["messages"]
    second_msgs = transport.calls[1]["messages"]
    # 闭包捕获同一物化副本：重试不重建（同一 list 对象），data URI 不退化
    assert first_msgs is second_msgs, "重试应复用同一物化副本（闭包捕获），不得重建"
    for msg in second_msgs:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "image_url":
                assert b["image_url"]["url"].startswith("data:image/png;base64,"), (
                    "重试时物化副本的 data URI 应原样可用"
                )


def test_materialize_mixed_str_content_messages():
    """纯字符串 content 消息混在多模态列表：materialize 不崩、str 原样保持。"""
    executor = _build_executor()
    payload = [
        {"role": "system", "content": "sys-str"},
        {"role": "user", "content": [{"type": "text", "text": "带图"}, dict(_IMAGE_REF_BLOCK)]},
        {"role": "user", "content": "纯文本消息"},
        {"role": "assistant", "content": None},
    ]
    out = executor._materialize_for_send(copy.deepcopy(payload))

    assert len(out) == len(payload), "消息条数不得变化"
    assert isinstance(out[0]["content"], str) and out[0]["content"] == "sys-str", "str content 必须原样保持"
    assert out[2]["content"] == "纯文本消息", "纯文本消息必须原样保持"
    assert out[3]["content"] is None, "None content 必须原样保持"
    # 多模态消息正常处理（未挂 persist_env：revive 找不到文件 → 降级 text 占位，不抛错）
    blocks = out[1]["content"]
    assert isinstance(blocks, list)
    assert all(not (isinstance(b, dict) and b.get("type") == "image_ref") for b in blocks), "不得外泄 image_ref"


def test_materialize_skips_already_url_blocks(monkeypatch, persist_env):
    """原件已含 image_url（未回收形态）：revive 幂等跳过，url 不变（不重复编码）。"""
    _ensure_plugins_loaded()
    executor = _build_executor()

    data_uri = f"data:image/png;base64,{base64.b64encode(_PNG_1PX).decode('ascii')}"
    original_block = {"type": "image_url", "image_url": {"url": data_uri}}
    payload = [{"role": "user", "content": [{"type": "text", "text": "已物化"}, dict(original_block)]}]

    out = executor._materialize_for_send(copy.deepcopy(payload))

    kept = [b for b in out[0]["content"] if isinstance(b, dict)]
    assert kept[1].get("type") == "image_url", "已是 image_url 的块必须原样保留"
    assert kept[1]["image_url"]["url"] == data_uri, "revive 幂等：url 不得被二次编码/改写"
    # 转换链 sanity：该块可正常转 input_image（不因幂等跳过而丢失）
    items, _ = messages_to_responses_input(out, True)
    parts = _image_parts_in_input_items(items)
    assert parts and parts[0].get("image_url") == data_uri, "image_url 块应正常转换为 input_image"


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))

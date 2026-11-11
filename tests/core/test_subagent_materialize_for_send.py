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


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))

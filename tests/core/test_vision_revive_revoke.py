# -*- coding: utf-8 -*-
"""[方案 A] vision 图片 revive→revoke 往返与压缩 LRU 回归测试。

覆盖 #11 设计卡 §5 测试点：
1. revive→revoke 幂等往返（块还原/落盘文件不动）
2. revoke 后再 revive 数据完整
3. compress LRU 命中与驱逐
4. 压缩图 revoke 保留 data URI；反推失败（文件缺失）安全降级
5. chat_worker._materialize_for_send：cache 不被污染、物化副本含 data URI
6. subagent 对称断言：_revoke_vision_after_send 走共享实现

无 QApplication 依赖（纯函数层 + __new__ 桩；压缩实际执行路径由
test_vision_injection 与门禁 bench 覆盖）。
"""

import base64
import hashlib
import json

import pytest

from app.utils import image_utils
from app.utils.image_utils import (
    _COMPRESS_CACHE,
    compress_data_uri,
    revoke_vision_blocks,
)

# persist/revive 从 session_repository 导入（persist 为私有 impl，测试同仓库可用）
from app.core.store.session_repository import (
    _persist_vision_image_blocks_impl,
    revive_vision_image_refs,
)

SESSION_ID = "t_revive_revoke"


def _png_data_uri(w: int = 4, h: int = 4, byte: int = 200) -> str:
    """确定性小 PNG（不依赖 Qt：手写最小 PNG 结构交给 QImage/persist 均可）。

    persist 链只关心 data:image 前缀与 base64 可解码，用真 PNG 字节更贴近真实。
    这里用 1x1 PNG 的确定性字节（base64 固定）。
    """
    # 1x1 红色 PNG 的标准字节
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "53de0000000c4944415408d763f8cfc00000030101"
        "00184dfc900000000049454e44ae426082"
    )
    del w, h, byte
    return f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"


@pytest.fixture()
def vision_env(tmp_path, monkeypatch):
    """落盘根目录指向临时目录（不污染真实 AppData），并清空压缩 LRU。"""
    root = tmp_path / "appdata"
    root.mkdir()
    import app.utils.utils as uutils

    monkeypatch.setattr(uutils, "get_app_data_dir", lambda: str(root))
    _COMPRESS_CACHE.clear()
    yield root
    _COMPRESS_CACHE.clear()


def _make_messages(n_img: int = 2):
    msgs = []
    for i in range(n_img):
        msgs.append(
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": _png_data_uri()}},
                    {"type": "text", "text": f"截图 {i}"},
                ],
            }
        )
    msgs.append({"role": "assistant", "content": "收到"})
    return msgs


def _image_ref_blocks(messages):
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "image_ref":
                    out.append(b)
    return out


def _data_uri_blocks(messages):
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "image_url":
                    out.append(b)
    return out


class TestReviveRevokeRoundtrip:
    def test_revoke_restores_image_ref(self, vision_env):
        """revive→revoke 幂等往返：块还原、file/meta/session_id 与 persist 原块一致。"""
        original = _make_messages(2)
        persisted = json.loads(json.dumps(original))
        persisted = _persist_vision_image_blocks_impl(persisted, SESSION_ID, str(vision_env))
        refs_before = json.loads(json.dumps(_image_ref_blocks(persisted)))
        assert len(refs_before) == 2

        revived = revive_vision_image_refs(persisted)
        assert len(_data_uri_blocks(revived)) == 2

        restored = revoke_vision_blocks(revived, SESSION_ID, root_dir=str(vision_env))
        assert restored == 2
        refs_after = _image_ref_blocks(revived)
        assert len(refs_after) == 2
        for a, b in zip(refs_after, refs_before):
            assert a["image_ref"]["file"] == b["image_ref"]["file"]
            assert a["image_ref"]["meta"] == b["image_ref"]["meta"]
            assert a["image_ref"]["session_id"] == b["image_ref"]["session_id"]
        assert _data_uri_blocks(revived) == []

    def test_revoke_does_not_touch_files(self, vision_env):
        """revoke 只改消息结构，落盘文件字节不动。"""
        persisted = _persist_vision_image_blocks_impl(_make_messages(1), SESSION_ID, str(vision_env))
        ref = _image_ref_blocks(persisted)[0]["image_ref"]
        fpath = vision_env / "screenshots" / "persisted" / SESSION_ID / ref["file"]
        raw_before = fpath.read_bytes()

        revived = revive_vision_image_refs(persisted)
        revoke_vision_blocks(revived, SESSION_ID, root_dir=str(vision_env))

        assert fpath.read_bytes() == raw_before

    def test_revoke_then_revive_data_intact(self, vision_env):
        """revoke 后再 revive：data URI 与首次 revive 逐字节一致。"""
        persisted = _persist_vision_image_blocks_impl(_make_messages(2), SESSION_ID, str(vision_env))
        first = revive_vision_image_refs(persisted)
        uris_first = [b["image_url"]["url"] for b in _data_uri_blocks(first)]

        revoke_vision_blocks(first, SESSION_ID, root_dir=str(vision_env))
        second = revive_vision_image_refs(first)
        uris_second = [b["image_url"]["url"] for b in _data_uri_blocks(second)]
        assert uris_second == uris_first

    def test_revoke_idempotent(self, vision_env):
        """重复 revoke 第二次返回 0。"""
        persisted = _persist_vision_image_blocks_impl(_make_messages(1), SESSION_ID, str(vision_env))
        revived = revive_vision_image_refs(persisted)
        assert revoke_vision_blocks(revived, SESSION_ID, root_dir=str(vision_env)) == 1
        assert revoke_vision_blocks(revived, SESSION_ID, root_dir=str(vision_env)) == 0


class TestRevokeSafetyDegradation:
    def test_compressed_uri_kept(self, vision_env):
        """压缩图（摘要命中压缩 LRU）revoke 时保留 data URI，不还原。"""
        persisted = _persist_vision_image_blocks_impl(_make_messages(1), SESSION_ID, str(vision_env))
        revived = revive_vision_image_refs(persisted)
        uri = _data_uri_blocks(revived)[0]["image_url"]["url"]
        # 模拟该 URI 经历过压缩：预填 LRU
        digest = image_utils._data_uri_digest(uri)
        _COMPRESS_CACHE[digest] = "data:image/jpeg;base64,zzz"

        assert revoke_vision_blocks(revived, SESSION_ID, root_dir=str(vision_env)) == 0
        assert _data_uri_blocks(revived)[0]["image_url"]["url"] == uri

    def test_missing_file_skipped(self, vision_env):
        """反推 sha1 无对应落盘文件（如伪造 URI）→ 安全降级不还原。"""
        fake = "data:image/png;base64," + base64.b64encode(b"never-persisted-bytes").decode()
        msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": fake}}]}]
        assert revoke_vision_blocks(msgs, SESSION_ID, root_dir=str(vision_env)) == 0
        assert _data_uri_blocks(msgs)[0]["image_url"]["url"] == fake

    def test_multi_session_candidates(self, vision_env):
        """session_id 候选列表：真实落盘目录在候选中即可还原。"""
        persisted = _persist_vision_image_blocks_impl(_make_messages(1), "other_session", str(vision_env))
        revived = revive_vision_image_refs(persisted)
        restored = revoke_vision_blocks(revived, [SESSION_ID, "other_session"], root_dir=str(vision_env))
        assert restored == 1
        ref = _image_ref_blocks(revived)[0]["image_ref"]
        assert ref["session_id"] == "other_session"


class TestCompressLRU:
    def test_hit_skips_recompress(self, vision_env, monkeypatch):
        """同 URI 第二次压缩命中 LRU，不再走压缩体。"""
        calls = []

        def fake_impl(data_uri, max_bytes):
            calls.append(data_uri)
            return "data:image/jpeg;base64,compressed"

        monkeypatch.setattr(image_utils, "_compress_impl", fake_impl)
        big = "data:image/png;base64," + base64.b64encode(b"x" * (6 * 1024 * 1024)).decode()

        out1 = compress_data_uri(big)
        out2 = compress_data_uri(big)
        assert out1 == out2 == "data:image/jpeg;base64,compressed"
        assert len(calls) == 1  # 第二次命中缓存

    def test_small_uri_not_cached(self, vision_env):
        """未超限的 URI 不进缓存（走快路径返回原样）。"""
        small = _png_data_uri()
        out = compress_data_uri(small)
        assert out == small
        assert len(_COMPRESS_CACHE) == 0

    def test_eviction_maxsize(self, vision_env, monkeypatch):
        """LRU 上限 4：第 5 条驱逐最早条目。"""
        monkeypatch.setattr(
            image_utils,
            "_compress_impl",
            lambda data_uri, max_bytes: "data:image/jpeg;base64,c_" + data_uri[-8:],
        )
        uris = []
        for i in range(5):
            uris.append(
                "data:image/png;base64," + base64.b64encode(f"img-{i}".encode() + b"x" * (6 * 1024 * 1024)).decode()
            )
            compress_data_uri(uris[-1])
        assert len(_COMPRESS_CACHE) == 4
        assert image_utils._data_uri_digest(uris[0]) not in _COMPRESS_CACHE  # 最早被驱逐
        assert image_utils._data_uri_digest(uris[-1]) in _COMPRESS_CACHE  # 最新保留


class TestChatWorkerMaterialize:
    def _stub_worker(self, cache_msgs):
        from app.core.workers.chat_worker import OpenAIChatWorker

        w = OpenAIChatWorker.__new__(OpenAIChatWorker)  # 测试桩先例：chat_worker.py:182
        w.session_id = SESSION_ID
        w.messages = []
        w._api_messages_cache = cache_msgs
        return w

    def test_materialize_does_not_pollute_cache(self, vision_env):
        """物化副本含 data URI，cache 本体保持 image_ref 形态。"""
        persisted = _persist_vision_image_blocks_impl(_make_messages(1), SESSION_ID, str(vision_env))
        cache = json.loads(json.dumps(persisted))
        assert _data_uri_blocks(cache) == []  # cache 是轻量形态

        w = self._stub_worker(cache)
        out = w._materialize_for_send(cache)
        assert len(_data_uri_blocks(out)) == 1  # 副本含 data URI（可发送）
        assert _data_uri_blocks(cache) == []  # cache 未被污染

    def test_materialize_idempotent_on_data_uri(self, vision_env):
        """对已是 data URI 形态的列表物化：内容不变（revive 对 data URI 块跳过）。"""
        persisted = _persist_vision_image_blocks_impl(_make_messages(1), SESSION_ID, str(vision_env))
        revived = revive_vision_image_refs(persisted)
        uris = [b["image_url"]["url"] for b in _data_uri_blocks(revived)]

        w = self._stub_worker(revived)
        out = w._materialize_for_send(revived)
        assert [b["image_url"]["url"] for b in _data_uri_blocks(out)] == uris


class TestSubagentSymmetry:
    def test_revoke_after_send_uses_shared_impl(self, vision_env, monkeypatch):
        """subagent._revoke_vision_after_send 走共享 revoke_vision_blocks（对称断言）。"""
        from app.core.workers.subagent_worker import SubAgentExecutor

        persisted = _persist_vision_image_blocks_impl(_make_messages(1), "parent_session", str(vision_env))
        revived = revive_vision_image_refs(persisted)

        w = SubAgentExecutor.__new__(SubAgentExecutor)
        w._vision_session_ids = ["parent_session"]
        seen = {}

        def fake_revoke(messages, session_id, root_dir=None):
            seen["messages"] = messages
            seen["session_id"] = session_id
            return revoke_vision_blocks(messages, session_id, root_dir=str(vision_env))

        monkeypatch.setattr(image_utils, "revoke_vision_blocks", fake_revoke)
        w._revoke_vision_after_send(revived)
        assert seen["messages"] is revived
        assert seen["session_id"] == ["parent_session"]  # 候选列表透传

    def test_collect_vision_session_ids(self):
        """继承历史的 image_ref.session_id 收集（revive 前调用）。"""
        from app.core.workers.subagent_worker import SubAgentExecutor

        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "image_ref", "image_ref": {"file": "a.png", "meta": "image/png", "session_id": "s1"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "image_ref", "image_ref": {"file": "b.jpg", "meta": "image/jpeg", "session_id": "s2"}},
                ],
            },
            {"role": "user", "content": "纯文本"},
        ]
        w = SubAgentExecutor.__new__(SubAgentExecutor)
        assert w._collect_vision_session_ids(msgs) == ["s1", "s2"]

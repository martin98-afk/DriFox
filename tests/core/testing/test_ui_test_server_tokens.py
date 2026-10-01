# -*- coding: utf-8 -*-
"""tokens.py 协议迁移测试（M2）：发放/校验/过期/错配/黑名单。"""

from __future__ import annotations

from tools.ui_test_server.tokens import TokenStore, match_dangerous


def test_issue_and_validate_roundtrip():
    store = TokenStore()
    token, expires = store.issue("send_btn")
    assert token.startswith("tok_") and expires > 0
    ok, reason = store.validate(token, "send_btn")
    assert ok and reason == ""


def test_one_time_consumption():
    store = TokenStore()
    token, _ = store.issue("send_btn")
    assert store.validate(token, "send_btn", consume=True)[0]
    ok, reason = store.validate(token, "send_btn", consume=True)
    assert not ok and reason == "missing_token"


def test_expired_rejected():
    store = TokenStore(ttl_s=-1)  # 立即过期
    token, _ = store.issue("send_btn")
    ok, reason = store.validate(token, "send_btn")
    assert not ok and reason == "expired_token"


def test_mismatch_rejected():
    store = TokenStore()
    token, _ = store.issue("send_btn")
    ok, reason = store.validate(token, "other_btn")
    assert not ok and reason == "token_mismatch"


def test_missing_rejected():
    store = TokenStore()
    ok, reason = store.validate("tok_nope", "send_btn")
    assert not ok and reason == "missing_token"


def test_sweep_clears_expired():
    import time

    store = TokenStore()
    for _ in range(5):
        tok, _ = store.issue("x")
        # 手动把 expires 改到过去，模拟既有过期令牌
        store._store[tok]["expires"] = time.time() - 1
    store.issue("live")  # issue 路径惰性清扫：5 个过期项被清
    assert len(store._store) == 1  # 仅剩 live


def test_dangerous_keywords():
    assert match_dangerous("删除按钮") == "删除"
    assert match_dangerous("Remove Item") == "remove"
    assert match_dangerous("发送") is None

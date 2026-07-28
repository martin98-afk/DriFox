# -*- coding: utf-8 -*-
"""SessionRepository._content_hash_cache 线程安全测试

回归保护：避免多线程并发 save 时因裸 dict 出现脏命中 / 丢失更新。
"""

import threading
from unittest.mock import MagicMock


def _make_repo():
    """构造一个最小可用的 SessionRepository，绕过数据库初始化。"""
    from app.core.store.session_repository import SessionRepository

    # 模拟 db_manager，is_initialized 返回 True 让 save 进入主流程
    db = MagicMock()
    db.is_connected = True
    repo = SessionRepository(db)
    return repo, db


def test_content_hash_cache_lock_exists():
    """回归保护：_cache_lock 必须存在（避免忘记加锁）。"""
    repo, _ = _make_repo()
    assert hasattr(repo, "_cache_lock"), "SessionRepository 必须有 _cache_lock 保护 _content_hash_cache"
    # 必须是锁对象
    assert hasattr(repo._cache_lock, "acquire"), "_cache_lock 必须是锁"


def test_concurrent_save_with_unchanged_content_does_not_corrupt_cache():
    """并发 save 相同内容 → 缓存状态必须一致（不出现 KeyError 或丢失更新）。"""
    repo, db = _make_repo()

    # 模拟 execute_sql 总是成功
    db.execute_sql.return_value = (True, [{"rowcount": 1}])

    session = {
        "session_id": "s1",
        "title": "T",
        "project": "P",
        "messages": [{"role": "user", "content": "hi"}],
        "system_prompt": "",
        "compaction_state": {},
        "compaction_cache": {},
        "message_count": 1,
        "user_edited_title": False,
        "worktree_path": "",
        "preview": "",
        "context_usage": 0,
        "last_api_prompt_tokens": 0,
        "last_api_message_count": 0,
    }

    errors = []

    def worker():
        try:
            for _ in range(50):
                repo.save(session)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发 save 出现异常: {errors}"
    # 缓存必须有一致的 key
    assert "s1" in repo._content_hash_cache


def test_save_returns_true_for_unchanged_content():
    """未变化内容时直接返回 True（跳过序列化+写盘）。"""
    repo, db = _make_repo()
    db.execute_sql.return_value = (True, [{"rowcount": 1}])

    session = {
        "session_id": "s2",
        "title": "T",
        "project": "P",
        "messages": [{"role": "user", "content": "hi"}],
        "system_prompt": "",
        "compaction_state": {},
        "compaction_cache": {},
        "message_count": 1,
        "user_edited_title": False,
        "worktree_path": "",
        "preview": "",
        "context_usage": 0,
        "last_api_prompt_tokens": 0,
        "last_api_message_count": 0,
    }

    assert repo.save(session) is True
    # 第一次会写盘，db.execute_sql 至少调用 1 次
    assert db.execute_sql.called

    # 再次 save 相同内容：命中缓存，不应再调用 db.execute_sql
    db.execute_sql.reset_mock()
    assert repo.save(session) is True
    # 注意：第二次的 db 调用次数取决于 _reclaim_freelist_if_needed；
    # 内容指纹检测应该在 serialize 之前，所以 execute_sql 不会被 serialize 路径触发
    # （_reclaim_freelist_if_needed 仍可能调用，但 1 次 PRAGMA 查询）
    # 关键断言：cache 命中路径不应调用 INSERT OR REPLACE
    insert_calls = [
        c for c in db.execute_sql.call_args_list
        if "INSERT OR REPLACE" in (c.args[0] if c.args else "")
    ]
    assert not insert_calls, f"未变化内容不应触发 INSERT OR REPLACE，实际调用: {db.execute_sql.call_args_list}"


def test_get_invalidates_cache_atomically():
    """get() 必须失效缓存（之前是裸 pop，加锁后行为不变）。"""
    repo, db = _make_repo()
    db.execute_sql.return_value = (True, [{"session_id": "s3", "title": "T", "messages": "[]"}])

    # 预填缓存
    repo._content_hash_cache["s3"] = (1, 12345)
    # 反序列化 messages 字段为空列表
    from app.core.store import serde

    db.execute_sql.return_value = (True, [{
        "session_id": "s3",
        "title": "T",
        "project": "P",
        "messages": serde.serialize([]),
        "system_prompt": "",
        "compaction_state": serde.serialize({}),
        "compaction_cache": serde.serialize({}),
        "message_count": 0,
        "user_edited_title": 0,
        "worktree_path": "",
        "preview": "",
        "context_usage": 0,
        "last_api_prompt_tokens": 0,
        "last_api_message_count": 0,
        "created_at": "2025-01-01 00:00:00",
        "updated_at": "2025-01-01 00:00:00",
    }])

    result = repo.get("s3")
    assert result["session_id"] == "s3"
    # 缓存被失效
    assert "s3" not in repo._content_hash_cache

# -*- coding: utf-8 -*-
"""失效 worktree 关键文档自净化测试

背景（2026-09-16）：worktree 目录被外部删除且 git 记录被 prune 之后，
worktree-manager 的缺失检测（数据源 `git worktree list`）再也列不出该路径，
DB 里的 git_worktree 记录永久残留 → 关键文档注入持续输出不存在的目录。

覆盖：
- repository 层只清 git_worktree、保留 manual / stage_files / URL
- memory_manager 层节流与悬空工作目录善后
- backend.build_key_documents_context 注入前自净化（不再注入失效路径）
"""

import os
import time

import pytest


@pytest.fixture
def store(tmp_path):
    from app.core.store.session_store import SessionStore

    SessionStore._instance = None
    s = SessionStore(str(tmp_path))
    assert s.is_initialized
    yield s
    try:
        s.close()
    except Exception:
        pass
    from app.core.store.session_store import SessionStore
    from app.utils.db_manager import DatabaseManager

    SessionStore._instance = None
    DatabaseManager._instance = None


@pytest.fixture
def repo(store):
    from app.core.store.key_documents_repository import KeyDocumentsRepository

    return KeyDocumentsRepository(store._db)


def _mkdir(tmp_path, name):
    p = tmp_path / name
    p.mkdir()
    return str(p).replace("\\", "/")


def _touch_added_at(mm, project, path, ts):
    """显式刷新 added_at

    added_at 精度为秒，同秒内多条 is_working_dir=1 时 ORDER BY added_at DESC
    的胜者不确定。测试需要确定的工作目录，这里直接写时间戳消除歧义。
    """
    mm._key_documents_repo._execute(
        "UPDATE key_documents SET added_at = ? WHERE project = ? AND file_path = ?",
        (ts, project, path.replace("\\", "/")),
    )


# ── repository 层 ──


def test_prune_removes_missing_worktree(repo, tmp_path):
    """路径不存在的 git_worktree 记录被清掉，存在的保留"""
    alive = _mkdir(tmp_path, "alive")
    gone = str(tmp_path / "gone").replace("\\", "/")

    repo.add("P", alive, "git_worktree")
    repo.add("P", gone, "git_worktree")

    removed = repo.prune_stale_worktrees("P")
    assert removed == [("P", gone)]

    left = [d["file_path"] for d in repo.get_by_project("P")]
    assert left == [alive]


def test_prune_keeps_manual_and_stage_files(repo, tmp_path):
    """manual / stage_files 记录不参与清理（可能是网络盘或临时拔除的移动盘）"""
    gone = str(tmp_path / "gone-manual").replace("\\", "/")
    gone2 = str(tmp_path / "gone-staged").replace("\\", "/")

    repo.add("P", gone, "manual")
    repo.add("P", gone2, "stage_files")

    assert repo.prune_stale_worktrees("P") == []
    assert len(repo.get_by_project("P")) == 2


def test_prune_keeps_url(repo):
    """URL 条目不参与路径探测（非文件系统路径）"""
    repo.add("P", "https://example.com/doc", "git_worktree")
    assert repo.prune_stale_worktrees("P") == []
    assert len(repo.get_by_project("P")) == 1


def test_prune_scoped_to_project(repo, tmp_path):
    """限定项目时不影响其他项目"""
    gone_a = str(tmp_path / "gone-a").replace("\\", "/")
    gone_b = str(tmp_path / "gone-b").replace("\\", "/")
    repo.add("A", gone_a, "git_worktree")
    repo.add("B", gone_b, "git_worktree")

    repo.prune_stale_worktrees("A")
    assert repo.get_by_project("A") == []
    assert len(repo.get_by_project("B")) == 1

    # 空项目名 = 扫描全部
    repo.prune_stale_worktrees("")
    assert repo.get_by_project("B") == []


# ── memory_manager 层 ──


@pytest.fixture
def mm(store):
    """绕过全局存储门面，直接把临时库的仓储注入 MemoryManagerCore"""
    from app.core.conversation.memory_manager import MemoryManagerCore
    from app.core.store.key_documents_repository import KeyDocumentsRepository

    MemoryManagerCore._instance = None
    inst = MemoryManagerCore.get_instance()
    inst._session_store = store
    inst._db_manager = store._db
    inst._key_documents_repo = KeyDocumentsRepository(store._db)
    inst._pruned_at.clear()
    yield inst
    MemoryManagerCore._instance = None


def test_mm_throttle_skips_second_call(mm, tmp_path):
    """节流窗口内第二次调用直接返回，不重复探测"""
    gone = str(tmp_path / "gone").replace("\\", "/")
    mm.add_key_document("P", gone, "git_worktree")

    first = mm.prune_stale_worktrees("P", throttle_seconds=60.0)
    assert len(first) == 1
    # 窗口内：即便再塞一条失效记录也不清理
    gone2 = str(tmp_path / "gone2").replace("\\", "/")
    mm.add_key_document("P", gone2, "git_worktree")
    assert mm.prune_stale_worktrees("P", throttle_seconds=60.0) == []
    assert len(mm.get_key_documents("P")) == 1

    # 窗口归零后恢复清理
    mm._pruned_at.clear()
    assert len(mm.prune_stale_worktrees("P", throttle_seconds=0.0)) == 1


def test_mm_heals_dangling_workdir(mm, tmp_path):
    """工作目录指向被清理的 worktree 时，回退到仍存在的根目录"""
    root = _mkdir(tmp_path, "root")
    wt = str(tmp_path / "wt-gone").replace("\\", "/")

    mm.add_key_document("P", root, "manual")
    mm.add_key_document("P", wt, "git_worktree")
    mm.set_working_directory("P", wt)
    mm.restore_working_directory_mark("P", root)
    # wt 的时间戳更新 → 工作目录解析为 wt（确定性）
    _touch_added_at(mm, "P", wt, "2099-01-01 00:00:00")
    _touch_added_at(mm, "P", root, "2020-01-01 00:00:00")
    assert mm.get_working_directory("P") == wt

    removed = mm.prune_stale_worktrees("P", throttle_seconds=0.0)
    assert [r["file_path"] for r in removed] == [wt]
    # 悬空工作目录回退到仍存在的根目录
    assert mm.get_working_directory("P") == root


def test_mm_clears_dangling_workdir_without_candidate(mm, tmp_path):
    """无可用根目录时清空工作目录标记，不留悬空指针"""
    wt = str(tmp_path / "wt-gone").replace("\\", "/")
    mm.add_key_document("P", wt, "git_worktree")
    mm.set_working_directory("P", wt)
    assert mm.get_working_directory("P") == wt

    mm.prune_stale_worktrees("P", throttle_seconds=0.0)
    assert mm.get_working_directory("P") is None


# ── backend 注入路径 ──


def test_backend_context_excludes_stale_worktree(mm, tmp_path, monkeypatch):
    """注入上下文不再包含失效 worktree 路径（端到端）"""
    from app.core.conversation.backend import ChatBackend

    root = _mkdir(tmp_path, "proj")
    gone = str(tmp_path / "gone-wt").replace("\\", "/")
    mm.add_key_document("P", root, "manual")
    mm.add_key_document("P", gone, "git_worktree")
    mm.set_working_directory("P", root)

    backend = ChatBackend.__new__(ChatBackend)  # 绕过重初始化，只测目标方法
    backend._memory_manager = mm
    backend._current_project = "P"
    backend._tool_executor = None

    ctx = backend.build_key_documents_context()
    paths = [d["display"] for d in ctx.get("key_documents", [])]
    assert gone not in " ".join(paths)
    # 失效记录已从 DB 清除
    assert all(d["file_path"] != gone for d in mm.get_key_documents("P"))


def test_backend_context_survives_prune_failure(mm, monkeypatch):
    """清理抛异常时注入路径不崩（防御性降级）"""
    from app.core.conversation.backend import ChatBackend

    def _boom(*a, **kw):
        raise RuntimeError("db locked")

    monkeypatch.setattr(mm, "prune_stale_worktrees", _boom)

    backend = ChatBackend.__new__(ChatBackend)
    backend._memory_manager = mm
    backend._current_project = "P"
    backend._tool_executor = None

    assert isinstance(backend.build_key_documents_context(), dict)


def test_prune_idempotent(repo, tmp_path):
    """重复清理无副作用（第二次返回空）"""
    gone = str(tmp_path / "gone").replace("\\", "/")
    repo.add("P", gone, "git_worktree")
    assert len(repo.prune_stale_worktrees("P")) == 1
    assert repo.prune_stale_worktrees("P") == []


def test_prune_handles_windows_separators(repo, tmp_path):
    """反斜杠路径也能正确判定（DB 统一存正斜杠，比对前做过归一）"""
    gone_dir = tmp_path / "win-gone"
    gone_backslash = str(gone_dir)
    repo.add("P", gone_backslash, "git_worktree")
    # DB 存正斜杠，os.path.isdir 在 Windows 上两种分隔符都认
    assert len(repo.prune_stale_worktrees("P")) == 1


def test_prune_on_uninitialized_repo():
    """未连接数据库时安全返回空列表"""
    from app.core.store.key_documents_repository import KeyDocumentsRepository

    assert KeyDocumentsRepository(None).prune_stale_worktrees("P") == []


def test_mm_prune_without_repo():
    """memory_manager 无仓储时安全返回"""
    from app.core.conversation.memory_manager import MemoryManagerCore

    MemoryManagerCore._instance = None
    inst = MemoryManagerCore.get_instance()
    inst._key_documents_repo = None
    try:
        assert inst.prune_stale_worktrees("P") == []
    finally:
        MemoryManagerCore._instance = None


def test_throttle_key_per_project(mm, tmp_path):
    """节流按项目独立：A 刚清理过不影响 B"""
    gone_a = str(tmp_path / "a").replace("\\", "/")
    gone_b = str(tmp_path / "b").replace("\\", "/")
    mm.add_key_document("A", gone_a, "git_worktree")
    mm.add_key_document("B", gone_b, "git_worktree")

    assert len(mm.prune_stale_worktrees("A", throttle_seconds=60.0)) == 1
    assert len(mm.prune_stale_worktrees("B", throttle_seconds=60.0)) == 1
    assert mm.prune_stale_worktrees("A", throttle_seconds=60.0) == []


def test_prune_timestamp_recorded(mm, tmp_path):
    """清理后记录节流时间戳，供后续调用判定"""
    gone = str(tmp_path / "gone").replace("\\", "/")
    mm.add_key_document("P", gone, "git_worktree")
    before = time.monotonic()
    mm.prune_stale_worktrees("P", throttle_seconds=0.0)
    assert mm._pruned_at.get("P", 0) >= before


def test_heal_prefers_marked_root(mm, tmp_path):
    """多个候选目录时优先回退到仍带根目录标记的那个"""
    root_marked = _mkdir(tmp_path, "marked")
    other = _mkdir(tmp_path, "other")
    wt = str(tmp_path / "wt-gone").replace("\\", "/")

    mm.add_key_document("P", root_marked, "manual")
    mm.add_key_document("P", other, "manual")
    mm.add_key_document("P", wt, "git_worktree")
    mm.set_working_directory("P", wt)
    mm.restore_working_directory_mark("P", root_marked)
    _touch_added_at(mm, "P", wt, "2099-01-01 00:00:00")
    _touch_added_at(mm, "P", root_marked, "2020-01-01 00:00:00")
    _touch_added_at(mm, "P", other, "2021-01-01 00:00:00")

    mm.prune_stale_worktrees("P", throttle_seconds=0.0)
    assert mm.get_working_directory("P") == root_marked

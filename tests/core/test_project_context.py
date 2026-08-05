# -*- coding: utf-8 -*-
"""ProjectContext 单元测试

测试范围：
1. bind_root:    DB 写入 + 缓存 + tool_executor + 信号
2. switch_project: 项目切换 + workdir 联动
3. switch_worktree: 幂等性 + DB 写入
4. restore_main_repo: 幂等性
5. clear_workdir: 缓存清除 + tool_executor 同步
6. get_workdir / get_effective_workdir: 缓存优先 + DB 回退
7. 多窗口隔离: 各窗口独立缓存
8. _sync_from_instance_cache: 新窗口初始化

设计说明：
- 使用 mock 替代 backend/memory_manager/tool_executor，避免真实 DB
- Qt 信号用 QSignalSpy 验证（PyQt5 提供）
- 不创建真实 QApplication，因为 ProjectContext 只用 QObject 信号基础设施
"""

import os
from unittest.mock import MagicMock

import pytest
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtTest import QSignalSpy


class SignalCounter(QObject):
    """简单信号计数辅助类（避免依赖 QSignalSpy 的 event loop）"""

    def __init__(self, signal):
        super().__init__()
        self.count = 0
        self.last_args = None
        signal.connect(self._on_emit)

    def _on_emit(self, *args):
        self.count += 1
        self.last_args = args


# 必须在导入 ProjectContext 前，因为 PyQt5 信号需要 QCoreApplication 或
# 在 PyQt5 测试中可省略（信号对象不依赖 event loop，仅 connect/emit）
from app.core.project_context import ProjectContext


# ══════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════


@pytest.fixture
def mock_backend():
    """构造一个 mock backend，模拟 memory_manager 和 tool_executor"""
    backend = MagicMock()
    backend.memory_manager = MagicMock()
    backend.memory_manager.get_working_directory.return_value = None
    backend.memory_manager.get_key_documents.return_value = []
    backend.memory_manager.add_key_document.return_value = True
    backend.memory_manager.set_working_directory.return_value = True
    backend.memory_manager.restore_working_directory_mark.return_value = True

    backend.tool_executor = MagicMock()
    backend.tool_executor.set_workdir = MagicMock()
    return backend


@pytest.fixture
def ctx(mock_backend):
    """构造 ProjectContext 实例"""
    return ProjectContext(backend=mock_backend, window_id="test-window")


# ══════════════════════════════════════════════════════════
# 1. bind_root - 完整同步链
# ══════════════════════════════════════════════════════════


class TestBindRoot:
    """测试 bind_root 完整同步链"""

    def test_writes_to_db(self, ctx, mock_backend):
        """应写入关键文档 + 设置工作目录"""
        ctx.bind_root("项目A", "/path/to/root")
        mock_backend.memory_manager.add_key_document.assert_called_once_with(
            "项目A", "/path/to/root", added_by="manual"
        )
        mock_backend.memory_manager.set_working_directory.assert_called_once_with("项目A", "/path/to/root")

    def test_updates_cache(self, ctx):
        """应更新实例缓存"""
        ctx.bind_root("项目A", "/path/to/root")
        assert ctx.get_workdir("项目A") == "/path/to/root"

    def test_syncs_tool_executor(self, ctx, mock_backend):
        """应同步到 tool_executor"""
        ctx.bind_root("项目A", "/path/to/root")
        mock_backend.tool_executor.set_workdir.assert_called_once_with("/path/to/root")

    def test_emits_signals_in_order(self, ctx):
        """应按顺序触发 workdir_changed / branch_refresh_needed / key_documents_refresh_needed"""
        wd_counter = SignalCounter(ctx.workdir_changed)
        branch_counter = SignalCounter(ctx.branch_refresh_needed)
        docs_counter = SignalCounter(ctx.key_documents_refresh_needed)

        ctx.bind_root("项目A", "/path/to/root")

        assert wd_counter.count == 1
        assert branch_counter.count == 1
        assert docs_counter.count == 1
        # workdir_changed 应携带 (project, workdir)
        assert wd_counter.last_args == ("项目A", "/path/to/root")

    def test_returns_true_on_success(self, ctx):
        """成功绑定应返回 True"""
        assert ctx.bind_root("项目A", "/path/to/root") is True

    def test_returns_false_on_empty_args(self, ctx):
        """空参数应返回 False，不写 DB"""
        assert ctx.bind_root("", "/path") is False
        assert ctx.bind_root("项目A", "") is False
        ctx._backend.memory_manager.add_key_document.assert_not_called()

    def test_handles_db_failure(self, ctx, mock_backend):
        """DB 写入失败应捕获异常并返回 False"""
        mock_backend.memory_manager.add_key_document.side_effect = RuntimeError("db error")
        assert ctx.bind_root("项目A", "/path/to/root") is False


# ══════════════════════════════════════════════════════════
# 2. switch_project - 项目切换
# ══════════════════════════════════════════════════════════


class TestSwitchProject:
    """测试 switch_project 行为"""

    def test_updates_current_project(self, ctx):
        """应更新 _current_project"""
        ctx.switch_project("项目A")
        assert ctx.get_current_project() == "项目A"

    def test_emits_project_changed(self, ctx):
        """应触发 project_changed 信号"""
        ctx.bind_root("项目A", "/path/a")
        counter = SignalCounter(ctx.project_changed)
        ctx.switch_project("项目A")
        assert counter.count == 1
        assert counter.last_args == ("项目A", "/path/a")

    def test_emits_branch_refresh(self, ctx):
        """切换项目应触发分支刷新"""
        counter = SignalCounter(ctx.branch_refresh_needed)
        ctx.switch_project("项目A")
        assert counter.count == 1

    def test_empty_workdir_propagation(self, ctx):
        """无 workdir 时 project_changed 应携带空字符串"""
        counter = SignalCounter(ctx.project_changed)
        ctx.switch_project("新项目")
        assert counter.last_args == ("新项目", "")


# ══════════════════════════════════════════════════════════
# 3. switch_worktree - 幂等性 + DB
# ══════════════════════════════════════════════════════════


class TestSwitchWorktree:
    """测试 switch_worktree 行为"""

    def test_idempotent_same_path(self, ctx, mock_backend, monkeypatch):
        """相同路径切换应跳过（幂等）"""
        # 让 os.path.isdir 对 /path/* 都返回 True（mock 文件系统）
        monkeypatch.setattr("os.path.isdir", lambda p: p.startswith("/path/"))
        ctx.bind_root("项目A", "/path/main")
        # 第一次切换
        result1 = ctx.switch_worktree("项目A", "/path/wt1")
        # 第二次切到同一路径
        mock_backend.memory_manager.set_working_directory.reset_mock()
        result2 = ctx.switch_worktree("项目A", "/path/wt1")
        assert result1 is True
        assert result2 is False  # 幂等：第二次跳过
        mock_backend.memory_manager.set_working_directory.assert_not_called()

    def test_invalid_path_returns_false(self, ctx, monkeypatch):
        """无效路径应返回 False"""
        monkeypatch.setattr("os.path.isdir", lambda p: False)
        assert ctx.switch_worktree("项目A", "") is False
        assert ctx.switch_worktree("项目A", "/nonexistent/path") is False

    def test_writes_key_document_with_worktree_tag(self, ctx, mock_backend, monkeypatch):
        """应添加 worktree 类型的关键文档"""
        monkeypatch.setattr("os.path.isdir", lambda p: p.startswith("/path/"))
        ctx.switch_worktree("项目A", "/path/wt1")
        mock_backend.memory_manager.add_key_document.assert_called_with("项目A", "/path/wt1", added_by="git_worktree")

    def test_restores_main_repo_mark(self, ctx, mock_backend, monkeypatch):
        """切换 worktree 后应恢复主仓库标记"""
        monkeypatch.setattr("os.path.isdir", lambda p: p.startswith("/path/"))
        mock_backend.memory_manager.get_working_directory.return_value = "/path/main"
        mock_backend.memory_manager.get_key_documents.return_value = [{"file_path": "/path/main", "added_by": "manual"}]
        ctx.switch_worktree("项目A", "/path/wt1")
        # 验证调用了 restore_working_directory_mark
        mock_backend.memory_manager.restore_working_directory_mark.assert_called_once_with("项目A", "/path/main")


# ══════════════════════════════════════════════════════════
# 4. restore_main_repo - 幂等性
# ══════════════════════════════════════════════════════════


class TestRestoreMainRepo:
    """测试 restore_main_repo 行为"""

    def test_idempotent_when_not_in_worktree(self, ctx):
        """不在 worktree 中应跳过"""
        ctx._workdir_cache["项目A"] = "/path/main"
        # 不是 worktree（patch detector）
        from unittest.mock import patch

        with patch("app.utils.git_worktree.GitWorktreeDetector") as mock_detector:
            mock_detector.is_worktree.return_value = False
            result = ctx.restore_main_repo("项目A")
            assert result is False


# ══════════════════════════════════════════════════════════
# 5. clear_workdir
# ══════════════════════════════════════════════════════════


class TestClearWorkdir:
    """测试 clear_workdir 行为"""

    def test_clears_cache(self, ctx, mock_backend):
        """应清除实例缓存"""
        ctx.bind_root("项目A", "/path/root")
        ctx.clear_workdir("项目A")
        assert ctx.get_workdir("项目A") is None

    def test_calls_tool_executor_none(self, ctx, mock_backend):
        """应调用 tool_executor.set_workdir(None)"""
        ctx.bind_root("项目A", "/path/root")
        mock_backend.tool_executor.set_workdir.reset_mock()
        ctx.clear_workdir("项目A")
        mock_backend.tool_executor.set_workdir.assert_called_with(None)

    def test_emits_workdir_changed_empty(self, ctx):
        """应触发 workdir_changed，携带空字符串"""
        ctx.bind_root("项目A", "/path/root")
        counter = SignalCounter(ctx.workdir_changed)
        ctx.clear_workdir("项目A")
        assert counter.last_args == ("项目A", "")


# ══════════════════════════════════════════════════════════
# 6. get_workdir / get_effective_workdir
# ══════════════════════════════════════════════════════════


class TestGetWorkdir:
    """测试 workdir 查询的优先级"""

    def test_cache_priority_over_db(self, ctx, mock_backend):
        """实例缓存应优先于 DB"""
        ctx._workdir_cache["项目A"] = "/cache/path"
        mock_backend.memory_manager.get_working_directory.return_value = "/db/path"
        assert ctx.get_effective_workdir("项目A") == "/cache/path"

    def test_db_fallback(self, ctx, mock_backend):
        """无缓存时从 DB 读取"""
        mock_backend.memory_manager.get_working_directory.return_value = "/db/path"
        result = ctx.get_effective_workdir("项目A")
        assert result == "/db/path"

    def test_db_fallback_writes_to_cache(self, ctx, mock_backend):
        """DB 回退时应同时写入缓存"""
        mock_backend.memory_manager.get_working_directory.return_value = "/db/path"
        ctx.get_effective_workdir("项目A")
        assert ctx._workdir_cache["项目A"] == "/db/path"

    def test_no_data_returns_none(self, ctx, mock_backend):
        """缓存+DB 都没有时返回 None"""
        assert ctx.get_effective_workdir("项目A") is None

    def test_empty_cache_value_returns_none(self, ctx):
        """缓存值为空字符串时返回 None"""
        ctx._workdir_cache["项目A"] = ""
        assert ctx.get_effective_workdir("项目A") is None


# ══════════════════════════════════════════════════════════
# 7. ensure_workdir_loaded
# ══════════════════════════════════════════════════════════


class TestEnsureWorkdirLoaded:
    """测试 ensure_workdir_loaded 行为"""

    def test_returns_cache_if_present(self, ctx):
        """缓存命中时直接返回"""
        ctx._workdir_cache["项目A"] = "/cache/path"
        assert ctx.ensure_workdir_loaded("项目A") == "/cache/path"

    def test_loads_from_db_when_cache_empty(self, ctx, mock_backend):
        """缓存空时从 DB 加载并写入缓存"""
        mock_backend.memory_manager.get_working_directory.return_value = "/db/path"
        result = ctx.ensure_workdir_loaded("项目A")
        assert result == "/db/path"
        assert ctx._workdir_cache["项目A"] == "/db/path"

    def test_syncs_tool_executor_on_db_load(self, ctx, mock_backend):
        """DB 加载时应同步到 tool_executor"""
        mock_backend.memory_manager.get_working_directory.return_value = "/db/path"
        mock_backend.tool_executor.set_workdir.reset_mock()
        ctx.ensure_workdir_loaded("项目A")
        mock_backend.tool_executor.set_workdir.assert_called_with("/db/path")


# ══════════════════════════════════════════════════════════
# 8. 多窗口隔离
# ══════════════════════════════════════════════════════════


class TestMultiWindowIsolation:
    """测试多窗口 ProjectContext 独立缓存"""

    def test_independent_caches(self, mock_backend):
        """两个 ProjectContext 实例应有独立缓存"""
        ctx1 = ProjectContext(backend=mock_backend, window_id="w1")
        ctx2 = ProjectContext(backend=mock_backend, window_id="w2")

        ctx1.bind_root("项目A", "/path/window1")

        assert ctx1.get_workdir("项目A") == "/path/window1"
        assert ctx2.get_workdir("项目A") is None

    def test_sync_from_instance_cache(self, mock_backend):
        """_sync_from_instance_cache 应正确复制外部缓存"""
        parent_ctx = ProjectContext(backend=mock_backend, window_id="parent")
        parent_ctx.bind_root("项目A", "/path/root")
        parent_ctx.bind_root("项目B", "/path/b")

        new_ctx = ProjectContext(backend=mock_backend, window_id="new")
        new_ctx._sync_from_instance_cache(parent_ctx._workdir_cache)

        assert new_ctx.get_workdir("项目A") == "/path/root"
        assert new_ctx.get_workdir("项目B") == "/path/b"

    def test_sync_skips_empty_values(self, mock_backend):
        """_sync_from_instance_cache 应跳过空值"""
        ctx = ProjectContext(backend=mock_backend, window_id="test")
        ctx._sync_from_instance_cache({"项目A": "/path/a", "项目B": ""})
        assert ctx.get_workdir("项目A") == "/path/a"
        assert ctx.get_workdir("项目B") is None


# ══════════════════════════════════════════════════════════
# 9. set_current_project - 静默设置
# ══════════════════════════════════════════════════════════


class TestSetCurrentProject:
    """测试 set_current_project 静默设置（不触发信号）"""

    def test_updates_internal_state_without_signal(self, ctx):
        """set_current_project 不应触发任何信号"""
        counter = SignalCounter(ctx.project_changed)
        ctx.set_current_project("项目A")
        assert ctx.get_current_project() == "项目A"
        assert counter.count == 0


# ══════════════════════════════════════════════════════════
# 10. 边界场景
# ══════════════════════════════════════════════════════════


class TestEdgeCases:
    """边界场景"""

    def test_no_backend(self):
        """backend=None 时不应崩溃"""
        ctx = ProjectContext(backend=None, window_id="test")
        # bind_root 在 backend=None 时应该只更新缓存，DB 操作被跳过
        result = ctx.bind_root("项目A", "/path/root")
        assert result is True  # 不抛异常就算成功
        assert ctx.get_workdir("项目A") == "/path/root"

    def test_no_memory_manager(self, mock_backend):
        """backend 没有 memory_manager 时不应崩溃"""
        mock_backend.memory_manager = None
        ctx = ProjectContext(backend=mock_backend, window_id="test")
        result = ctx.bind_root("项目A", "/path/root")
        # tool_executor 仍然会被调用
        mock_backend.tool_executor.set_workdir.assert_called_once()
        assert result is True

    def test_no_tool_executor(self, mock_backend):
        """backend 没有 tool_executor 时不应崩溃"""
        mock_backend.tool_executor = None
        ctx = ProjectContext(backend=mock_backend, window_id="test")
        result = ctx.bind_root("项目A", "/path/root")
        # DB 操作正常
        mock_backend.memory_manager.add_key_document.assert_called_once()
        assert result is True

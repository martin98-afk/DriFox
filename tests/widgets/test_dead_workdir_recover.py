# -*- coding: utf-8 -*-
"""失效工作目录回落主仓库根回归守卫

场景：会话曾在 ``<主仓库>/.worktrees/<名>`` 下工作，工作树被删后
``project_workdir`` 表与实例缓存里仍是死路径 —— 工作树页显示「工作目录不存在」，
文件工具指向不存在的目录。修复口径：解析工作目录时判活，失效且能推断主仓库
根则回落到主树并写回缓存/DB。

覆盖：
1. 活目录原样返回，不写 DB
2. 失效 ``.worktrees/<名>`` → 回主树，缓存与 DB 同步更新
3. 空值原样返回
4. 非 ``.worktrees`` 形态的死路径不动（可能是移动硬盘/网络盘临时不可用）
5. ``.worktrees`` 形态但主仓库也不在了 → 不动
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.main_widget import OpenAIChatToolWindow


def _make_window(current_workdir=None, memory_manager=None):
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._current_workdir = dict(current_workdir or {})
    backend = MagicMock()
    backend.memory_manager = memory_manager if memory_manager is not None else MagicMock()
    win.backend = backend
    return win


class TestRecoverDeadWorkdir:
    def test_alive_dir_kept_and_no_db_write(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        win = _make_window({"P": str(work)})
        assert win._recover_dead_workdir("P", str(work)) == str(work)
        win.backend.memory_manager.set_working_directory.assert_not_called()

    def test_dead_worktree_falls_back_to_main(self, tmp_path):
        main = tmp_path / "app"
        main.mkdir()
        dead = main / ".worktrees" / "gone"
        win = _make_window({"P": str(dead)})

        recovered = win._recover_dead_workdir("P", str(dead))

        assert Path(recovered) == main
        assert win._current_workdir["P"] == recovered, "实例缓存必须同步，否则下次又读死路径"
        win.backend.memory_manager.set_working_directory.assert_called_once_with("P", recovered)

    def test_empty_workdir_returned_as_is(self):
        win = _make_window()
        assert win._recover_dead_workdir("P", None) is None
        assert win._recover_dead_workdir("P", "") == ""
        win.backend.memory_manager.set_working_directory.assert_not_called()

    def test_dead_plain_dir_untouched(self, tmp_path):
        """非 .worktrees 形态的死路径（他机路径/未挂载盘）不猜，保持用户设置"""
        dead = str(tmp_path / "never-existed")
        win = _make_window({"P": dead})
        assert win._recover_dead_workdir("P", dead) == dead
        win.backend.memory_manager.set_working_directory.assert_not_called()

    def test_dead_worktree_without_main_repo_untouched(self, tmp_path):
        dead = str(tmp_path / "gone-main" / ".worktrees" / "feat")
        win = _make_window({"P": dead})
        assert win._recover_dead_workdir("P", dead) == dead
        win.backend.memory_manager.set_working_directory.assert_not_called()

    def test_db_write_failure_does_not_raise(self, tmp_path):
        """持久层写失败不影响本次回落（缓存已改，本次会话可正常用）"""
        main = tmp_path / "app"
        main.mkdir()
        dead = main / ".worktrees" / "gone"
        mm = MagicMock()
        mm.set_working_directory.side_effect = RuntimeError("db locked")
        win = _make_window({"P": str(dead)}, memory_manager=mm)

        recovered = win._recover_dead_workdir("P", str(dead))

        assert Path(recovered) == main
        assert win._current_workdir["P"] == recovered


if __name__ == "__main__":
    pytest.main([__file__, "-q"])

# -*- coding: utf-8 -*-
"""memory_manager 层 workdir 路由测试"""

import pytest


class TestSqliteRouting:
    """无 workdir → 走 SQLite"""

    def test_get_or_create_uses_initial_template_when_no_workdir(self, patched_memory_manager):
        mgr = patched_memory_manager
        # workdir 默认为 None
        note = mgr.get_or_create_project_note("demo")
        assert note["content"]  # 非空
        assert note["path"] == ""  # 没文件路径
        # 内容应该是模板
        from app.core.project_notes_manager import INITIAL_TEMPLATE
        assert note["content"] == INITIAL_TEMPLATE

    def test_save_then_get_roundtrips_via_sqlite(self, patched_memory_manager):
        mgr = patched_memory_manager
        content = "# my note\nhello"
        assert mgr.save_project_note("demo", content) is True
        note = mgr.get_or_create_project_note("demo")
        assert note["content"] == content


class TestFileRoutingMigration:
    """有 workdir 无 AGENTS.md → 从 SQLite 迁或写默认模板"""

    def test_migrates_from_sqlite_when_file_missing(self, patched_memory_manager, tmp_workdir):
        mgr = patched_memory_manager
        # 1) 先在 SQLite 写入自定义内容(模拟无 workdir 时期的笔记)
        mgr.save_project_note("demo", "# my custom note\nlegacy content")
        # 2) 模拟设置 workdir(用 mock 替换 get_working_directory)
        mgr._key_documents_repo.get_working_directory.return_value = str(tmp_workdir)
        # 3) 首次访问应触发迁移
        note = mgr.get_or_create_project_note("demo")
        assert note["content"] == "# my custom note\nlegacy content"
        # 4) 文件确实被创建
        assert (tmp_workdir / "AGENTS.md").exists()
        assert (tmp_workdir / "AGENTS.md").read_text(encoding="utf-8") == "# my custom note\nlegacy content"

    def test_writes_initial_template_when_no_sqlite_content(self, patched_memory_manager, tmp_workdir):
        mgr = patched_memory_manager
        # SQLite 无任何记录
        mgr._key_documents_repo.get_working_directory.return_value = str(tmp_workdir)
        note = mgr.get_or_create_project_note("fresh_project")
        from app.core.project_notes_manager import INITIAL_TEMPLATE
        assert note["content"] == INITIAL_TEMPLATE
        assert (tmp_workdir / "AGENTS.md").exists()


class TestFileExistsNotOverwritten:
    """workdir 下 AGENTS.md 已存在 → 尊重原内容,不覆盖"""

    def test_existing_agents_md_not_overwritten(self, patched_memory_manager, tmp_workdir):
        mgr = patched_memory_manager
        # 1) 预先在 workdir 放一份用户自定义 AGENTS.md
        existing = "# 我的项目\n这是用户在文件里手写的笔记"
        (tmp_workdir / "AGENTS.md").write_text(existing, encoding="utf-8")
        # 2) SQLite 也有内容(理论上不应被使用)
        mgr.save_project_note("demo", "# sqlite content (should be ignored)")
        # 3) 模拟设置 workdir
        mgr._key_documents_repo.get_working_directory.return_value = str(tmp_workdir)
        # 4) 首次访问
        note = mgr.get_or_create_project_note("demo")
        # 5) 必须返回文件原内容,不是 sqlite
        assert note["content"] == existing
        # 6) 文件未被改写
        assert (tmp_workdir / "AGENTS.md").read_text(encoding="utf-8") == existing


class TestWorkdirSwitch:
    """workdir 切换 A → B (B 无文件) → 从 SQLite 迁"""

    def test_switching_workdir_triggers_migration_from_sqlite(self, patched_memory_manager, tmp_path):
        mgr = patched_memory_manager
        # 1) A workdir 下有文件(模拟切换前的工作状态)
        workdir_a = tmp_path / "A"
        workdir_a.mkdir()
        (workdir_a / "AGENTS.md").write_text("# A 的笔记", encoding="utf-8")
        # 2) SQLite 有内容(可能是另一个项目的旧笔记)
        mgr.save_project_note("demo", "# from sqlite (legacy)")
        # 3) 切换到 B workdir
        workdir_b = tmp_path / "B"
        workdir_b.mkdir()
        mgr._key_documents_repo.get_working_directory.return_value = str(workdir_b)
        # 4) 访问项目笔记
        note = mgr.get_or_create_project_note("demo")
        # 5) B 下应被写入迁移内容(SQLite 的旧值)
        assert note["content"] == "# from sqlite (legacy)"
        assert (workdir_b / "AGENTS.md").read_text(encoding="utf-8") == "# from sqlite (legacy)"
        # 6) A 下的文件保留,不主动删除
        assert (workdir_a / "AGENTS.md").read_text(encoding="utf-8") == "# A 的笔记"

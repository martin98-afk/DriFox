# -*- coding: utf-8 -*-
"""
memory_manager.save_project_note 空内容保护回归测试

Bug 复现: AGENTS.md 偶尔被清空成 0 字节
根因:     save_project_note(content="") 直接 write_text 写入,无空内容检查
          触发链: UI 编辑器被全选删除 → 300ms 防抖自动保存 → 写入空字符串

修复:     save_project_note 加 `if not (content or "").strip(): return False`
          阻止空内容/纯空白覆盖已有 AGENTS.md

测试覆盖:
1. 空字符串 "" → 返回 False,磁盘内容不变
2. 纯空白 "   \n\t  " → 返回 False,磁盘内容不变
3. None content → 返回 False,磁盘内容不变
4. 有效内容 → 正常写入,返回 True
5. 无 workdir → 返回 False(原有行为)
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.core.memory_manager import MemoryManagerCore


@pytest.fixture
def mgr_with_workdir(tmp_path: Path):
    """构造一个带 mock workdir 的 MemoryManagerCore(不依赖 SQLite)"""
    mgr = MemoryManagerCore.__new__(MemoryManagerCore)
    mgr._key_documents_repo = MagicMock()
    mgr._key_documents_repo.get_working_directory.return_value = str(tmp_path)
    mgr._db_manager = None
    mgr._entry_memories_repo = None
    return mgr


@pytest.fixture
def seeded_agents_md(tmp_path: Path) -> Path:
    """预置一份有内容的 AGENTS.md,模拟用户已写好的笔记"""
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        "# 我的项目笔记\n这是用户辛苦写的笔记内容\n- 目标\n- 边界\n",
        encoding="utf-8",
    )
    return agents


class TestEmptyContentProtection:
    """save_project_note 必须拒绝空内容/纯空白,保护已有 AGENTS.md"""

    def test_empty_string_does_not_overwrite(self, mgr_with_workdir, seeded_agents_md):
        """空字符串 '' 不能清空文件"""
        original = seeded_agents_md.read_text(encoding="utf-8")

        result = mgr_with_workdir.save_project_note("demo", "", workdir=str(seeded_agents_md.parent))

        assert result is False
        assert seeded_agents_md.read_text(encoding="utf-8") == original, "空字符串写入后文件被破坏!"
        assert seeded_agents_md.stat().st_size > 0, "文件被清空成 0 字节!"

    def test_whitespace_only_does_not_overwrite(self, mgr_with_workdir, seeded_agents_md):
        """纯空白(空格/换行/Tab)不能清空文件"""
        original = seeded_agents_md.read_text(encoding="utf-8")

        for content in ["   ", "\n\n", "\t\t", "  \n  \t  \n", "\r\n\r\n"]:
            result = mgr_with_workdir.save_project_note("demo", content, workdir=str(seeded_agents_md.parent))
            assert result is False, f"纯空白 {content!r} 应该被拒绝"
            assert seeded_agents_md.read_text(encoding="utf-8") == original, f"纯空白 {content!r} 写入后文件被破坏!"

    def test_none_content_does_not_overwrite(self, mgr_with_workdir, seeded_agents_md):
        """None content 不应崩溃,也不应清空文件"""
        original = seeded_agents_md.read_text(encoding="utf-8")

        result = mgr_with_workdir.save_project_note("demo", None, workdir=str(seeded_agents_md.parent))

        assert result is False
        assert seeded_agents_md.read_text(encoding="utf-8") == original


class TestValidContentStillWorks:
    """有效内容的正常保存路径不能被破坏"""

    def test_valid_content_saves(self, mgr_with_workdir, tmp_path: Path):
        """正常内容应该被写入"""
        agents = tmp_path / "AGENTS.md"
        # 模拟首次创建(文件不存在)
        assert not agents.exists()

        result = mgr_with_workdir.save_project_note("demo", "# 新笔记\n内容", workdir=str(tmp_path))

        assert result is True
        assert agents.read_text(encoding="utf-8") == "# 新笔记\n内容"

    def test_valid_content_overwrites_existing(self, mgr_with_workdir, seeded_agents_md):
        """正常新内容应该覆盖已有内容(合法覆盖)"""
        new_content = "# 全新内容\n完全替换"

        result = mgr_with_workdir.save_project_note("demo", new_content, workdir=str(seeded_agents_md.parent))

        assert result is True
        assert seeded_agents_md.read_text(encoding="utf-8") == new_content

    def test_single_newline_is_empty(self, mgr_with_workdir, seeded_agents_md):
        """边界:单个换行 '\\n' 算空内容(被 strip)"""
        original = seeded_agents_md.read_text(encoding="utf-8")

        result = mgr_with_workdir.save_project_note("demo", "\n", workdir=str(seeded_agents_md.parent))

        assert result is False
        assert seeded_agents_md.read_text(encoding="utf-8") == original


class TestNoWorkdirStillReturnsFalse:
    """原有行为不能破坏:无 workdir 时仍返回 False"""

    def test_no_workdir_returns_false(self, mgr_with_workdir, tmp_path: Path):
        """workdir 为空时不能写入"""
        mgr_with_workdir._key_documents_repo.get_working_directory.return_value = None

        result = mgr_with_workdir.save_project_note("demo", "内容", workdir=None)

        assert result is False
        assert not (tmp_path / "AGENTS.md").exists()

# -*- coding: utf-8 -*-
"""
shell_compressor.py 回归测试

主要覆盖 git status --short 格式压缩（issue: --short 输出被压缩成 "?"）。
"""

import sys
from pathlib import Path

# 仓库根目录加入 sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# shell_compressor 随工具插件存放（工具插件化），从插件路径加载
import importlib.util  # noqa: E402

_plugin_path = _REPO_ROOT / "plugins" / "system" / "tools" / "_shell_compressor.py"
_spec = importlib.util.spec_from_file_location("_shell_compressor", _plugin_path)
_shell_compressor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_shell_compressor)
compress_output = _shell_compressor.compress_output  # noqa: E402
_compress_git_status = _shell_compressor._compress_git_status  # noqa: E402
_compress_git_status_short = _shell_compressor._compress_git_status_short  # noqa: E402


# ============================================================================
# 回归测试：用户真实场景
# ============================================================================

class TestGitStatusShortRegression:
    """git status --short 输出不再被压缩成 '?'"""

    REAL_OUTPUT = (
        " M app/main_widget.py\n"
        " M app/widgets/cards/settings/hook_setting_card.py\n"
        " M app/widgets/cards/settings/list_setting_card.py\n"
        " M app/widgets/cards/settings/llm_settings_card.py\n"
        " M app/widgets/cards/settings/provider_setting_card.py\n"
        "?? scratch_search.py\n"
        "?? uv.lock\n"
    )

    def test_short_status_is_not_question_mark(self):
        """核心 bug 修复：--short 输出不能再变成 '?'"""
        result = compress_output("git status --short", self.REAL_OUTPUT)
        assert result != "?", f"压缩结果是 '?'，bug 未修复: {result!r}"

    def test_short_status_via_internal_helper(self):
        """直接调用内部函数验证 - 必须传未 stripped 行，否则 X=' ' 状态被吞"""
        # 注意：必须 split('\\n') 不 strip，否则 ' M' 的 X=' ' 会变成 'M'
        lines = self.REAL_OUTPUT.split('\n')
        result = _compress_git_status_short(lines)
        assert result != "?"
        assert "\nstaged:" not in result  # 全部是 " M"（仅工作区 modified），不应有独立 staged 行
        assert "\nunstaged:" in result

    def test_short_status_lists_all_unstaged_files(self):
        """5 个 modified 文件：超过阈值 4 时显示前 4 + '+N more' 标记"""
        result = compress_output("git status --short", self.REAL_OUTPUT)
        assert result is not None
        # 前 4 个完整显示
        for fname in (
            "app/main_widget.py",
            "app/widgets/cards/settings/hook_setting_card.py",
            "app/widgets/cards/settings/list_setting_card.py",
            "app/widgets/cards/settings/llm_settings_card.py",
        ):
            assert fname in result, f"缺失文件: {fname}\n实际输出: {result}"
        # 截断标记
        assert "(+1 more)" in result, f"应有截断标记\n实际输出: {result}"

    def test_short_status_lists_untracked_files(self):
        """2 个 untracked 文件必须出现在 untracked 段"""
        result = compress_output("git status --short", self.REAL_OUTPUT)
        assert result is not None
        assert "scratch_search.py" in result
        assert "uv.lock" in result
        assert "untracked:" in result

    def test_short_status_branch_placeholder(self):
        """--short 无分支信息，使用 '-' 占位"""
        result = compress_output("git status --short", self.REAL_OUTPUT)
        assert result is not None
        # 第一行是分支占位
        first_line = result.split('\n')[0]
        assert first_line == "-", f"分支占位应为 '-'，实际: {first_line!r}"

    def test_short_status_compresses_better_than_passthrough(self):
        """压缩结果应短于原始输出"""
        result = compress_output("git status --short", self.REAL_OUTPUT)
        assert result is not None
        assert len(result) < len(self.REAL_OUTPUT), (
            f"压缩后更长: original={len(self.REAL_OUTPUT)}, compressed={len(result)}"
        )


# ============================================================================
# 状态码全覆盖
# ============================================================================

class TestGitStatusShortCodes:
    """git status --short 两列状态码的正确分类"""

    def test_added(self):
        """A = added to staging"""
        result = _compress_git_status_short(["A  new_file.py"])
        assert "+new_file.py" in result
        assert "staged:" in result

    def test_deleted_in_staging(self):
        """D (第 1 列) = deleted from staging"""
        result = _compress_git_status_short(["D  removed.py"])
        assert "-removed.py" in result
        assert "staged:" in result

    def test_renamed(self):
        """R = renamed in staging"""
        result = _compress_git_status_short(["R  old.py -> new.py"])
        assert "->old.py -> new.py" in result or "->new.py" in result
        assert "staged:" in result

    def test_untracked(self):
        """?? = untracked"""
        result = _compress_git_status_short(["?? untracked.txt"])
        assert "untracked.txt" in result
        assert "untracked:" in result
        assert "staged:" not in result

    def test_ignored(self):
        """!! = ignored"""
        result = _compress_git_status_short(["!! ignored.log"])
        assert "!ignored.log" in result
        assert "untracked:" in result

    def test_both_modified(self):
        """MM = staged and worktree both modified → 同时进入 staged 和 unstaged"""
        result = _compress_git_status_short(["MM conflict.py"])
        assert "staged:" in result
        assert "unstaged:" in result
        # conflict.py 应在两个段中各出现一次
        assert result.count("~conflict.py") == 2

    def test_worktree_deleted(self):
        """空格 + D = worktree deletion only（仅工作区删除）"""
        # 注意：行必须以 " D" 开头（X=' '），不能 strip
        result = _compress_git_status_short([" D gone.py"])
        assert "-gone.py" in result
        assert "\nunstaged:" in result
        assert "\nstaged:" not in result


# ============================================================================
# 长格式回归保护
# ============================================================================

class TestGitStatusLongFormatRegression:
    """长格式 git status 不能被 --short 分支误判"""

    LONG_OUTPUT = (
        "On branch dev\n"
        "Your branch is ahead of 'origin/dev' by 1 commit.\n"
        "  (use \"git push\" to publish your local commits)\n"
        "\n"
        "Changes to be committed:\n"
        "  (use \"git restore --staged <file>...\" to unstage)\n"
        "        modified:   app/main_widget.py\n"
        "        new file:   app/new_feature.py\n"
        "\n"
        "Changes not staged for commit:\n"
        "  (use \"git add <file>...\" to update what will be committed)\n"
        "        modified:   app/widgets/cards/settings/hook_setting_card.py\n"
        "\n"
        "Untracked files:\n"
        "  (use \"git add <file>...\" to include in what will be committed)\n"
        "        scratch_search.py\n"
    )

    def test_long_format_still_parsed_correctly(self):
        """长格式 git status 输出仍能被正确解析（不被 --short 分支误判）

        注意：长格式输出格式是 '~:   filename'（前缀 '~:' + 多个空格 + 文件名），
        这是原代码的既有实现风格（`f"~{f}"`，其中 `f` 是 strip 后的但保留了前导空格）。
        本测试不修这个既有 bug，只确认长格式不被破坏。
        """
        result = compress_output("git status", self.LONG_OUTPUT)
        assert result is not None
        assert result != "?"
        assert "dev" in result  # 分支名
        assert "app/main_widget.py" in result  # staged modified 文件名
        assert "+app/new_feature.py" in result  # staged added 文件名（带 + 前缀）
        assert "\nstaged:" in result
        assert "\nunstaged:" in result
        assert "scratch_search.py" in result  # untracked

    def test_long_format_not_routed_to_short_handler(self):
        """长格式第一行是 'On branch'，不应匹配 --short 分支"""
        result = _compress_git_status(self.LONG_OUTPUT)
        assert result.startswith("dev")
        assert result != "-"

    def test_clean_long_format(self):
        """完全干净的长格式输出 clean 标记"""
        clean_output = "On branch main\nnothing to commit, working tree clean\n"
        result = compress_output("git status", clean_output)
        assert result is not None
        assert "clean" in result


# ============================================================================
# 边界情况
# ============================================================================

class TestGitStatusShortEdgeCases:
    """边界情况"""

    def test_empty_output(self):
        """空输出应不崩溃"""
        result = compress_output("git status --short", "")
        # 压缩后应至少是可读字符串，不应抛异常
        assert isinstance(result, str)

    def test_only_whitespace(self):
        """仅空白行"""
        result = compress_output("git status --short", "\n\n\n")
        assert isinstance(result, str)

    def test_filename_with_spaces(self):
        """文件名含空格（split(None, 1) 应正确切分）"""
        result = _compress_git_status_short([" M my file with spaces.py"])
        assert "my file with spaces.py" in result
        assert "unstaged:" in result

    def test_porcelain_v1_alias(self):
        """--porcelain 是 --short 的别名，行为应一致"""
        output = " M file.py\n?? new.py\n"
        r1 = compress_output("git status --short", output)
        r2 = compress_output("git status --porcelain", output)
        assert r1 == r2

    def test_short_helper_returns_dash_for_branch(self):
        """辅助函数默认分支占位为 '-'"""
        result = _compress_git_status_short([])
        # 空输入应只输出 "-"
        assert result == "-"


# ============================================================================
# 集成测试：通过 compress_output 完整链路
# ============================================================================

class TestCompressOutputIntegration:
    """验证完整链路 classify → 路由 → 压缩"""

    def test_short_status_classified_as_compress(self):
        """git status --short 应被分类为 compress"""
        classify = _shell_compressor.classify

        assert classify("git status --short") == "compress"
        assert classify("git status --porcelain") == "compress"
        assert classify("git status") == "compress"

    def test_full_pipeline_token_savings(self):
        """完整流程：压缩不再返回 '?' 且包含所有关键信息

        短列表压缩可能不省 token（路径短 + 前缀反而更长），但核心价值是
        把可读性差的状态码变成结构化分段。
        """
        output = (
            " M app/main_widget.py\n"
            " M app/widgets/cards/settings/hook_setting_card.py\n"
            "?? scratch_search.py\n"
        )
        result = compress_output("git status --short", output)
        assert result is not None
        assert result != "?"
        # 关键信息保留
        assert "app/main_widget.py" in result
        assert "app/widgets/cards/settings/hook_setting_card.py" in result
        assert "scratch_search.py" in result
        assert "\nunstaged:" in result
        assert "\nuntracked:" in result


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
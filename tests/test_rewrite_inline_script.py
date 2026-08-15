# -*- coding: utf-8 -*-
"""
测试 _rewrite_inline_script 和 _parse_inline_script

主要验证：
1. 不含换行/嵌套引号的简单内联脚本不触发 rewrite
2. 含换行的多行脚本触发 rewrite，路径正确加引号
3. \" 转义序列写入临时文件前被反转义
4. 含空格临时目录路径正确加引号
"""

import importlib.util
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# 工具插件化：app/tools/terminal_tools.py 已迁移为 plugins/system/tools/terminal_tools.py
# （主程序 fallback 已移除）。plugins/ 非 Python 包，用 _load_module 模式加载插件模块
# （复用 test_file_tree_root_watch.py 的做法）。
_PLUGIN_TOOLS = Path(__file__).resolve().parent.parent / "plugins" / "system" / "tools"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_terminal_tools = _load_module("_rewrite_inline_script_terminal", _PLUGIN_TOOLS / "terminal_tools.py")
_parse_inline_script = _terminal_tools._parse_inline_script
_rewrite_inline_script = _terminal_tools._rewrite_inline_script
_cleanup_script_temp = _terminal_tools._cleanup_script_temp


# ========================================================================
# Helper
# ========================================================================


def _run_path_a(command: str):
    """模拟 Path A (shell=False) 的执行"""
    args = shlex.split(command)
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    stdout, stderr = proc.communicate(timeout=15)
    return proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")


# ========================================================================
# _parse_inline_script 测试
# ========================================================================


class TestParseInlineScript:
    def test_simple_double_quote(self):
        """基础双引号包裹"""
        r = _parse_inline_script('python -c "print(1)"')
        assert r is not None
        assert r["interpreter"] == "python"
        assert r["flag"] == "-c"
        assert r["script"] == "print(1)"
        assert r["outer_quote"] == '"'

    def test_simple_single_quote(self):
        """基础单引号包裹"""
        r = _parse_inline_script("python -c 'print(1)'")
        assert r is not None
        assert r["script"] == "print(1)"
        assert r["outer_quote"] == "'"

    def test_multi_line_script(self):
        """多行脚本"""
        cmd = 'python -c "' + "\nprint(1)\n" + '"'
        r = _parse_inline_script(cmd)
        assert r is not None
        assert "\n" in r["script"]

    def test_escaped_quotes(self):
        """脚本内含 \\" 转义引号"""
        r = _parse_inline_script('python -c "print(\\"hello\\")"')
        assert r is not None
        assert r["script"] == 'print(\\"hello\\")'
        assert r["outer_quote"] == '"'

    def test_different_quote_inside(self):
        """脚本内用不同类型引号"""
        r = _parse_inline_script("python -c \"print('hello')\"")
        assert r is not None
        assert r["script"] == "print('hello')"

    def test_no_match_not_inline(self):
        """不是 -c/-e 内联脚本"""
        assert _parse_inline_script("python --version") is None
        assert _parse_inline_script("node --help") is None

    def test_no_match_unknown_interpreter(self):
        """未知解释器"""
        assert _parse_inline_script("gcc -c test.c") is None

    def test_with_remaining_args(self):
        """脚本后有额外参数"""
        r = _parse_inline_script('python -c "print(1)" arg1 arg2')
        assert r is not None
        assert r["script"] == "print(1)"
        assert r["rest"] == "arg1 arg2"


# ========================================================================
# _rewrite_inline_script 测试（核心）
# ========================================================================


class TestRewriteInlineScript:
    def test_simple_not_rewritten(self):
        """简单内联脚本不触发 rewrite"""
        cmd = 'python -c "print(1)"'
        new_cmd, tmp_path = _rewrite_inline_script(cmd)
        assert new_cmd == cmd
        assert tmp_path is None

    def test_escaped_quotes_not_rewritten_anymore(self):
        """
        [关键修复] \\" 引号嵌套不再触发 rewrite
        Path A (shell=False) 已能正确处理，走 rewrite 反而引入 SyntaxError
        """
        cmd = 'python -c "print(\\"hello\\")"'
        new_cmd, tmp_path = _rewrite_inline_script(cmd)
        assert new_cmd == cmd, "不应触发 rewrite，Path A 已能正确处理"
        assert tmp_path is None

    def test_different_quotes_not_rewritten(self):
        """不同引号类型不触发 rewrite"""
        cmd = "python -c \"print('hello')\""
        new_cmd, tmp_path = _rewrite_inline_script(cmd)
        assert new_cmd == cmd
        assert tmp_path is None

    def test_multi_line_rewritten(self):
        """多行脚本触发 rewrite"""
        # 使用单引号作为 Python 字符串引号（与外层双引号区分）
        cmd = 'python -c "' + "\nprint('hello world')\n" + '"'
        new_cmd, tmp_path = _rewrite_inline_script(cmd)
        assert tmp_path is not None, "多行脚本应触发 rewrite"
        assert new_cmd != cmd
        # 验证临时文件存在
        assert os.path.exists(tmp_path), "临时文件应存在"
        # 验证路径被正确引号包裹
        assert '"' in new_cmd, "路径应被引号包裹"
        # 验证内容正确
        with open(tmp_path, encoding="utf-8") as f:
            content = f.read()
        assert "print('hello world')" in content, "脚本内容应正确"
        # 清理
        _cleanup_script_temp(tmp_path)

    def test_multi_line_with_escaped_quotes(self):
        """
        [关键修复] 多行 + \" 转义
        写入临时文件前应反转义 \" → "
        """
        cmd = 'python -c "' + '\nprint(\\"hello\\")\n' + '"'
        new_cmd, tmp_path = _rewrite_inline_script(cmd)
        assert tmp_path is not None, "多行应触发 rewrite"
        # 读取临时文件内容
        with open(tmp_path, encoding="utf-8") as f:
            content = f.read()
        # \" 应被反转义为 "
        assert '"hello"' in content, f'\\" 应被反转义为 "，实际内容: {repr(content)}'
        assert '\\"' not in content, f'不应残留 \\"，实际内容: {repr(content)}'
        _cleanup_script_temp(tmp_path)

    def test_path_with_quotes(self):
        """[关键修复] 临时文件路径应被引号包裹"""
        cmd = 'python -c "' + "\nprint(1)\n" + '"'
        new_cmd, tmp_path = _rewrite_inline_script(cmd)
        assert tmp_path is not None
        # 验证路径部分被引号包裹：python "C:/path/to/file.py"
        interpreter = cmd.split(None, 2)[0]
        # 从 new_cmd 中提取路径部分
        after_interpreter = new_cmd[len(interpreter) :].strip()
        assert after_interpreter.startswith('"'), f"路径应以引号开头: {new_cmd}"
        _cleanup_script_temp(tmp_path)

    def test_remaining_args_preserved(self):
        """额外参数应保留"""
        script = '\nprint("hello")\n'
        cmd = f'python -c "{script}" arg1 "arg2 with space"'
        new_cmd, tmp_path = _rewrite_inline_script(cmd)
        assert tmp_path is not None
        # 额外参数应在新命令中
        assert "arg1" in new_cmd
        assert "arg2 with space" in new_cmd
        _cleanup_script_temp(tmp_path)

    def test_nodejs_multi_line(self):
        """Node.js 多行脚本"""
        # 使用单引号作为 JS 字符串引号（与外层双引号区分）
        script = "\nconsole.log('hello')\n"
        cmd = f'node -e "{script}"'
        new_cmd, tmp_path = _rewrite_inline_script(cmd)
        assert tmp_path is not None
        assert tmp_path.endswith(".js"), "Node 脚本扩展名应为 .js"
        with open(tmp_path, encoding="utf-8") as f:
            content = f.read()
        assert "console.log('hello')" in content
        _cleanup_script_temp(tmp_path)


# ========================================================================
# 集成测试 - 实际执行验证
# ========================================================================


class TestRewriteIntegration:
    """验证 rewrite 后的命令能正确执行"""

    def test_multi_line_python_execution(self):
        """多行 Python 脚本 rewrite 后能正确执行"""
        cmd = 'python -c "' + "\nimport sys\nprint(sys.argv[0])\n" + '"'
        new_cmd, tmp_path = _rewrite_inline_script(cmd)
        assert tmp_path is not None
        try:
            rc, stdout, stderr = _run_path_a(new_cmd)
            assert rc == 0, f"执行失败: {stderr}"
            # 输出应为临时文件名（argv[0] = 脚本路径）
            assert stdout.strip(), "应有输出"
        finally:
            _cleanup_script_temp(tmp_path)

    def test_multi_line_with_quotes_execution(self):
        """多行脚本（不同引号类型）rewrite 后能正确执行"""
        cmd = 'python -c "' + "\nprint('hello world')\n" + '"'
        new_cmd, tmp_path = _rewrite_inline_script(cmd)
        assert tmp_path is not None
        try:
            rc, stdout, stderr = _run_path_a(new_cmd)
            assert rc == 0, f"执行失败: {stderr}"
            assert stdout.strip() == "hello world"
        finally:
            _cleanup_script_temp(tmp_path)

    def test_escaped_quotes_direct_path_a(self):
        """
        [关键修复] 含 \\" 的脚本不经 rewrite 直接 Path A 可正确运行
        """
        cmd = 'python -c "print(\\"hello world\\")"'
        # 不应触发 rewrite
        new_cmd, tmp_path = _rewrite_inline_script(cmd)
        assert tmp_path is None, "不应触发 rewrite"
        # 直接 Path A 执行
        rc, stdout, stderr = _run_path_a(new_cmd)
        assert rc == 0, f"Path A 执行失败: {stderr}"
        assert stdout.strip() == "hello world"

    @pytest.mark.skipif(" " not in tempfile.gettempdir(), reason="临时目录不含空格，跳过")
    def test_temp_path_with_spaces(self):
        """[关键修复] 临时目录含空格时路径应被正确引用"""
        # 这个测试只在临时目录含空格时运行
        cmd = 'python -c "' + '\nprint("ok")\n' + '"'
        new_cmd, tmp_path = _rewrite_inline_script(cmd)
        assert tmp_path is not None
        try:
            # 验证 shlex.split 能正确拆分路径
            args = shlex.split(new_cmd)
            assert len(args) == 2, f"路径被切碎: {args}"
            rc, stdout, stderr = _run_path_a(new_cmd)
            assert rc == 0, f"含空格路径执行失败: {stderr}"
        finally:
            _cleanup_script_temp(tmp_path)

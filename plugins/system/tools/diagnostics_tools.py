# -*- coding: utf-8 -*-
"""
系统工具插件 — 诊断（get_diagnostics 完整实现）

从主程序 app/tools/diagnostics_tools.py 整体迁移（工具插件化）：
- DiagnosticsTools 类：LSP CLI 诊断（pyright/mypy/flake8/tsc/eslint/shellcheck）
- impl 通过 tool_ctx["workdir"] 驱动，不依赖主程序 services
"""

# -*- coding: utf-8 -*-
"""
诊断工具集 - 提供代码静态分析功能

支持：
- Python: pyright 语法检查
- JavaScript/TypeScript: ESLint 检查
- 通用: grep 搜索错误关键词
"""
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

import orjson as json

from app.tools.result import ToolResult

# pyright Python 模块可用性检测
try:
    from pyright import cli as pyright_cli
    _HAS_PYRIGHT = True
except ImportError:
    _HAS_PYRIGHT = False

class DiagnosticsTools:
    def __init__(self, owner):
        self._owner = owner

    @property
    def workdir(self) -> Path:
        return self._owner.workdir

    def _detect_language(self, file_path: str) -> str:
        ext = Path(file_path).suffix.lower()
        return {
            ".py": "python",
            ".js": "javascript",
            ".mjs": "javascript",
            ".cjs": "javascript",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".sh": "shellscript",
            ".bash": "shellscript",
            ".zsh": "shellscript",
        }.get(ext, "unknown")

    def _run_quietly(self, cmd: list, cwd: Optional[str] = None, timeout: int = 30):
        # Windows: 如果命令不以 .exe/.cmd/.bat 结尾，通过 cmd /c 包装以支持 PATHEXT（如 tsc → tsc.cmd）
        if sys.platform == "win32" and cmd and not cmd[0].lower().endswith((".exe", ".cmd", ".bat")):
            cmd = ["cmd", "/c"] + cmd
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.workdir) if cwd is None else cwd,
                creationflags=subprocess.CREATE_NO_WINDOW
                if sys.platform == "win32"
                else 0,
            )
            out = (r.stdout + ("\n" + r.stderr if r.stderr else "")).strip()
            return r.returncode, out
        except FileNotFoundError:
            return -1, f"(command not found: {cmd[0]})"
        except subprocess.TimeoutExpired:
            return -1, f"(timed out after {timeout}s)"
        except Exception as e:
            return -1, f"(error: {e})"

    def _run_pyright_module(self, file_path: str, timeout: int = 60) -> Tuple[int, str]:
        """使用 pyright Python 模块运行诊断"""
        try:
            r = pyright_cli.run(  # type: ignore[possibly-undefined]
                "--outputjson", file_path, capture_output=True, timeout=timeout
            )
            stdout = r.stdout
            if stdout is None:
                return r.returncode, ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8")
            return r.returncode, stdout
        except Exception as e:
            return -1, f"(pyright module error: {e})"

    def _parse_pyright_json(self, output: str) -> Optional[str]:
        """解析 pyright JSON 输出，返回格式化字符串或 None"""
        try:
            data = json.loads(output)
            diags = data.get("generalDiagnostics", [])
            if not diags:
                return None
            lines = [f"pyright ({len(diags)} issue(s)):"]
            for d in diags[:50]:
                rng = d.get("range", {}).get("start", {})
                ln = rng.get("line", 0) + 1
                ch = rng.get("character", 0) + 1
                sev = d.get("severity", "error")
                msg = d.get("message", "")
                rule = d.get("rule", "")
                lines.append(
                    f"  {ln}:{ch} [{sev}] {msg}" + (f" ({rule})" if rule else "")
                )
            return "\n".join(lines)
        except (json.JSONDecodeError, KeyError):
            # 解析失败时返回原始输出的截断版本
            if len(output) > 0:
                return output[:3000]
            return None

    def get_diagnostics(self, file_path: str, language: Optional[str] = None) -> ToolResult:
        p = Path(file_path)
        if not p.exists():
            return ToolResult(False, error=f"File not found: {file_path}")

        lang = language or self._detect_language(file_path)
        abs_path = str(p.resolve())
        results = []

        if lang == "python":
            # 优先使用 pyright Python 模块
            if _HAS_PYRIGHT:
                rc, out = self._run_pyright_module(abs_path)
                if rc != -1:
                    parsed = self._parse_pyright_json(out)
                    if parsed:
                        results.append(parsed)
                    else:
                        results.append("pyright: no diagnostics")
                else:
                    results.append(f"pyright: {out}")
            else:
                # 回退到系统 pyright 命令
                rc, out = self._run_quietly(["pyright", "--outputjson", abs_path])
                if rc != -1:
                    try:
                        data = json.loads(out)
                        diags = data.get("generalDiagnostics", [])
                        if not diags:
                            results.append("pyright: no diagnostics")
                        else:
                            lines = [f"pyright ({len(diags)} issue(s)):"]
                            for d in diags[:50]:
                                rng = d.get("range", {}).get("start", {})
                                ln = rng.get("line", 0) + 1
                                ch = rng.get("character", 0) + 1
                                sev = d.get("severity", "error")
                                msg = d.get("message", "")
                                rule = d.get("rule", "")
                                lines.append(
                                    f"  {ln}:{ch} [{sev}] {msg}"
                                    + (f" ({rule})" if rule else "")
                                )
                            results.append("\n".join(lines))
                    except json.JSONDecodeError:
                        if out:
                            results.append(f"pyright:\n{out[:3000]}")
                else:
                    # 最后回退到 mypy/flake8/py_compile
                    rc2, out2 = self._run_quietly(["mypy", "--no-error-summary", abs_path])
                    if rc2 != -1:
                        results.append(
                            f"mypy:\n{out2[:3000]}" if out2 else "mypy: no diagnostics"
                        )
                    else:
                        rc3, out3 = self._run_quietly(["flake8", abs_path])
                        if rc3 != -1:
                            results.append(
                                f"flake8:\n{out3[:3000]}"
                                if out3
                                else "flake8: no diagnostics"
                            )
                        else:
                            rc4, out4 = self._run_quietly(
                                ["python3", "-m", "py_compile", abs_path]
                            )
                            if out4:
                                results.append(f"py_compile (syntax check):\n{out4}")
                            else:
                                results.append(
                                    "py_compile: syntax OK (no further tools available)"
                                )

        elif lang in ("javascript", "typescript"):
            rc, out = self._run_quietly(["tsc", "--noEmit", "--strict", abs_path])
            if rc != -1:
                results.append(f"tsc:\n{out[:3000]}" if out else "tsc: no errors")
            else:
                rc2, out2 = self._run_quietly(["eslint", abs_path])
                if rc2 != -1:
                    results.append(
                        f"eslint:\n{out2[:3000]}" if out2 else "eslint: no issues"
                    )
                else:
                    results.append(
                        "No TypeScript/JavaScript checker found (install tsc or eslint)"
                    )

        elif lang == "shellscript":
            rc, out = self._run_quietly(["shellcheck", abs_path])
            if rc != -1:
                results.append(
                    f"shellcheck:\n{out[:3000]}" if out else "shellcheck: no issues"
                )
            else:
                rc2, out2 = self._run_quietly(["bash", "-n", abs_path])
                results.append(
                    f"bash -n (syntax check):\n{out2}" if out2 else "bash -n: syntax OK"
                )

        else:
            results.append(
                f"No diagnostic tool available for language: {lang or 'unknown'} (ext: {Path(file_path).suffix})"
            )

        return ToolResult(
            True, content="\n\n".join(results) if results else "(no diagnostics output)"
        )

# ============================================================
# 工具插件化：owner 包装 / impl / register
# ============================================================

class _OwnerShim:
    """DiagnosticsTools 的 owner 最小实现（仅 workdir）"""

    def __init__(self, workdir):
        self.workdir = Path(workdir)


_diag_instance = None


def _get_diagnostics_tools(workdir):
    """获取 DiagnosticsTools 实例（进程级懒创建，workdir 每次更新）"""
    global _diag_instance
    if _diag_instance is None:
        _diag_instance = DiagnosticsTools(_OwnerShim(workdir))
    else:
        _diag_instance._owner.workdir = Path(workdir)
    return _diag_instance


GROUP_DIAG = "诊断与代码智能"

_GET_DIAGNOSTICS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_diagnostics",
        "description": "获取文件语法检查结果(错误/警告/提示)。支持 Python(pyright/mypy/flake8)、JS/TS(tsc/eslint)、Shell(shellcheck)。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "language": {"type": "string", "description": "语言: python/javascript/typescript/shellscript"},
            },
            "required": ["path"],
        },
    },
}


def _get_diagnostics_impl(tool_ctx, **kwargs):
    diag = _get_diagnostics_tools(tool_ctx.get("workdir") or Path.cwd())
    return diag.get_diagnostics(kwargs.get("path", ""), kwargs.get("language"))


_LSP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "lsp",
        "description": (
            "通过LSP执行代码智能操作。\n"
            "\n"
            "操作类型：\n"
            "- diagnostics: 文件诊断(错误/警告/提示)\n"
            "- documentSymbols: 符号列表(类/函数/变量)\n"
            "- goToDefinition: 跳定义\n"
            "- findReferences: 找引用\n"
            "- hover: 光标位置文档/类型\n"
            "- listServers: 列出LSP服务器状态\n"
            "\n"
            "line/column从1开始。diagnostics/listServers无需line/column。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（listServers 操作不需要但可忽略）"},
                "operation": {
                    "type": "string",
                    "enum": ["diagnostics", "documentSymbols", "goToDefinition", "findReferences", "hover", "listServers"],
                    "description": "操作类型",
                },
                "line": {"type": "integer", "description": "行号（diagnostics/listServers 不需要）"},
                "column": {"type": "integer", "description": "列号（diagnostics/listServers/documentSymbols 不需要）"},
            },
            "required": ["path", "operation"],
        },
    },
}


def _lsp_impl(tool_ctx, **kwargs):
    service = tool_ctx.get("services", {}).get("lsp")
    if service is None:
        return ToolResult(False, error="LSP 服务不可用")
    return service.lsp(
        path=kwargs.get("path", ""),
        operation=kwargs.get("operation", "diagnostics"),
        line=int(kwargs.get("line") or 0),
        column=int(kwargs.get("column") or 0),
        language=kwargs.get("language"),
    )


def register(registry):
    registry.register(
        "get_diagnostics", _GET_DIAGNOSTICS_SCHEMA, impl=_get_diagnostics_impl,
        danger="safe", icon="工具", cn_name="诊断",
        group=GROUP_DIAG, description="获取代码诊断信息",
    )
    registry.register(
        "lsp", _LSP_SCHEMA, impl=_lsp_impl,
        danger="safe", icon="工具", cn_name="LSP",
        group=GROUP_DIAG, description="LSP代码智能操作",
    )

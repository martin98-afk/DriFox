# -*- coding: utf-8 -*-
"""
诊断工具集 - 提供代码静态分析功能

支持：
- Python: pyright 语法检查
- JavaScript/TypeScript: ESLint 检查
- 通用: grep 搜索错误关键词
"""
import orjson as json
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

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

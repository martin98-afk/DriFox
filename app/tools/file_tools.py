# -*- coding: utf-8 -*-
"""
文件工具集 - 提供文件读写和编辑功能

支持：
- 读取：read（返回文件原文）
- 写入：write, write_file（含 diff 审查）
- 编辑：edit（基于 oldString/newString 字符串替换）
- 目录：list, mkdir, delete_file
- 搜索：grep, scan_repo
- 批量编辑：multi_edit（支持多次替换）
"""
import fnmatch
import re
from typing import Dict, List, Optional
from pathlib import Path
import os
from functools import lru_cache

from loguru import logger
import difflib
from app.tools.result import ToolResult

MAX_GREP_CONTENT_LENGTH = 15000

# ========== 性能优化：模块级别常量和缓存 ==========
_GREP_EXCLUDE_DIRS = frozenset({
    '.drifox', '.mypy_cache', '.git', 'node_modules', '__pycache__',
    'venv', '.venv', 'dist', 'build', '.idea', '.vscode'
})


@lru_cache(maxsize=128)
def _compile_grep_pattern(pattern: str) -> re.Pattern:
    """编译 grep 正则表达式（带缓存）"""
    return re.compile(pattern, re.IGNORECASE)


def _resolve_path(workdir: Path, path: str) -> Path:
    """
    解析相对路径为绝对路径

    Args:
        workdir: 工作目录
        path: 要解析的路径

    Returns:
        解析后的绝对路径
    """
    if not path:
        return workdir
    try:
        expanded = os.path.expandvars(path)
        if expanded != path:
            path = expanded
        p = Path(path)
        if p.is_absolute():
            return p.resolve()
        else:
            return (workdir / p).resolve()
    except (ValueError, OSError, RuntimeError) as e:
        logger.warning(f"[FileTools] Failed to resolve path {path}: {e}")
        return workdir


class FileTools:
    def __init__(self, owner):
        self._owner = owner
        # 文件修改时间追踪：{绝对路径: 修改时间戳}
        self._file_mtimes: Dict[str, float] = {}

    @property
    def workdir(self) -> Path:
        return self._owner.workdir

    @workdir.setter
    def workdir(self, value: Path):
        """保留向后兼容：外部设置 workdir 时同步更新 owner"""
        self._owner.workdir = value

    def _resolve_path(self, path: str) -> Path:
        """委托给模块级函数"""
        return _resolve_path(self.workdir, path)

    def _check_file_modified(self, full_path: Path) -> Optional[ToolResult]:
        """
        检查文件是否被外部修改。
        如果文件之前被读取过，且当前修改时间与记录不一致，返回警告
        """
        path_key = str(full_path)
        if path_key not in self._file_mtimes:
            # 文件没有被读取过，不检查
            return None

        try:
            current_mtime = full_path.stat().st_mtime
            recorded_mtime = self._file_mtimes[path_key]

            if current_mtime != recorded_mtime:
                return ToolResult(
                    False,
                    error=f"⚠️ 文件已被外部修改: {full_path.name}\n\n"
                          f"该文件在你读取后被其他人/进程修改过。\n"
                          f"你的编辑可能会覆盖他人的更改。\n\n"
                          f"建议: 请先重新读取文件(Read)确认最新内容后再进行编辑。"
                )
        except OSError:
            pass

        return None

    def read_file(self, path: str, offset: int = 1, limit: int = 500,
                  show_line_numbers: bool = False) -> ToolResult:
        """
        读取文件内容

        Args:
            path: 文件路径
            offset: 起始行号（从1开始）
            limit: 最大读取行数
            show_line_numbers: 是否显示行号，默认 False（返回原文）

        读取时记录文件的修改时间，用于后续编辑时检测文件是否被外部修改
        """
        try:
            full_path = self._resolve_path(path)
            if not full_path.exists():
                return ToolResult(False, error=f"File not found: {path}")

            if full_path.is_dir():
                return self.list_directory(path)

            # 记录文件修改时间
            self._file_mtimes[str(full_path)] = full_path.stat().st_mtime

            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()

            total_lines = len(all_lines)
            start_idx = max(0, offset - 1)
            end_idx = min(total_lines, start_idx + limit)

            content_slice = all_lines[start_idx:end_idx]
            # 文件头用相对路径（根目录外 fallback 到原始路径）
            try:
                display_path = str(full_path.relative_to(self.workdir))
            except ValueError:
                display_path = path
            res_info = f"#File: {display_path} (Lines {start_idx + 1}-{end_idx} of {total_lines})\n\n#Content:\n"
            if show_line_numbers:
                # 带行号格式
                formatted_content = "".join(
                    f"{i + start_idx + 1:6d}|{line}" for i, line in enumerate(content_slice)
                )

                return ToolResult(True, content=res_info + formatted_content)
            else:
                # 返回原文
                return ToolResult(True, content=res_info + "".join(content_slice))
        except Exception as e:
            return ToolResult(False, error=f"Read error: {str(e)}")

    def read_persisted_output(self, file_path: str) -> ToolResult:
        """
        读取之前被持久化的工具结果完整内容

        配合 app.core.tool_result_persister 使用:
        当工具结果超 50K 字符时会被自动持久化到磁盘, 上文里只保留预览.
        如果模型需要完整内容, 调用本工具读取.

        Args:
            file_path: 持久化时返回的文件绝对路径
                       (形如 .drifox/projects/{session_id}/tool-results/xxx.txt)

        Returns:
            ToolResult: 包含完整内容
        """
        try:
            from pathlib import Path
            from app.utils.utils import get_app_data_dir

            p = Path(file_path)
            # 安全检查: 必须在 .drifox/projects/*/tool-results/ 下
            # 防止任意文件读取
            try:
                allowed_root = (get_app_data_dir() / "projects").resolve()
                p_resolved = p.resolve()
                p_resolved.relative_to(allowed_root)  # 越界则抛 ValueError
            except ValueError:
                return ToolResult(
                    False,
                    error=(
                        f"Access denied: {file_path} is outside the "
                        f"tool-results directory."
                    ),
                )
            if not p.exists() or p.suffix != ".txt":
                return ToolResult(
                    False,
                    error=f"Persisted output not found: {file_path}",
                )

            content = p.read_text(encoding="utf-8", errors="replace")
            # 二级软截断: 避免"恢复"时再爆 context
            if len(content) > 200_000:
                content = (
                    content[:200_000]
                    + f"\n\n[Truncated at 200,000 chars. "
                    f"Full size on disk: {p.stat().st_size} bytes]"
                )
            return ToolResult(True, content=content)
        except Exception as e:
            return ToolResult(
                False, error=f"Read persisted output error: {str(e)}"
            )

    def write_file(self, path: str, content: str) -> ToolResult:
        """
        写入文件，自动创建中间目录
        写入前检查文件是否被外部修改
        """
        try:
            full_path = self._resolve_path(path)

            # 检查文件是否被外部修改
            check_result = self._check_file_modified(full_path)
            if check_result:
                return check_result

            full_path.parent.mkdir(parents=True, exist_ok=True)

            # 写入前读取旧内容，用于 diff 计算
            old_content = ""
            try:
                if full_path.exists():
                    old_content = full_path.read_text(encoding="utf-8")
            except Exception:
                old_content = ""

            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content if content is not None else "")

            # 计算 unified diff
            diff_str = ""
            if old_content or content:
                diff_lines = list(difflib.unified_diff(
                    old_content.splitlines(),
                    (content or "").splitlines(),
                    fromfile=path, tofile=path,
                    lineterm=''
                ))
                diff_str = "\n".join(diff_lines)

            # 更新修改时间记录
            self._file_mtimes[str(full_path)] = full_path.stat().st_mtime

            return ToolResult(True, content=f"Successfully written to {path}", diff=diff_str or None)
        except Exception as e:
            return ToolResult(False, error=f"Write error: {str(e)}")

    def edit_file(self, path: str, oldString: str, newString: str,
                  replaceAll: bool = False) -> ToolResult:
        """
        精确文本替换编辑。包含唯一性校验，防止误改多处代码。
        编辑前检查文件是否被外部修改。
        生成 unified diff 用于审查。

        Args:
            path: 文件路径
            oldString: 要替换的旧文本（精确匹配，包含空白字符）
            newString: 替换后的新文本
            replaceAll: 是否替换所有匹配项（默认 False，只替换第一个）
                        当 oldString 出现多次时需设置为 True

        Returns:
            ToolResult: 包含 diff 和编辑结果
        """
        try:
            full_path = self._resolve_path(path)
            if not full_path.exists():
                return ToolResult(False, error=f"File not found: {path}")

            # 检查文件是否被外部修改
            check_result = self._check_file_modified(full_path)
            if check_result:
                return check_result

            old_content = full_path.read_text(encoding="utf-8", errors="replace")

            count = old_content.count(oldString)
            if count == 0:
                return ToolResult(
                    False,
                    error="The specified 'oldString' was not found in the file. "
                          "Ensure exact match including whitespace and indentation."
                )

            if count > 1 and not replaceAll:
                return ToolResult(
                    False,
                    error=f"The 'oldString' appears {count} times in the file. "
                          f"Please provide a more specific context to ensure uniqueness, "
                          f"or set replaceAll=True."
                )

            new_content = old_content.replace(oldString, newString, -1 if replaceAll else 1)
            full_path.write_text(new_content, encoding="utf-8")
            self._file_mtimes[str(full_path)] = full_path.stat().st_mtime

            # ── 生成 unified diff ──
            diff_lines = list(difflib.unified_diff(
                old_content.splitlines(),
                new_content.splitlines(),
                fromfile=path, tofile=path,
                lineterm=''
            ))
            diff_str = "\n".join(diff_lines) if diff_lines else ""

            return ToolResult(
                True,
                content=f"Successfully edited {path}.",
                diff=diff_str,
            )
        except Exception as e:
            return ToolResult(False, error=f"Edit error: {str(e)}")

    def multi_edit(self, path: str, edits: List[dict]) -> ToolResult:
        """
        批量编辑同一文件，支持多次 oldString/newString 替换。
        所有替换完成后生成 unified diff 用于审查。

        Args:
            path: 文件路径
            edits: 编辑操作列表，每项为 {"oldString": "...", "newString": "..."}
                   按顺序逐条执行替换（仅替换第一个匹配项）

        Returns:
            ToolResult: 包含 diff 和编辑结果
        """
        try:
            full_path = self._resolve_path(path)
            if not full_path.exists():
                return ToolResult(False, error=f"File not found: {path}")

            old_content = full_path.read_text(encoding="utf-8", errors="replace")
            content = old_content
            applied = 0
            skipped_indices = []

            for idx, edit in enumerate(edits):
                old = edit.get("oldString")
                new = edit.get("newString")
                if old in content:
                    content = content.replace(old, new, 1)
                    applied += 1
                else:
                    skipped_indices.append(idx + 1)  # 1-based

            if applied == 0:
                return ToolResult(False, error="No edits were applied: none of the oldStrings were found.")

            full_path.write_text(content, encoding="utf-8")
            self._file_mtimes[str(full_path)] = full_path.stat().st_mtime

            # ── 生成 unified diff ──
            diff_lines = list(difflib.unified_diff(
                old_content.splitlines(),
                content.splitlines(),
                fromfile=path, tofile=path,
                lineterm=''
            ))
            diff_str = "\n".join(diff_lines) if diff_lines else ""

            result = f"Applied {applied}/{len(edits)} edits to {path}."
            if skipped_indices:
                skipped_str = ", ".join(f"#{i}" for i in skipped_indices)
                result += f" (skipped: {skipped_str} - no match, 1-based index)"

            return ToolResult(
                True,
                content=result,
                diff=diff_str,
            )
        except Exception as e:
            return ToolResult(False, error=f"Multi-edit error: {str(e)}")

    def grep_files(self, pattern: str, path: str = ".", include: str = None) -> ToolResult:
        """同步执行 grep"""
        try:
            search_root = self._resolve_path(path)
            # 性能优化：使用带缓存的编译函数
            regex = _compile_grep_pattern(pattern)
            results = []

            for root, dirs, files in os.walk(search_root):
                # 性能优化：使用 frozenset
                dirs[:] = [d for d in dirs if d not in _GREP_EXCLUDE_DIRS]

                for filename in files:
                    if include and not fnmatch.fnmatch(filename, include):
                        continue

                    file_path = Path(root) / filename
                    try:
                        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                            for i, line in enumerate(f, 1):
                                if regex.search(line):
                                    try:
                                        rel_path = file_path.relative_to(self.workdir)
                                    except ValueError:
                                        rel_path = file_path
                                    results.append(f"{rel_path}:{i}: {line.strip()}")
                                    if len(results) >= 100:
                                        return ToolResult(True, content="\n".join(
                                            results) + "\n\n... (Too many matches, please refine your search pattern)")
                    except Exception:
                        continue

            content = "\n".join(results) if results else "No matches found."
            if len(content) > MAX_GREP_CONTENT_LENGTH:
                content = content[:MAX_GREP_CONTENT_LENGTH] + f"\n\n... (Content truncated, exceeds {MAX_GREP_CONTENT_LENGTH} characters limit)"
            return ToolResult(True, content=content)
        except Exception as e:
            return ToolResult(False, error=f"Grep error: {str(e)}")

    def list_directory(self, path: str = ".") -> ToolResult:
        """
        列出目录，增加 [DIR] 标识
        """
        try:
            target_path = self._resolve_path(path)
            if not target_path.exists():
                return ToolResult(False, error=f"Path not found: {path}")

            entries = []
            for item in sorted(target_path.iterdir()):
                prefix = "[DIR] " if item.is_dir() else "      "
                entries.append(f"{prefix}{item.name}")

            output = f"Contents of {path}:\n" + ("\n".join(entries) if entries else "(Empty directory)")
            return ToolResult(True, content=output)
        except Exception as e:
            return ToolResult(False, error=f"List error: {str(e)}")

    def glob_files(self, pattern: str, path: str = ".") -> ToolResult:
        """
        通过通配符查找文件
        """
        try:
            search_path = self._resolve_path(path)
            matches = list(search_path.rglob(pattern))

            if not matches:
                return ToolResult(True, content="No files matched the pattern.")

            results = []
            for m in matches[:100]:
                if m.is_file():
                    try:
                        results.append(str(m.relative_to(self.workdir)))
                    except ValueError:
                        results.append(str(m))

            return ToolResult(True, content="\n".join(results))
        except Exception as e:
            return ToolResult(False, error=f"Glob error: {str(e)}")

    def diff_files(
        self, file1: str, file2: str = None, use_git: bool = False
    ) -> ToolResult:
        import subprocess
        import sys

        try:
            path1 = self._resolve_path(file1)
            if not path1.exists():
                return ToolResult(False, error=f"File not found: {file1}")

            _cf = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

            if use_git:
                result = subprocess.run(
                    ["git", "diff", str(path1)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    cwd=str(self.workdir),
                    creationflags=_cf,
                )
                if result.returncode != 0 and "not a git repository" in result.stderr:
                    return ToolResult(False, error="Not a git repository")
                diff_output = result.stdout or result.stderr
                if not diff_output:
                    return ToolResult(
                        True, content=f"No changes in {file1} (compared to git)"
                    )
                return ToolResult(True, content=diff_output)

            if file2:
                path2 = self._resolve_path(file2)
                if not path2.exists():
                    return ToolResult(False, error=f"File not found: {file2}")
                result = subprocess.run(
                    ["diff", "-u", str(path1), str(path2)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    creationflags=_cf,
                )
            else:
                result = subprocess.run(
                    ["git", "diff", "HEAD", str(path1)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    cwd=str(self.workdir),
                    creationflags=_cf,
                )
                if result.returncode != 0 and "not a git repository" in result.stderr:
                    return ToolResult(
                        False, error="Not a git repository and no second file provided"
                    )
                return ToolResult(
                    True,
                    content=result.stdout
                    if result.stdout
                    else f"No changes in {file1} (compared to git HEAD)",
                )

            if not result.stdout:
                return ToolResult(True, content="Files are identical")
            return ToolResult(True, content=result.stdout)
        except Exception as e:
            return ToolResult(False, error=f"Diff error: {str(e)}")

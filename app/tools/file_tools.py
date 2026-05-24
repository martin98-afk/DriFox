# -*- coding: utf-8 -*-
"""
文件工具集 - 提供文件读写和编辑功能

支持：
- 读取：read（返回文件原文）
- 写入：write, write_file（含 diff 审查）
- 编辑：edit（基于 oldString/newString 字符串替换）
- 目录：list, mkdir, delete_file
- 搜索：grep (异步), scan_repo (异步）
- 批量编辑：multi_edit（支持多次替换）
"""
import fnmatch
import re
from typing import Dict, List, Optional, Callable
from pathlib import Path
import os
from functools import lru_cache

from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool, QRunnable
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


class GrepTask(QRunnable):
    """异步 Grep 任务，在子线程中执行"""

    class Signals(QObject):
        finished = pyqtSignal(object)  # ToolResult

    def __init__(self, pattern: str, path: str, include: str, workdir: Path, cancelled_ref: list):
        super().__init__()
        self.signals = self.Signals()
        self.pattern = pattern
        self.path = path
        self.include = include
        self.workdir = workdir
        self.cancelled_ref = cancelled_ref  # [bool] 引用，可被外部修改

    def run(self):
        """在子线程中执行 grep"""
        try:
            result = self._do_grep()
            self.signals.finished.emit(result)
        except Exception as e:
            self.signals.finished.emit(ToolResult(False, error=f"Grep error: {str(e)}"))

    def _do_grep(self) -> ToolResult:
        """实际的 grep 实现"""
        try:
            if not self.path:
                search_root = self.workdir
            else:
                search_root = _resolve_path(self.workdir, self.path)

            # 性能优化：使用带缓存的编译函数
            regex = _compile_grep_pattern(self.pattern)

            results = []

            for root, dirs, files in os.walk(search_root):
                # 定期检查取消标志
                if self.cancelled_ref and self.cancelled_ref[0]:
                    return ToolResult(False, error="搜索已取消")

                # 性能优化：使用 frozenset
                dirs[:] = [d for d in dirs if d not in _GREP_EXCLUDE_DIRS]

                for filename in files:
                    if self.cancelled_ref and self.cancelled_ref[0]:
                        return ToolResult(False, error="搜索已取消")

                    if self.include and not fnmatch.fnmatch(filename, self.include):
                        continue

                    file_path = Path(root) / filename
                    try:
                        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                            for i, line in enumerate(f, 1):
                                if self.cancelled_ref and self.cancelled_ref[0]:
                                    return ToolResult(False, error="搜索已取消")
                                if regex.search(line):
                                    try:
                                        rel_path = file_path.relative_to(self.workdir)
                                    except ValueError:
                                        rel_path = file_path
                                    results.append(f"{rel_path}:{i}: {line.strip()}")
                                    if len(results) >= 100:
                                        return ToolResult(True, content="\n".join(
                                            results) + "\n\n... (Too many matches, please refine your search pattern)")
                    except:
                        continue

            content = "\n".join(results) if results else "No matches found."
            if len(content) > MAX_GREP_CONTENT_LENGTH:
                content = content[:MAX_GREP_CONTENT_LENGTH] + f"\n\n... (Content truncated, exceeds {MAX_GREP_CONTENT_LENGTH} characters limit)"
            return ToolResult(True, content=content)
        except Exception as e:
            return ToolResult(False, error=f"Grep error: {str(e)}")

    def _resolve_path(self, path: str) -> Path:
        """委托给模块级函数"""
        return _resolve_path(self.workdir, path)


class FileTools:
    def __init__(self, owner):
        self._owner = owner
        self._thread_pool: Optional[QThreadPool] = None
        self._current_grep_task: Optional[GrepTask] = None
        self._grep_cancelled = [False]  # 使用列表引用，可以在子线程中被检查
        # 文件修改时间追踪：{绝对路径: 修改时间戳}
        self._file_mtimes: Dict[str, float] = {}

    @property
    def workdir(self) -> Path:
        return self._owner.workdir

    @workdir.setter
    def workdir(self, value: Path):
        """保留向后兼容：外部设置 workdir 时同步更新 owner"""
        self._owner.workdir = value

    def _get_thread_pool(self) -> QThreadPool:
        """获取或创建线程池"""
        if self._thread_pool is None:
            self._thread_pool = QThreadPool.globalInstance()
        return self._thread_pool

    def cancel(self):
        """取消当前正在执行的操作"""
        self._grep_cancelled[0] = True

    def reset_cancelled(self):
        """重置取消标志"""
        self._grep_cancelled[0] = False

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
            res_info = f"#File: {path} (Lines {start_idx + 1}-{end_idx} of {total_lines})\n\n#Content:\n"
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

    def grep_files(self, pattern: str, path: str = ".", include: str = None,
                   callback: Optional[Callable[[ToolResult], None]] = None) -> Optional[ToolResult]:
        """
        高效搜索，排除干扰目录，限制返回行数

        如果提供 callback，则异步执行并返回 None
        否则同步执行并返回 ToolResult

        Args:
            pattern: 正则表达式模式
            path: 搜索路径，默认当前目录
            include: 文件名过滤模式
            callback: 异步完成后的回调函数

        Returns:
            同步执行时返回 ToolResult，异步执行时返回 None
        """
        # 每次调用前重置取消标志
        self._grep_cancelled[0] = False

        if callback is not None:
            # 异步执行
            self._run_grep_async(pattern, path, include, callback)
            return None
        else:
            # 同步执行（保持向后兼容）
            return self._run_grep_sync(pattern, path, include)

    def _run_grep_sync(self, pattern: str, path: str, include: str) -> ToolResult:
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
                    if self._grep_cancelled[0]:
                        return ToolResult(False, error="搜索已取消")

                    if include and not fnmatch.fnmatch(filename, include):
                        continue

                    file_path = Path(root) / filename
                    try:
                        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                            for i, line in enumerate(f, 1):
                                if self._grep_cancelled[0]:
                                    return ToolResult(False, error="搜索已取消")
                                if regex.search(line):
                                    try:
                                        rel_path = file_path.relative_to(self.workdir)
                                    except ValueError:
                                        rel_path = file_path
                                    results.append(f"{rel_path}:{i}: {line.strip()}")
                                    if len(results) >= 100:
                                        return ToolResult(True, content="\n".join(
                                            results) + "\n\n... (Too many matches, please refine your search pattern)")
                    except:
                        continue

            content = "\n".join(results) if results else "No matches found."
            if len(content) > MAX_GREP_CONTENT_LENGTH:
                content = content[:MAX_GREP_CONTENT_LENGTH] + f"\n\n... (Content truncated, exceeds {MAX_GREP_CONTENT_LENGTH} characters limit)"
            return ToolResult(True, content=content)
        except Exception as e:
            return ToolResult(False, error=f"Grep error: {str(e)}")

    def _run_grep_async(self, pattern: str, path: str, include: str,
                        callback: Callable[[ToolResult], None]):
        """异步执行 grep"""
        task = GrepTask(pattern, path, include, self.workdir, self._grep_cancelled)
        self._current_grep_task = task

        def on_finished(result: ToolResult):
            self._current_grep_task = None
            callback(result)

        task.signals.finished.connect(on_finished)
        self._get_thread_pool().start(task)
        logger.info(f"[FileTools] Started async grep task, pattern={pattern}")

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

        try:
            path1 = self._resolve_path(file1)
            if not path1.exists():
                return ToolResult(False, error=f"File not found: {file1}")

            if use_git:
                result = subprocess.run(
                    ["git", "diff", str(path1)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    cwd=str(self.workdir),
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
                )
            else:
                result = subprocess.run(
                    ["git", "diff", "HEAD", str(path1)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    cwd=str(self.workdir),
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

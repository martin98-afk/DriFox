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

import difflib
import fnmatch
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from app.constants import IMAGE_EXTENSIONS
from app.tools.result import ToolResult

MAX_GREP_CONTENT_LENGTH = 15000

# ========== 性能优化：模块级别常量和缓存 ==========
_GREP_EXCLUDE_DIRS = frozenset(
    {
        ".drifox",
        ".mypy_cache",
        ".git",
        "node_modules",
        "__pycache__",
        "venv",
        ".venv",
        "dist",
        "build",
        ".idea",
        ".vscode",
    }
)

# scan_repo 专用排除集合：grep 集合的超集，覆盖更多构建产物与缓存目录
# 使用 fnmatch 风格通配符：'*' 匹配任意字符序列（涵盖 .venv/.venv_old/.venv.bak 这类变体）
_SCAN_EXCLUDE_DIRS = frozenset(
    {
        # 来自 grep 集合（保证 scan 至少和 grep 一样严格）
        ".drifox",
        ".mypy_cache*",
        ".git*",
        "node_modules*",
        "__pycache__",
        "venv*",
        ".venv*",
        "dist*",
        "build*",
        ".idea*",
        ".vscode*",
        # Python 测试/打包
        ".pytest_cache*",
        ".tox*",
        "site-packages*",
        ".eggs*",
        ".ipynb_checkpoints*",
        "htmlcov*",
        ".ruff_cache*",
        # JS/前端构建产物
        ".next*",
        ".nuxt*",
        ".svelte-kit*",
        ".cache*",
        ".parcel-cache*",
        ".turbo*",
        ".nyc_output*",
        "coverage*",
        ".vercel*",
        # C/C++/Rust
        "target*",
        "cmake-build-debug*",
        "cmake-build-release*",
        # Java/Kotlin
        ".gradle*",
        "out*",
    }
)


@lru_cache(maxsize=128)
def _compile_grep_pattern(pattern: str) -> re.Pattern:
    """编译 grep 正则表达式（带缓存）"""
    return re.compile(pattern, re.IGNORECASE)


# ========== grep_files 优化：.gitignore 加载与缓存 ==========
_GITIGNORE_CACHE: dict[str, list[str]] = {}


def _load_gitignore_patterns(search_root: Path) -> list[str]:
    """读取项目的 .gitignore 并返回 fnmatch 模式列表"""
    key = str(search_root)
    if key in _GITIGNORE_CACHE:
        return _GITIGNORE_CACHE[key]
    patterns = []
    gitignore_path = search_root / ".gitignore"
    if gitignore_path.exists():
        try:
            for line in gitignore_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    # 去掉开头的 '/'（.gitignore 中的路径是相对于根目录的）
                    if line.startswith("/"):
                        line = line[1:]
                    # 去掉结尾的 '/'（目录标识）
                    if line.endswith("/"):
                        line = line.rstrip("/")
                    patterns.append(line)
        except Exception:
            pass
    _GITIGNORE_CACHE[key] = patterns
    # 最多缓存 8 个项目的 .gitignore
    if len(_GITIGNORE_CACHE) > 8:
        _GITIGNORE_CACHE.pop(next(iter(_GITIGNORE_CACHE)))
    return patterns


# 常见二进制文件扩展名（跳过不必要的扫描）
_BINARY_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".svg",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".otf",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".pyd",
        ".bin",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".mp3",
        ".mp4",
        ".avi",
        ".mkv",
        ".mov",
        ".wav",
        ".flac",
        ".pyc",
        ".pyo",
        ".class",
        ".o",
        ".a",
        ".lib",
        ".wasm",
        ".dat",
        ".dmg",
        ".iso",
        ".min.js",
        ".min.css",
    }
)

# 已知文本扩展名：精确匹配直接跳过嗅探，避免为每个文件读 8KB
_TEXT_EXTENSIONS = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".html",
        ".css",
        ".scss",
        ".less",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".md",
        ".markdown",
        ".rst",
        ".txt",
        ".csv",
        ".xml",
        ".log",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".bat",
        ".ps1",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".java",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".swift",
        ".kt",
        ".scala",
        ".lua",
        ".r",
        ".sql",
        ".gitignore",
        ".env",
        ".dockerfile",
        ".makefile",
        ".editorconfig",
        ".lock",
        ".gradle",
        ".sbt",
    }
)

# 二进制文件嗅探：读取前 8192 字节，如果含 null 字节则视为二进制
_BINARY_NULL_LIMIT = 8192


def _is_binary_file(file_path: Path) -> bool:
    """快速判断文件是否为二进制"""
    ext = file_path.suffix.lower()
    # 已知文本扩展名 → 直接返回 False，无需遍历和嗅探（热路径优化）
    if ext in _TEXT_EXTENSIONS:
        return False
    # 简单扩展名命中二进制集合 → 直接返回 True
    if ext in _BINARY_EXTENSIONS:
        return True
    # 复合扩展名（如 .tar.gz, .min.js）→ 检查完整文件名
    for bin_ext in _BINARY_EXTENSIONS:
        if file_path.name.endswith(bin_ext):
            return True
    # 二进制嗅探：读前 8KB，含 null 字节就跳过
    try:
        with open(file_path, "rb") as f:
            head = f.read(_BINARY_NULL_LIMIT)
        if b"\0" in head:
            return True
    except Exception:
        return True  # 无法读取就当二进制跳过
    return False


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


def _glob_match(rel_path: str, pattern: str) -> bool:
    """
    标准 glob 语义匹配（忽略大小写，跨平台一致）。

    与 fnmatch.translate + re.search 实现的差异修复：
    - '*' 只匹配单个目录层级，不跨路径分隔符
    - '**' 匹配零个或多个目录层级
    - '?' 匹配单个字符，不跨分隔符
    - 路径分隔符统一按 '/' 处理（Windows 反斜杠同样兼容）

    Args:
        rel_path: 相对路径（/ 或 \\ 分隔均可）
        pattern: glob 模式，如 "*.py"、"**/*.py"、"src/**/*.ts"

    Returns:
        是否匹配
    """
    rel_parts = rel_path.replace("\\", "/").split("/")
    if not pattern or pattern in ("/", "."):
        return False
    pat_parts = [p for p in pattern.replace("\\", "/").split("/") if p not in ("", ".")]

    # DP 匹配：dp[i] 表示 rel_parts[:i] 已被模式段消费
    dp = [False] * (len(rel_parts) + 1)
    dp[0] = True
    for pat in pat_parts:
        ndp = [False] * (len(rel_parts) + 1)
        if pat == "**":
            # '**' 可吞掉零个或多个层级
            first = next((i for i, v in enumerate(dp) if v), None)
            if first is not None:
                for k in range(first, len(rel_parts) + 1):
                    ndp[k] = True
        else:
            for i, part in enumerate(rel_parts):
                if dp[i] and fnmatch.fnmatchcase(part.lower(), pat.lower()):
                    ndp[i + 1] = True
        dp = ndp
    return dp[len(rel_parts)]


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
                    f"建议: 请先重新读取文件(Read)确认最新内容后再进行编辑。",
                )
        except OSError:
            pass

        return None

    def read_file(
        self, path: str, startline: int = 1, endline: int | None = None, show_line_numbers: bool = False
    ) -> ToolResult:
        """
        读取文件内容

        Args:
            path: 文件路径
            startline: 起始行号（从1开始）
            endline: 结束行号（包含，从1开始）。不传时默认从 startline 读取 500 行
            show_line_numbers: 是否显示行号，默认 False（返回原文）

        读取时记录文件的修改时间，用于后续编辑时检测文件是否被外部修改

        对图片文件（.png/.jpg/.jpeg/.gif/.webp/.bmp）自动以二进制读取，
        返回 base64 编码数据（通过 image_data 字段），视觉模型可自动注入上下文。
        """
        try:
            full_path = self._resolve_path(path)
            if not full_path.exists():
                return ToolResult(False, error=f"File not found: {path}")

            if full_path.is_dir():
                return self.list_directory(path)

            # ===== 图片文件检测：自动读取为 base64，供视觉模型使用 =====
            ext = full_path.suffix.lower()
            if ext in IMAGE_EXTENSIONS:
                import base64

                with open(full_path, "rb") as f:
                    img_bytes = f.read()
                img_b64 = base64.b64encode(img_bytes).decode("utf-8")

                _MIME_MAP = {
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".gif": "image/gif",
                    ".webp": "image/webp",
                    ".bmp": "image/bmp",
                }
                mime = _MIME_MAP.get(ext, "image/png")

                try:
                    display_path = str(full_path.relative_to(self.workdir))
                except ValueError:
                    display_path = path

                file_size_kb = len(img_bytes) / 1024
                text_preview = f"[图片: {display_path} ({file_size_kb:.1f} KB, {ext.upper()})]"
                self._file_mtimes[str(full_path)] = full_path.stat().st_mtime

                return ToolResult(
                    success=True,
                    content=text_preview,
                    image_data={"mime": mime, "data": img_b64},
                )

            # 记录文件修改时间
            self._file_mtimes[str(full_path)] = full_path.stat().st_mtime

            from itertools import islice

            start_idx = max(0, startline - 1)
            if endline is not None:
                # endline 为含尾的 1-based 行号，恰好等于 islice 的 0-based 停止索引
                # （islice(f, start, stop) 取索引 [start, stop)，含 stop-1 对应的行）
                read_end = endline
            else:
                # 默认从 startline 起读 500 行：stop 必须是 0-based 偏移
                # （用 start_idx 而非 startline，避免多读 1 行导致分段行号错位）
                read_end = start_idx + 500

            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                # 使用 islice 按需读取，避免大文件全量 readlines()
                content_slice = list(islice(f, start_idx, read_end))

            end_idx = start_idx + len(content_slice)
            # 如果实际行数少于请求量，说明已到文件末尾，可准确获知总行数
            if len(content_slice) < (read_end - start_idx):
                total_lines = end_idx
            else:
                total_lines = f"{end_idx}+"

            # 文件头用相对路径（根目录外 fallback 到原始路径）
            try:
                display_path = str(full_path.relative_to(self.workdir))
            except ValueError:
                display_path = path
            res_info = f"#File: {display_path} (Lines {start_idx + 1}-{end_idx} of {total_lines})\n\n#Content:\n"
            if show_line_numbers:
                # 带行号格式
                formatted_content = "".join(f"{i + start_idx + 1:6d}|{line}" for i, line in enumerate(content_slice))

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
                    error=(f"Access denied: {file_path} is outside the tool-results directory."),
                )
            if not p.exists() or p.suffix != ".txt":
                return ToolResult(
                    False,
                    error=f"Persisted output not found: {file_path}",
                )

            content = p.read_text(encoding="utf-8", errors="replace")

            # 格式化内容以提高 LLM 可读性:
            #   1. JSON 内容 prettify 为多行
            #   2. 非 JSON 超长单行强制在 100 字符处换行
            stripped = content.strip()
            if stripped and stripped[0] in ("{", "["):
                try:
                    import orjson

                    parsed = orjson.loads(stripped)
                    formatted = orjson.dumps(parsed, option=orjson.OPT_INDENT_2).decode("utf-8")
                    if len(formatted) < len(content) * 3:  # 防止异常膨胀(格式化的 JSON 通常 < 3x)
                        content = formatted
                except Exception:
                    pass  # 非 JSON 或解析失败, 保持原样
            elif "\n" not in content and len(content) > 100:
                # 非 JSON 超长单行: 强制换行, 避免 LLM 无法定位行尾
                wrapped_lines = []
                for i in range(0, len(content), 100):
                    wrapped_lines.append(content[i : i + 100])
                content = "\n".join(wrapped_lines)

            # 二级软截断: 避免"恢复"时再爆 context
            if len(content) > 200_000:
                content = (
                    content[:200_000] + f"\n\n[Truncated at 200,000 chars. Full size on disk: {p.stat().st_size} bytes]"
                )
            return ToolResult(True, content=content)
        except Exception as e:
            return ToolResult(False, error=f"Read persisted output error: {str(e)}")

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
                diff_lines = list(
                    difflib.unified_diff(
                        old_content.splitlines(), (content or "").splitlines(), fromfile=path, tofile=path, lineterm=""
                    )
                )
                diff_str = "\n".join(diff_lines)

            # 更新修改时间记录
            self._file_mtimes[str(full_path)] = full_path.stat().st_mtime

            return ToolResult(True, content=f"Successfully written to {path}", diff=diff_str or None)
        except Exception as e:
            return ToolResult(False, error=f"Write error: {str(e)}")

    def edit_file(self, path: str, oldString: str, newString: str, replaceAll: bool = False) -> ToolResult:
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
                    "Ensure exact match including whitespace and indentation.",
                )

            if count > 1 and not replaceAll:
                return ToolResult(
                    False,
                    error=f"The 'oldString' appears {count} times in the file. "
                    f"Please provide a more specific context to ensure uniqueness, "
                    f"or set replaceAll=True.",
                )

            new_content = old_content.replace(oldString, newString, -1 if replaceAll else 1)
            full_path.write_text(new_content, encoding="utf-8")
            self._file_mtimes[str(full_path)] = full_path.stat().st_mtime

            # ── 生成 unified diff ──
            diff_lines = list(
                difflib.unified_diff(
                    old_content.splitlines(), new_content.splitlines(), fromfile=path, tofile=path, lineterm=""
                )
            )
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
            diff_lines = list(
                difflib.unified_diff(
                    old_content.splitlines(), content.splitlines(), fromfile=path, tofile=path, lineterm=""
                )
            )
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

    def grep_files(
        self,
        pattern: str,
        path: str = ".",
        include: str = None,
        multiline: bool = False,
        max_size_mb: int = 1,
        workers: int = 4,
    ) -> ToolResult:
        """
        高性能 grep：并行搜索文件内容

        Args:
            pattern: 搜索正则表达式
            path: 搜索路径（默认 workdir）
            include: 文件名过滤（fnmatch 模式，如 "*.py"）
            multiline: 是否启用跨行搜索（默认 False）
            max_size_mb: 单文件最大 MB，超过跳过（默认 1MB，防大文件卡死）
            workers: 并行工作线程数（默认 4）

        Returns:
            ToolResult: 匹配结果
        """
        try:
            search_root = self._resolve_path(path)
            if not search_root.exists():
                return ToolResult(False, error=f"Path not found: {path}")

            # ── 支持直接传入文件路径 ──
            if search_root.is_file():
                return self._grep_single_file(search_root, pattern)

            regex = _compile_grep_pattern(pattern)

            # ── 1. 收集待搜索文件列表 ──
            gitignore_patterns = _load_gitignore_patterns(search_root)
            file_paths: list[Path] = []

            for root, dirs, files in os.walk(search_root):
                # 1a. 硬编码排除目录
                dirs[:] = [d for d in dirs if d not in _GREP_EXCLUDE_DIRS]
                # 1b. .gitignore 排除目录
                if gitignore_patterns:
                    dirs[:] = [d for d in dirs if not any(fnmatch.fnmatch(d, p) for p in gitignore_patterns)]
                for filename in files:
                    if include and not fnmatch.fnmatch(filename, include):
                        continue
                    # 1c. .gitignore 排除文件
                    if gitignore_patterns and any(fnmatch.fnmatch(filename, p) for p in gitignore_patterns):
                        continue
                    fp = Path(root) / filename
                    # 1d. 跳过二进制 / 大文件
                    if _is_binary_file(fp):
                        continue
                    try:
                        if fp.stat().st_size > max_size_mb * 1024 * 1024:
                            continue
                    except OSError:
                        continue
                    file_paths.append(fp)

            if not file_paths:
                return ToolResult(True, content="No matching files found to search.")

            # ── 2. 并行搜索 ──
            all_results: list[str] = []
            result_limit = 100
            scanned_count = 0
            start_time = time.time()

            # 每搜到一点就打日志（防长时间无反馈）
            _log_interval = max(1, len(file_paths) // 10)

            def _search_single(fp: Path) -> list[str]:
                """单个文件的搜索任务"""
                hits: list[str] = []
                try:
                    if multiline:
                        # 跨行模式：整个文件一次读取 + re.DOTALL
                        text = fp.read_text(encoding="utf-8", errors="ignore")
                        for m in regex.finditer(text):
                            # 截取匹配行周围的上下文（取匹配起始行）
                            line_num = text[: m.start()].count("\n") + 1
                            try:
                                rel = fp.relative_to(self.workdir)
                            except ValueError:
                                rel = fp
                            hits.append(f"{rel}:{line_num}: {m.group()[:200]}")
                    else:
                        # 逐行模式
                        with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                            for i, line in enumerate(f, 1):
                                if regex.search(line):
                                    try:
                                        rel = fp.relative_to(self.workdir)
                                    except ValueError:
                                        rel = fp
                                    hits.append(f"{rel}:{i}: {line.strip()[:300]}")
                except Exception:
                    pass
                return hits

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_search_single, fp): fp for fp in file_paths}
                for i, future in enumerate(as_completed(futures)):
                    scanned_count += 1
                    if scanned_count % _log_interval == 0:
                        elapsed = time.time() - start_time
                    hits = future.result()
                    all_results.extend(hits)
                    if len(all_results) >= result_limit:
                        # 达到上限，取消剩余任务
                        for f in futures:
                            f.cancel()
                        break

            elapsed = time.time() - start_time
            logger.info(
                f"[grep] 完成：扫描 {scanned_count}/{len(file_paths)} 个文件"
                f"，耗时 {elapsed:.1f}s，命中 {len(all_results)} 条"
            )

            # ── 3. 组装结果 ──
            if not all_results:
                content = (
                    f"No matches found for pattern: {pattern}"
                    f" (scanned {scanned_count} files, skipped binaries/large files)"
                )
                return ToolResult(True, content=content)

            content = "\n".join(all_results[:result_limit])
            if len(content) > MAX_GREP_CONTENT_LENGTH:
                content = (
                    content[:MAX_GREP_CONTENT_LENGTH]
                    + f"\n\n... (Content truncated, exceeds {MAX_GREP_CONTENT_LENGTH} characters limit)"
                )
            meta = f"# Search: {pattern} | {len(all_results)} matches in {scanned_count} files ({elapsed:.1f}s)\n\n"
            return ToolResult(True, content=meta + content)

        except Exception as e:
            return ToolResult(False, error=f"Grep error: {str(e)}")

    def _grep_single_file(self, file_path: Path, pattern: str) -> ToolResult:
        """在单个文件中执行 grep 搜索"""
        try:
            regex = _compile_grep_pattern(pattern)
            # 跳过二进制文件
            if _is_binary_file(file_path):
                return ToolResult(True, content=f"Skipped binary file: {file_path.name}")
            try:
                if file_path.stat().st_size > 1 * 1024 * 1024:
                    return ToolResult(True, content=f"File too large (>1MB): {file_path.name}")
            except OSError:
                return ToolResult(False, error=f"Cannot access file: {file_path}")

            results: list[str] = []
            try:
                rel = file_path.relative_to(self.workdir)
            except ValueError:
                rel = file_path

            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f, 1):
                    if regex.search(line):
                        results.append(f"{rel}:{i}: {line.strip()[:300]}")
                        if len(results) >= 100:
                            break

            if not results:
                return ToolResult(True, content=f"No matches found in {rel}")
            meta = f"# Search: {pattern} in {rel} | {len(results)} matches\n\n"
            return ToolResult(True, content=meta + "\n".join(results))
        except Exception as e:
            return ToolResult(False, error=f"Grep single file error: {str(e)}")

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

    def glob_files(self, pattern: str, path: str = ".", max_results: int = 100, workers: int = 4) -> ToolResult:
        """
        高性能 glob：通过通配符查找文件（带排除和并行收集）

        Args:
            pattern: 通配符模式，如 "*.py", "**/*.tsx", "src/**/*.css"
            path: 搜索路径（默认 workdir）
            max_results: 最大返回数（默认 100）
            workers: 并行工作线程数（默认 4）

        Returns:
            ToolResult: 匹配的文件列表
        """
        try:
            search_path = self._resolve_path(path)
            if not search_path.exists():
                return ToolResult(False, error=f"Path not found: {path}")
            if not search_path.is_dir():
                return ToolResult(False, error=f"Not a directory: {path}")

            gitignore_patterns = _load_gitignore_patterns(search_path)

            # ── 1. 收集候选文件列表（os.walk + 排除） ──
            candidates: list[Path] = []
            for root, dirs, files in os.walk(search_path):
                dirs[:] = [d for d in dirs if d not in _GREP_EXCLUDE_DIRS]
                if gitignore_patterns:
                    dirs[:] = [d for d in dirs if not any(fnmatch.fnmatch(d, p) for p in gitignore_patterns)]
                for filename in files:
                    if gitignore_patterns and any(fnmatch.fnmatch(filename, p) for p in gitignore_patterns):
                        continue
                    candidates.append(Path(root) / filename)

            if not candidates:
                return ToolResult(True, content="No files found (all excluded by .gitignore or hardcoded rules).")

            start_time = time.time()

            # ── 2. 并行 glob 匹配（标准语义：* 不跨目录，** 跨任意层级） ──
            results: list[str] = []

            def _match_single(fp: Path) -> str | None:
                """单文件 glob 匹配"""
                # 匹配基准：优先相对搜索起点，其次相对 workdir，兜底绝对路径
                try:
                    rel_match = fp.relative_to(search_path).as_posix()
                except ValueError:
                    try:
                        rel_match = fp.relative_to(self.workdir).as_posix()
                    except ValueError:
                        rel_match = str(fp).replace("\\", "/")
                if not _glob_match(rel_match, pattern):
                    return None
                # 输出：相对 workdir（与 read 等工具一致），越界则 fallback 绝对路径
                try:
                    return str(fp.relative_to(self.workdir))
                except ValueError:
                    return str(fp)

            # Path.rglob 在剔除排除目录后没意义了，因为我们自己 walk 了
            # 小规模直接串行，大批量并行
            if len(candidates) < 500:
                for fp in candidates:
                    r = _match_single(fp)
                    if r:
                        results.append(r)
                        if len(results) >= max_results:
                            break
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(_match_single, fp): fp for fp in candidates}
                    for future in as_completed(futures):
                        r = future.result()
                        if r:
                            results.append(r)
                            if len(results) >= max_results:
                                for f in futures:
                                    f.cancel()
                                break

            elapsed = time.time() - start_time
            logger.info(f"[glob] {pattern}: {len(results)} matches in {len(candidates)} candidates ({elapsed:.2f}s)")

            if not results:
                return ToolResult(True, content=f"No files matched pattern: {pattern}")

            content = "\n".join(results)
            if len(content) > MAX_GREP_CONTENT_LENGTH:
                meta = f"# Glob: {pattern} | {len(results)} matches ({elapsed:.2f}s)\n\n"
                content = (
                    content[:MAX_GREP_CONTENT_LENGTH]
                    + f"\n\n... (Content truncated, exceeds {MAX_GREP_CONTENT_LENGTH} characters limit)"
                )
                return ToolResult(True, content=meta + content)

            return ToolResult(True, content=content)
        except Exception as e:
            return ToolResult(False, error=f"Glob error: {str(e)}")

    def scan_repo(
        self,
        path: str = None,
        max_depth: int = 2,
        include: str = None,
        exclude: List[str] = None,
    ) -> ToolResult:
        """扫描仓库目录并返回结构化摘要。

        Args:
            path: 扫描起始路径（默认 workdir）
            max_depth: 最大扫描深度
            include: 仅显示匹配的文件名（fnmatch 模式，如 "*.py"）
            exclude: 额外排除的目录（fnmatch 模式列表，如 [".venv*", "tmp-*"]），
                     默认集合见 _SCAN_EXCLUDE_DIRS
        """
        try:
            target_path = self._resolve_path(path) if path else self.workdir
            if not target_path.exists():
                return ToolResult(False, error=f"Path not found: {target_path}")

            try:
                scan_display = str(target_path.relative_to(self.workdir))
            except ValueError:
                scan_display = str(target_path)

            extra_excludes = list(exclude) if exclude else []
            # 默认集合 + 用户传入统一走 fnmatch，确保通配符在两边都生效
            all_patterns = (*_SCAN_EXCLUDE_DIRS, *extra_excludes)

            def _is_excluded(d: str) -> bool:
                return any(fnmatch.fnmatch(d, pat) for pat in all_patterns)

            lines = [f"Repository scan: {scan_display}"]
            root_depth = len(target_path.parts)

            for root, dirs, files in os.walk(target_path):
                rel_depth = len(Path(root).parts) - root_depth
                if rel_depth > max_depth:
                    dirs[:] = []
                    continue

                dirs[:] = [d for d in dirs if not _is_excluded(d)]

                visible_files = files
                if include:
                    visible_files = [f for f in files if fnmatch.fnmatch(f, include)]

                rel_root = Path(root).relative_to(target_path)
                display_root = "." if str(rel_root) == "." else str(rel_root)
                lines.append(f"\n[{display_root}]")

                sample_dirs = sorted(dirs)[:8]
                sample_files = sorted(visible_files)[:12]
                if sample_dirs:
                    lines.append("dirs: " + ", ".join(sample_dirs))
                if sample_files:
                    lines.append("files: " + ", ".join(sample_files))

            return ToolResult(True, content="\n".join(lines[:200]))
        except Exception as e:
            return ToolResult(False, error=f"scan_repo error: {str(e)}")

    def diff_files(self, file1: str, file2: str = None, use_git: bool = False) -> ToolResult:
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
                    return ToolResult(True, content=f"No changes in {file1} (compared to git)")
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
                    return ToolResult(False, error="Not a git repository and no second file provided")
                return ToolResult(
                    True,
                    content=result.stdout if result.stdout else f"No changes in {file1} (compared to git HEAD)",
                )

            if not result.stdout:
                return ToolResult(True, content="Files are identical")
            return ToolResult(True, content=result.stdout)
        except Exception as e:
            return ToolResult(False, error=f"Diff error: {str(e)}")

    # ========== Gitee 上传 ==========

    def gitee_upload(self, local_path: str) -> ToolResult:
        """
        将本地文件上传至 Gitee 仓库，返回公开下载链接。

        AI 可调用此工具上传文件到 Gitee 图床，
        Gateway 适配器在发送文件/图片时会自动调用。

        Args:
            local_path: 本地文件路径（绝对路径或相对 workdir 的路径）

        Returns:
            ToolResult:
                成功: content 包含 {"url": "https://...", "filename": "..."}
                失败: error 包含错误描述
        """
        from app.gateway.utils.gitee_uploader import GiteeUploader

        full_path = self._resolve_path(local_path)
        uploader = GiteeUploader.get_instance()

        if not uploader.is_configured():
            return ToolResult(
                False,
                error="Gitee 未配置。请在设置中填写 Gitee Token、Owner、Repo。",
            )

        url, err = uploader.upload_file(str(full_path))
        if err:
            return ToolResult(False, error=f"Gitee 上传失败: {err}")

        return ToolResult(
            True,
            content={
                "url": url,
                "filename": full_path.name,
                "local_path": str(full_path),
            },
        )

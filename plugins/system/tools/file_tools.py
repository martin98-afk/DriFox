# -*- coding: utf-8 -*-
"""
系统工具插件 — 文件操作工具（自包含实现）

工具逻辑完全自包含：不依赖主程序 BuiltinTools/FileTools，纯标准库实现。
运行环境仅从 tool_ctx 获取（workdir 工作目录）。impl 返回 ToolResult 协议对象
（仅类型依赖，用于携带 diff/image_data 等扩展字段）。

核心行为对齐主程序原实现：
- read：行区间读取 + 图片 base64 + mtime 外部修改检测
- write：自动建目录
- edit：oldString 唯一性校验 + unified diff 产出
- grep/glob/list/scan_repo：排除目录集合与主程序一致
"""
import base64
import difflib
import fnmatch
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import islice
from pathlib import Path
from typing import Dict, List, Optional

from app.tools.result import ToolResult

GROUP_READ = "文件读取"
GROUP_WRITE = "文件写入"

# ========== 常量（与主程序对齐） ==========

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
_MAX_GREP_CONTENT_LENGTH = 15000

_GREP_EXCLUDE_DIRS = frozenset(
    {
        ".drifox", ".mypy_cache", ".git", "node_modules", "__pycache__",
        "venv", ".venv", "dist", "build", ".idea", ".vscode",
    }
)

_SCAN_EXCLUDE_DIRS = frozenset(
    {
        ".drifox", ".mypy_cache*", ".git*", "node_modules*", "__pycache__",
        "venv*", ".venv*", "dist*", "build*", ".idea*", ".vscode*",
        ".pytest_cache*", ".tox*", "site-packages*", ".eggs*",
        ".ipynb_checkpoints*", "htmlcov*", ".ruff_cache*",
        ".next*", ".nuxt*", ".svelte-kit*", ".cache*", ".parcel-cache*",
        ".turbo*", ".nyc_output*", "coverage*", ".vercel*",
        "target*", "cmake-build-debug*", "cmake-build-release*",
        ".gradle*", "out*",
    }
)

_BINARY_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".zip",
     ".tar", ".gz", ".7z", ".exe", ".dll", ".so", ".dylib", ".pyc", ".pyo",
     ".woff", ".woff2", ".ttf", ".otf", ".eot", ".bin", ".dat", ".db", ".sqlite"}
)

_TEXT_EXTENSIONS = frozenset(
    {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".scss", ".json",
     ".md", ".txt", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".sh",
     ".bat", ".cmd", ".ps1", ".c", ".h", ".cpp", ".hpp", ".java", ".kt", ".go",
     ".rs", ".rb", ".php", ".swift", ".m", ".sql", ".xml", ".svg", ".vue", ".svelte"}
)

_BINARY_NULL_LIMIT = 8192

# 模块级 mtime 缓存（外部修改检测）
_file_mtimes: Dict[str, float] = {}


# ========== 内部 helper（自包含） ==========

def _resolve(workdir: Optional[Path], path: str) -> Path:
    """解析路径：绝对路径直接用；~ 展开；相对路径基于 workdir"""
    if not path:
        return Path(workdir or Path.cwd())
    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    base = workdir or Path.cwd()
    return base / p


def _display_path(workdir: Optional[Path], full_path: Path, original: str) -> str:
    """显示路径：workdir 内用相对路径，外部回退原始路径"""
    if workdir is not None:
        try:
            return str(full_path.relative_to(workdir))
        except ValueError:
            pass
    return original


def _check_modified(workdir: Optional[Path], full_path: Path, original: str) -> Optional[ToolResult]:
    """外部修改检测：read 后文件被外部改动则拒绝编辑"""
    key = str(full_path)
    recorded = _file_mtimes.get(key)
    if recorded is not None:
        try:
            current = full_path.stat().st_mtime
        except OSError:
            return None
        if abs(current - recorded) > 1e-6:
            return ToolResult(
                False,
                error=(
                    f"File has been modified externally since it was read: {_display_path(workdir, full_path, original)}. "
                    f"Re-read the file to get the latest content before editing."
                ),
            )
    return None


def _is_binary(file_path: Path) -> bool:
    """二进制文件检测：扩展名 + NUL 字节启发式"""
    ext = file_path.suffix.lower()
    if ext in _BINARY_EXTENSIONS:
        return True
    if ext in _TEXT_EXTENSIONS:
        return False
    try:
        with open(file_path, "rb") as f:
            head = f.read(_BINARY_NULL_LIMIT)
        return b"\x00" in head
    except OSError:
        return True


def _excluded_dir(name: str, patterns: frozenset) -> bool:
    """目录排除判断（fnmatch 通配符）"""
    for pat in patterns:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def _compile_grep_pattern(pattern: str) -> re.Pattern:
    """编译 grep 正则：支持 (?i) 等内联标志"""
    if pattern.startswith("(?i)"):
        return re.compile(pattern[4:], re.IGNORECASE)
    return re.compile(pattern)


# ========== read ==========

_READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read",
        "description": "读文件。返回原文，可选行号。支持文本/图片(.png/.jpg/.jpeg/.gif/.webp/.bmp)，图片返base64。记录mtime检测外部修改。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "startline": {"type": "integer", "description": "起始行号 (从1开始)", "default": 1},
                "endline": {"type": "integer", "description": "结束行号(从1开始,含)。不传默认从 startline 起读 500 行"},
                "show_line_numbers": {"type": "boolean", "description": "是否显示行号，默认 False", "default": False},
            },
            "required": ["path"],
        },
    },
}


def _read_impl(tool_ctx, **kwargs):
    workdir = Path(tool_ctx["workdir"]) if tool_ctx.get("workdir") else None
    path = kwargs.get("path", "")
    startline = int(kwargs.get("startline") or 1)
    endline = kwargs.get("endline")
    show_line_numbers = kwargs.get("show_line_numbers", False)
    try:
        full_path = _resolve(workdir, path)
        if not full_path.exists():
            return ToolResult(False, error=f"File not found: {path}")
        if full_path.is_dir():
            return _list_impl(tool_ctx, path=str(full_path))
        display = _display_path(workdir, full_path, path)

        # 图片：base64 返回（视觉模型注入）
        ext = full_path.suffix.lower()
        if ext in IMAGE_EXTENSIONS:
            img_bytes = full_path.read_bytes()
            mime_map = {
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
            }
            mime = mime_map.get(ext, "image/png")
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            size_kb = len(img_bytes) / 1024
            preview = f"[图片: {display} ({size_kb:.1f} KB, {ext.upper()})]"
            _file_mtimes[str(full_path)] = full_path.stat().st_mtime
            return ToolResult(True, content=preview, image_data={"mime": mime, "data": img_b64})

        _file_mtimes[str(full_path)] = full_path.stat().st_mtime
        start_idx = max(0, startline - 1)
        read_end = endline if endline is not None else start_idx + 500
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            content_slice = list(islice(f, start_idx, read_end))
        end_idx = start_idx + len(content_slice)
        total_lines = end_idx if len(content_slice) < (read_end - start_idx) else f"{end_idx}+"
        header = f"#File: {display} (Lines {startline}-{end_idx} of {total_lines})"
        if show_line_numbers:
            body = "\n".join(
                f"{start_idx + i}:{line.rstrip(chr(10))}" for i, line in enumerate(content_slice)
            )
        else:
            body = "".join(content_slice).rstrip(chr(10))
        return ToolResult(True, content=f"{header}\n{body}")
    except Exception as e:
        return ToolResult(False, error=f"Read error: {str(e)}")


# ========== write ==========

_WRITE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write",
        "description": "创建/覆盖文件。自动建目录。超大文件用多次 edit 写入。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件相对路径"},
                "content": {"type": "string", "description": "完整的文件内容"},
            },
            "required": ["path", "content"],
        },
    },
}


def _write_impl(tool_ctx, **kwargs):
    workdir = Path(tool_ctx["workdir"]) if tool_ctx.get("workdir") else None
    path = kwargs.get("path", "")
    content = kwargs.get("content", "")
    try:
        full_path = _resolve(workdir, path)
        # 外部修改检测 + 旧内容（diff 计算用）
        check = _check_modified(workdir, full_path, path)
        if check:
            return check
        old_content = ""
        try:
            if full_path.exists():
                old_content = full_path.read_text(encoding="utf-8")
        except Exception:
            old_content = ""
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        _file_mtimes[str(full_path)] = full_path.stat().st_mtime
        # unified diff（新建/覆盖均生成，与主程序行为一致）
        diff_str = ""
        if old_content or content:
            diff_lines = list(
                difflib.unified_diff(
                    old_content.splitlines(), (content or "").splitlines(),
                    fromfile=path, tofile=path, lineterm="",
                )
            )
            diff_str = "\n".join(diff_lines) if diff_lines else ""
        display = _display_path(workdir, full_path, path)
        return ToolResult(True, content=f"已写入 {display} ({len(content)} 字符)", diff=diff_str)
    except Exception as e:
        return ToolResult(False, error=f"Write error: {str(e)}")


# ========== edit ==========

_EDIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "edit",
        "description": "精确文本替换。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "oldString": {"type": "string", "description": "旧文本(精确匹配，含空白)"},
                "newString": {"type": "string", "description": "替换后的新文本"},
                "replaceAll": {"type": "boolean", "description": "替换全部匹配(默认False)。oldString重复时设True", "default": False},
            },
            "required": ["path", "oldString", "newString"],
        },
    },
}


def _edit_impl(tool_ctx, **kwargs):
    workdir = Path(tool_ctx["workdir"]) if tool_ctx.get("workdir") else None
    path = kwargs.get("path", "")
    old_string = kwargs.get("oldString", "")
    new_string = kwargs.get("newString", "")
    replace_all = kwargs.get("replaceAll", False)
    try:
        full_path = _resolve(workdir, path)
        if not full_path.exists():
            return ToolResult(False, error=f"File not found: {path}")
        check = _check_modified(workdir, full_path, path)
        if check:
            return check
        old_content = full_path.read_text(encoding="utf-8", errors="replace")
        count = old_content.count(old_string)
        if count == 0:
            return ToolResult(
                False,
                error="The specified 'oldString' was not found in the file. "
                "Ensure exact match including whitespace and indentation.",
            )
        if count > 1 and not replace_all:
            return ToolResult(
                False,
                error=f"The 'oldString' appears {count} times in the file. "
                f"Please provide a more specific context to ensure uniqueness, or set replaceAll=True.",
            )
        new_content = old_content.replace(old_string, new_string, -1 if replace_all else 1)
        full_path.write_text(new_content, encoding="utf-8")
        _file_mtimes[str(full_path)] = full_path.stat().st_mtime
        diff_lines = list(
            difflib.unified_diff(
                old_content.splitlines(), new_content.splitlines(),
                fromfile=path, tofile=path, lineterm="",
            )
        )
        diff_str = "\n".join(diff_lines) if diff_lines else ""
        display = _display_path(workdir, full_path, path)
        return ToolResult(True, content=f"已编辑 {display}（{count} 处匹配，替换 {1 if not replace_all else count} 处）", diff=diff_str)
    except Exception as e:
        return ToolResult(False, error=f"Edit error: {str(e)}")


# ========== multi_edit ==========

_MULTI_EDIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "multi_edit",
        "description": "批量编辑同文件。多次替换后生成 unified diff 审查。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "edits": {
                    "type": "array",
                    "description": "编辑列表。每项{oldString,newString}，按序替换首个匹配",
                    "items": {
                        "type": "object",
                        "properties": {
                            "oldString": {"type": "string", "description": "要替换的旧文本"},
                            "newString": {"type": "string", "description": "替换后的新文本"},
                        },
                        "required": ["oldString", "newString"],
                    },
                },
            },
            "required": ["path", "edits"],
        },
    },
}


def _multi_edit_impl(tool_ctx, **kwargs):
    workdir = Path(tool_ctx["workdir"]) if tool_ctx.get("workdir") else None
    path = kwargs.get("path", "")
    edits = kwargs.get("edits", [])
    try:
        full_path = _resolve(workdir, path)
        if not full_path.exists():
            return ToolResult(False, error=f"File not found: {path}")
        check = _check_modified(workdir, full_path, path)
        if check:
            return check
        old_content = full_path.read_text(encoding="utf-8", errors="replace")
        current = old_content
        for i, edit in enumerate(edits or []):
            old_s, new_s = edit.get("oldString", ""), edit.get("newString", "")
            if old_s not in current:
                return ToolResult(
                    False,
                    error=f"Edit #{i + 1} failed: oldString not found (文件可能已被前面的编辑改动)。",
                )
            current = current.replace(old_s, new_s, 1)
        full_path.write_text(current, encoding="utf-8")
        _file_mtimes[str(full_path)] = full_path.stat().st_mtime
        diff_lines = list(
            difflib.unified_diff(
                old_content.splitlines(), current.splitlines(),
                fromfile=path, tofile=path, lineterm="",
            )
        )
        diff_str = "\n".join(diff_lines) if diff_lines else ""
        display = _display_path(workdir, full_path, path)
        return ToolResult(True, content=f"已批量编辑 {display}（{len(edits)} 处）", diff=diff_str)
    except Exception as e:
        return ToolResult(False, error=f"MultiEdit error: {str(e)}")


# ========== grep ==========

_GREP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "grep",
        "description": "递归搜索正则匹配内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {"type": "string", "description": "起始搜索目录 (默认当前目录)", "default": "."},
                "include": {"type": "string", "description": "文件过滤模式 (如 '*.py')"},
            },
            "required": ["pattern"],
        },
    },
}


def _grep_single(workdir: Optional[Path], file_path: Path, pattern: re.Pattern, original: str) -> Optional[ToolResult]:
    """单文件 grep（含行号匹配）"""
    if _is_binary(file_path):
        return None
    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    matches = []
    for i, line in enumerate(lines, 1):
        if pattern.search(line):
            matches.append(f"{i}:{line[:200]}")
    if not matches:
        return None
    display = _display_path(workdir, file_path, original)
    content = "\n".join(matches)
    if len(content) > _MAX_GREP_CONTENT_LENGTH:
        content = content[:_MAX_GREP_CONTENT_LENGTH] + "\n...(截断)"
    return ToolResult(True, content=f"匹配文件: {display}\n{content}")


def _grep_impl(tool_ctx, **kwargs):
    workdir = Path(tool_ctx["workdir"]) if tool_ctx.get("workdir") else None
    pattern_str = kwargs.get("pattern", "")
    path = kwargs.get("path", "") or "."
    include = kwargs.get("include")
    try:
        search_root = _resolve(workdir, path)
        if not search_root.exists():
            return ToolResult(False, error=f"Path not found: {path}")
        pattern = _compile_grep_pattern(pattern_str)
        if search_root.is_file():
            result = _grep_single(workdir, search_root, pattern, path)
            return result or ToolResult(True, content="无匹配")

        # 目录：递归搜索（并行收集）
        def _search_file(fp: Path):
            if _excluded_dir(fp.name, _GREP_EXCLUDE_DIRS) and fp.is_dir():
                return None
            if include and not fnmatch.fnmatch(fp.name, include):
                return None
            return _grep_single(workdir, fp, pattern, str(fp))

        results = []
        files = []
        for root, dirs, names in os.walk(search_root):
            dirs[:] = [d for d in dirs if not _excluded_dir(d, _GREP_EXCLUDE_DIRS)]
            for name in names:
                files.append(Path(root) / name)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_search_file, fp) for fp in files]
            for fut in as_completed(futures):
                r = fut.result()
                if r:
                    results.append(r)
        if not results:
            return ToolResult(True, content=f"无匹配（pattern: {pattern_str}）")
        return ToolResult(
            True,
            content=f"搜索到 {len(results)} 个文件:\n" + "\n".join(str(r.content) for r in results)[:_MAX_GREP_CONTENT_LENGTH],
        )
    except re.error as e:
        return ToolResult(False, error=f"无效正则: {e}")
    except Exception as e:
        return ToolResult(False, error=f"Grep error: {str(e)}")


# ========== list ==========

_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list",
        "description": "列目录。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径", "default": "."},
            },
        },
    },
}


def _list_impl(tool_ctx, **kwargs):
    workdir = Path(tool_ctx["workdir"]) if tool_ctx.get("workdir") else None
    path = kwargs.get("path", "") or "."
    try:
        target = _resolve(workdir, path)
        if not target.exists():
            return ToolResult(False, error=f"Path not found: {path}")
        if not target.is_dir():
            return ToolResult(False, error=f"Not a directory: {path}")
        display = _display_path(workdir, target, path)
        items = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        lines = []
        for item in items:
            marker = "[DIR] " if item.is_dir() else ""
            lines.append(f"{marker}{item.name}")
        return ToolResult(True, content=f"目录: {display}\n" + "\n".join(lines))
    except Exception as e:
        return ToolResult(False, error=f"List error: {str(e)}")


# ========== glob ==========

_GLOB_SCHEMA = {
    "type": "function",
    "function": {
        "name": "glob",
        "description": "通配符递归查找。支持 **, *, ? 等glob。",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "匹配模式：如 *.py, **/*.json, src/**/*.ts"},
                "path": {"type": "string", "description": "搜索起始路径 (默认当前目录)", "default": "."},
            },
            "required": ["pattern"],
        },
    },
}


def _glob_impl(tool_ctx, **kwargs):
    workdir = Path(tool_ctx["workdir"]) if tool_ctx.get("workdir") else None
    pattern = kwargs.get("pattern", "")
    path = kwargs.get("path", "") or "."
    try:
        search_path = _resolve(workdir, path)
        if not search_path.exists():
            return ToolResult(False, error=f"Path not found: {path}")
        if not search_path.is_dir():
            return ToolResult(False, error=f"Not a directory: {path}")
        # 标准 glob 语义（glob.glob recursive=True）：
        # - '*' 只匹配单个目录层级（不跨路径分隔符）
        # - '**' 匹配零或多个目录层级（recursive=True 时）
        # - '?' 匹配单字符
        # - Windows 反斜杠模式（sub\*.txt）兼容：转正斜杠
        import glob as _std_glob

        norm_pattern = pattern.replace("\\", "/")
        full_pattern = str(search_path / norm_pattern)
        raw_matches = _std_glob.glob(full_pattern, recursive=True)
        matches = []
        for fp_str in raw_matches:
            fp = Path(fp_str)
            try:
                rel = fp.relative_to(search_path)
            except ValueError:
                continue
            # 排除目录过滤（逐级检查相对路径）
            if any(_excluded_dir(part, _GREP_EXCLUDE_DIRS) for part in rel.parts[:-1]):
                continue
            if _excluded_dir(fp.name, _GREP_EXCLUDE_DIRS):
                continue
            matches.append(fp_str.replace("\\", "/"))
            if len(matches) >= 100:
                break
        if not matches:
            return ToolResult(True, content=f"未找到匹配: {pattern}")
        return ToolResult(True, content=f"找到 {len(matches)} 个文件:\n" + "\n".join(matches))
    except Exception as e:
        return ToolResult(False, error=f"Glob error: {str(e)}")



_SCAN_REPO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "scan_repo",
        "description": "扫描仓库，返回结构化摘要。编码前快速建模上下文。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "扫描路径"},
                "max_depth": {"type": "integer", "description": "最大扫描深度"},
            },
        },
    },
}


def _scan_repo_impl(tool_ctx, **kwargs):
    workdir = Path(tool_ctx["workdir"]) if tool_ctx.get("workdir") else None
    path = kwargs.get("path") or ""
    max_depth = int(kwargs.get("max_depth") or 2)
    try:
        target = _resolve(workdir, path) if path else (workdir or Path.cwd())
        if not target.exists():
            return ToolResult(False, error=f"Path not found: {target}")
        display = _display_path(workdir, target, path or str(target))

        def _walk(dirp: Path, depth: int):
            """返回 (dirs, files) 摘要"""
            dirs, files = [], []
            try:
                entries = sorted(dirp.iterdir(), key=lambda p: p.name.lower())
            except OSError:
                return dirs, files
            for entry in entries:
                if entry.is_dir():
                    if not _excluded_dir(entry.name, _SCAN_EXCLUDE_DIRS):
                        dirs.append(entry)
                else:
                    files.append(entry)
            return dirs, files

        dirs, files = _walk(target, 0)
        lines = [f"# {display}", ""]
        if dirs:
            lines.append("## 目录")
            for d in dirs:
                lines.append(f"- {d.name}/")
                if max_depth > 1:
                    sub_dirs, sub_files = _walk(d, 1)
                    for sd in sub_dirs[:30]:
                        lines.append(f"  - {sd.name}/")
                    if len(sub_dirs) > 30:
                        lines.append(f"  - ... 等 {len(sub_dirs)} 个子目录")
        lines.append("")
        if files:
            lines.append(f"## 文件 ({len(files)})")
            for f in files[:100]:
                try:
                    size = f.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"- {f.name} ({size} B)")
            if len(files) > 100:
                lines.append(f"- ... 等 {len(files)} 个文件")
        return ToolResult(True, content="\n".join(lines))
    except Exception as e:
        return ToolResult(False, error=f"ScanRepo error: {str(e)}")


# ========== stage_files ==========

_STAGE_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "stage_files",
        "description": "标记任务相关文件，聚焦后续编辑/验证。",
        "parameters": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "文件路径列表",
                },
            },
            "required": ["files"],
        },
    },
}


def _stage_files_impl(tool_ctx, **kwargs):
    workdir = Path(tool_ctx["workdir"]) if tool_ctx.get("workdir") else None
    files = kwargs.get("files", [])
    staged = []
    for file_path in files or []:
        if not file_path:
            continue
        resolved = _resolve(workdir, file_path)
        if not resolved.exists():
            continue
        staged.append(_display_path(workdir, resolved, file_path))
    if not staged:
        return ToolResult(False, error="未找到有效文件（均不存在或路径为空）")
    return ToolResult(True, content=f"已标记 {len(staged)} 个文件:\n" + "\n".join(staged))


def register(registry):
    registry.register(
        "read", _READ_SCHEMA, impl=_read_impl,
        danger="safe", icon="read", cn_name="读取",
        group=GROUP_READ, description="读取文件内容",
        aliases=["Read", "ReadFile", "ReadFiles", "cat"],
    )
    registry.register(
        "write", _WRITE_SCHEMA, impl=_write_impl,
        danger="dangerous", icon="编辑", cn_name="写入",
        group=GROUP_WRITE, description="覆盖/创建文件",
        aliases=["Write", "WriteFile", "CreateFile", "create_file"],
    )
    registry.register(
        "edit", _EDIT_SCHEMA, impl=_edit_impl,
        danger="dangerous", icon="编辑", cn_name="编辑",
        group=GROUP_WRITE, description="精确文本替换",
        aliases=["Edit", "TextEdit", "ReplaceInFile", "replace"],
    )
    registry.register(
        "multi_edit", _MULTI_EDIT_SCHEMA, impl=_multi_edit_impl,
        danger="dangerous", icon="编辑", cn_name="批量编辑",
        group=GROUP_WRITE, description="批量文件编辑",
        aliases=["MultiEdit", "MultiEditTool"],
    )
    registry.register(
        "grep", _GREP_SCHEMA, impl=_grep_impl,
        danger="safe", icon="Search", cn_name="搜索",
        group=GROUP_READ, description="正则搜索文件内容",
        aliases=["Grep", "Search", "SearchFiles", "Find"],
    )
    registry.register(
        "list", _LIST_SCHEMA, impl=_list_impl,
        danger="safe", icon="folder", cn_name="列出文件",
        group=GROUP_READ, description="列出目录内容",
        aliases=["List", "LS", "Ls", "ListDir", "list_directory"],
    )
    registry.register(
        "glob", _GLOB_SCHEMA, impl=_glob_impl,
        danger="safe", icon="Search", cn_name="匹配",
        group=GROUP_READ, description="通配符查找文件",
        aliases=["Glob", "LS", "ListFiles", "list_files"],
    )
    registry.register(
        "scan_repo", _SCAN_REPO_SCHEMA, impl=_scan_repo_impl,
        danger="safe", icon="Search", cn_name="扫描仓库",
        group=GROUP_READ, description="扫描仓库生成摘要",
        aliases=["ScanRepo", "scan_repo"],
    )
    registry.register(
        "stage_files", _STAGE_FILES_SCHEMA, impl=_stage_files_impl,
        danger="dangerous", icon="Search", cn_name="标记文件",
        group=GROUP_READ, description="标记相关文件",
        aliases=["StageFiles", "stage_files"],
    )

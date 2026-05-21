# -*- coding: utf-8 -*-
"""
文件工具集 - 提供文件读写和编辑功能

支持：
- 读取：read（hashline 格式，每行标注 LINE:HASH|content）
- 写入：write, write_file
- 编辑：edit（基于 hashline LINE:HASH 锚点定位，替代旧版 str_replace）
- 目录：list, mkdir, delete_file
- 搜索：grep (异步), scan_repo (异步)
"""
import hashlib
import fnmatch
import re
from typing import Dict, List, Optional, Callable
from pathlib import Path
import os
from functools import lru_cache

from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool, QRunnable
from loguru import logger
from app.tools.result import ToolResult

MAX_GREP_CONTENT_LENGTH = 15000

# ========== 性能优化：模块级别常量和缓存 ==========
# Grep 排除目录（性能优化：预创建集合）
_GREP_EXCLUDE_DIRS = frozenset({
    '.drifox', '.mypy_cache', '.git', 'node_modules', '__pycache__',
    'venv', '.venv', 'dist', 'build', '.idea', '.vscode'
})

# ========== Hashline 核心函数 ==========

_HASHLINE_EMPTY_PLACEHOLDER = " "  # 占位符，空行哈希时使用
_HASHLINE_EMPTY_NUM_LINES = frozenset()  # 哨兵


def _line_hash(line: str, line_num: int = 0) -> str:
    """
    计算行的 hashline 2-字符哈希。
    
    哈希算法：
    1. 去除尾部空白（保持前导缩进，Python 缩进敏感）
    2. 如果是空行或纯空白行，混入行号以区分相同空行
    3. MD5 取前 2 个 hex 字符
    
    Args:
        line: 行内容
        line_num: 行号（从1开始），用于空行区分
    
    Returns:
        2 字符哈希值
    """
    normalized = line.rstrip('\n\r ')
    if not normalized:
        # 空行/纯空白行：混入行号确保不同位置的相同空行有不同哈希
        seed = f"\0line:{line_num}"
    else:
        seed = normalized
    return hashlib.md5(seed.encode("utf-8")).hexdigest()[:2]


def _parse_anchor(anchor: str) -> tuple:
    """
    解析 'LINE:HASH' 锚点为 (line_num, hash_value)
    
    Args:
        anchor: 格式如 "12:a3" 的锚点
        
    Returns:
        (line_number, hash_value)
        
    Raises:
        ValueError: 格式无效
    """
    parts = anchor.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid anchor format: {anchor}, expected LINE:HASH")
    return int(parts[0]), parts[1]


def _format_hashline(lines: List[str], start_line: int = 1) -> str:
    """
    将行列表格式化为 hashline 格式。
    
    Args:
        lines: 行列表（每行可能含 \n）
        start_line: 起始行号
        
    Returns:
        hashline 格式字符串：LINE:HASH|content
    """
    result = []
    for i, line in enumerate(lines, start=start_line):
        content = line.rstrip('\n')
        h = _line_hash(content, i)
        result.append(f"{i}:{h}|{content}")
    return "\n".join(result)


# ── Hashline: 邻近搜索 & remaps ─────────────────────────────────────

_PROXIMITY_WINDOW = 5  # 邻近搜索窗口大小（±N 行）


def _find_anchor_in_proximity(expected_hash: str, expected_line: int,
                              file_lines: List[str]) -> Optional[int]:
    """
    在目标行附近搜索匹配哈希的行。
    
    处理场景：前面插/删行导致锚点偏移。先在精确位置检查，
    如果匹配不上，在 ±_PROXIMITY_WINDOW 范围内搜索。
    
    Args:
        expected_hash: 期望的哈希值
        expected_line: 期望的行号（1-based）
        file_lines: 文件当前行列表
        
    Returns:
        匹配的行号（1-based），未找到返回 None
    """
    total = len(file_lines)
    lo = max(1, expected_line - _PROXIMITY_WINDOW)
    hi = min(total, expected_line + _PROXIMITY_WINDOW)
    
    for line_num in range(lo, hi + 1):
        content = file_lines[line_num - 1].rstrip('\n')
        h = _line_hash(content, line_num)
        if h == expected_hash:
            return line_num
    return None


def _find_anchor_remaps(stale_anchors: List[tuple],
                        file_lines: List[str]) -> Dict[str, str]:
    """
    全文件扫描，为失效率找当前正确锚点。
    
    扫描文件的每一行，匹配内容哈希。返回 stale→current 的映射字典。
    
    Args:
        stale_anchors: 失效锚点列表 [(line_num, hash), ...]
        file_lines: 文件当前行列表
        
    Returns:
        remaps 字典: {"12:a3": "14:a3"} 等
    """
    remaps: Dict[str, str] = {}
    for line_num, expected_hash in stale_anchors:
        # 扫描整个文件找匹配哈希
        found = False
        for i in range(len(file_lines)):
            content = file_lines[i].rstrip('\n')
            h = _line_hash(content, i + 1)
            if h == expected_hash:
                stale_key = f"{line_num}:{expected_hash}"
                current_key = f"{i + 1}:{h}"
                remaps[stale_key] = current_key
                found = True
                break
        if not found:
            stale_key = f"{line_num}:{expected_hash}"
            remaps[stale_key] = f"?{expected_hash}"  # 内容已不复存在
    return remaps


def _build_hash_error_message(errors: List[Dict], file_lines: List[str]) -> str:
    """
    构建带 remaps 和 >>> 标记的增强错误信息。
    
    格式类似 aron/hashline，让 LLM 能直接根据 remaps 修正锚点，
    无需重新读取文件。
    """
    n = len(errors)
    lines = []
    
    if n == 1:
        lines.append("1 anchor has changed since last read. Use remaps below.")
    else:
        lines.append(f"{n} anchors have changed since last read. Use remaps below.")
    lines.append("")
    
    # 收集需要显示的上下文行号
    show_lines = set()
    mismatches = {}
    for err in errors:
        ln = err["line"]
        show_lines.add(ln)
        mismatches[ln] = err
        for offset in range(1, 3):  # ±2 行上下文
            if ln - offset >= 1:
                show_lines.add(ln - offset)
            if ln + offset <= len(file_lines):
                show_lines.add(ln + offset)
    
    # 按行号排序输出
    sorted_lines = sorted(show_lines)
    prev = -1
    for ln in sorted_lines:
        if prev != -1 and ln > prev + 1:
            lines.append("    ...")
        prev = ln
        content = file_lines[ln - 1].rstrip('\n')
        h = _line_hash(content, ln)
        tag = f"{ln}:{h}"
        if ln in mismatches:
            lines.append(f">>> {tag}|{content}")
        else:
            lines.append(f"    {tag}|{content}")
    
    return "\n".join(lines)


@lru_cache(maxsize=128)
def _compile_grep_pattern(pattern: str) -> re.Pattern:
    """
    编译 grep 正则表达式（带缓存）

    性能优化：避免每次调用都重新编译相同的正则表达式
    """
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
        检查文件是否被外部修改（仅用于 write_file 全量写入）。
        
        edit_file 不使用此方法——hashline 的逐行哈希校验已是更精确的
        内容级修改检测，且能给出行级错误信息 + 正确锚点供自动修正。
        
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

    def read_file(self, path: str, offset: int = 1, limit: int = 500) -> ToolResult:
        """
        读取文件内容，返回 hashline 格式（每行标注 LINE:HASH|content）。
        
        Hashline 使用 2-字符内容哈希作为稳定锚点，编辑时通过 LINE:HASH 定位，
        无需精确匹配旧文本，避免因空白/缩进差异导致的编辑失败。
        
        Args:
            path: 文件路径
            offset: 起始行号（从1开始）
            limit: 最大读取行数

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
            hashline_content = _format_hashline(content_slice, start_idx + 1)

            res_info = f"File: {path} (Lines {start_idx + 1}-{end_idx} of {total_lines})\n\n"
            return ToolResult(True, content=res_info + hashline_content)
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

            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content if content is not None else "")

            # 更新修改时间记录
            self._file_mtimes[str(full_path)] = full_path.stat().st_mtime

            return ToolResult(True, content=f"Successfully written to {path}")
        except Exception as e:
            return ToolResult(False, error=f"Write error: {str(e)}")

    def edit_file(self, path: str, operations: List[Dict]) -> ToolResult:
        """
        通过 hashline LINE:HASH 锚点编辑文件。
        
        替代旧版 str_replace 编辑方式。每行通过 'LINE:HASH' 锚点定位，
        支持批量操作，编辑从文件底部向上执行以避免行号偏移。
        
        邻近搜索：如果锚点所在行哈希不匹配，会在 ±5 行范围内搜索匹配的
        内容哈希。这处理了"前面插行后锚点偏移"的常见场景。
        
        如果邻近搜索也找不到，会全文件扫描所有失效锚点的当前位置，
        返回 remaps 映射 + >>> 标记的差异上下文，LLM 可直接用 remaps
        修正锚点，无需重新读取文件。
        
        Args:
            path: 文件路径
            operations: 编辑操作列表，每个操作包含：
                - op: 操作类型
                    "replace"    替换单行或范围
                    "insert_after"  在锚点后插入
                    "insert_before" 在锚点前插入  
                    "delete"     删除单行或范围
                - anchor: "LINE:HASH" 锚点（起始行/定位行）
                - anchor_end: "LINE:HASH"（可选，范围操作的结束行）
                - lines: 新内容行列表（delete 操作不需要）
        
        示例：
            [{"op": "replace", "anchor": "12:a3", "lines": ["new content"]}]
            [{"op": "replace", "anchor": "12:a3", "anchor_end": "15:b7", "lines": ["a", "b"]}]
            [{"op": "insert_after", "anchor": "11:c2", "lines": ["inserted"]}]
            [{"op": "delete", "anchor": "12:a3"}]
        """
        try:
            full_path = self._resolve_path(path)
            if not full_path.exists():
                return ToolResult(False, error=f"File not found: {path}")

            with open(full_path, "r", encoding="utf-8") as f:
                all_lines = f.readlines()

            # ── 第一遍：校验所有锚点，含邻近搜索自动修正 ──
            resolved_ops = []     # 修正后的操作 [(anchor_line, end_line, op), ...]
            raw_errors = []       # {line, hash, anchor_str} 收集完全匹配失败的行
            validation_failed = False

            for op in operations:
                op_type = op.get("op", "replace")
                anchor_str = op.get("anchor", "")
                
                # 解析 anchor
                try:
                    orig_line, expected_hash = _parse_anchor(anchor_str)
                except (ValueError, IndexError):
                    return ToolResult(False, error=f"Invalid anchor format: {anchor_str}")

                # 1) 精确匹配
                actual_line = orig_line
                if 1 <= actual_line <= len(all_lines):
                    actual_content = all_lines[actual_line - 1].rstrip('\n')
                    actual_hash = _line_hash(actual_content, actual_line)
                    if actual_hash == expected_hash:
                        pass  # 精确匹配成功
                    else:
                        # 2) 邻近搜索（处理插行偏移）
                        nearby = _find_anchor_in_proximity(expected_hash, orig_line, all_lines)
                        if nearby is not None and nearby != orig_line:
                            actual_line = nearby
                            logger.info(
                                f"[Hashline] Proximity fix: anchor {anchor_str} "
                                f"→ line {nearby}:{expected_hash}"
                            )
                        else:
                            # 完全匹配失败
                            raw_errors.append({
                                "line": orig_line,
                                "hash": expected_hash,
                                "anchor": anchor_str,
                            })
                            validation_failed = True
                            continue
                else:
                    raw_errors.append({
                        "line": orig_line,
                        "hash": expected_hash,
                        "anchor": anchor_str,
                        "out_of_range": True,
                    })
                    validation_failed = True
                    continue

                # 解析 anchor_end（如果有）
                anchor_end_str = op.get("anchor_end")
                actual_end_line = actual_line  # 默认为单行
                if anchor_end_str:
                    try:
                        orig_end_line, end_hash = _parse_anchor(anchor_end_str)
                    except (ValueError, IndexError):
                        return ToolResult(False, error=f"Invalid anchor_end format: {anchor_end_str}")

                    # 同样尝试精确匹配 + 邻近搜索
                    actual_end_line = orig_end_line
                    if 1 <= actual_end_line <= len(all_lines):
                        end_content = all_lines[actual_end_line - 1].rstrip('\n')
                        end_actual_hash = _line_hash(end_content, actual_end_line)
                        if end_actual_hash == end_hash:
                            pass
                        else:
                            nearby_end = _find_anchor_in_proximity(
                                end_hash, orig_end_line, all_lines
                            )
                            if nearby_end is not None and nearby_end != orig_end_line:
                                actual_end_line = nearby_end
                            else:
                                raw_errors.append({
                                    "line": orig_end_line,
                                    "hash": end_hash,
                                    "anchor": anchor_end_str,
                                })
                                validation_failed = True
                                continue

                    if actual_end_line < actual_line:
                        return ToolResult(
                            False,
                            error=f"End line {actual_end_line} is before start line {actual_line}"
                        )

                resolved_ops.append((actual_line, actual_end_line, op))

            # ── 如果有校验失败，构建增强错误 ──
            if validation_failed:
                remaps = _find_anchor_remaps(
                    [(e["line"], e["hash"]) for e in raw_errors],
                    all_lines
                )
                # 构建错误消息
                msg_parts = []
                if remaps:
                    msg_parts.append(
                        "Hashline edit failed — anchors changed since last read.\n"
                        "Use the remaps below to update your anchors:\n"
                    )
                    for stale, current in remaps.items():
                        msg_parts.append(f"  {stale} → {current}")
                    msg_parts.append("")
                
                msg_parts.append(_build_hash_error_message(raw_errors, all_lines))
                
                return ToolResult(False, error="\n".join(msg_parts))

            # ── 第二遍：应用操作（从底部向上） ──
            sorted_ops = sorted(
                resolved_ops,
                key=lambda x: (-x[1], -x[0])  # 先按 end_line 降序，再按 start_line 降序
            )

            new_lines = list(all_lines)
            applied_count = 0

            for actual_line, actual_end_line, op in sorted_ops:
                op_type = op.get("op", "replace")
                anchor_end = op.get("anchor_end")

                if op_type == "replace":
                    if anchor_end:
                        # 替换范围
                        insert_lines = [
                            l + ("\n" if not l.endswith("\n") else "")
                            for l in op.get("lines", [])
                        ]
                        new_lines[actual_line - 1:actual_end_line] = insert_lines
                    else:
                        # 替换单行（可扩展为多行）
                        insert_lines = op.get("lines", [])
                        if insert_lines:
                            first = insert_lines[0]
                            new_lines[actual_line - 1] = first + (
                                "\n" if not first.endswith("\n") else ""
                            )
                            if len(insert_lines) > 1:
                                extra = [
                                    l + ("\n" if not l.endswith("\n") else "")
                                    for l in insert_lines[1:]
                                ]
                                new_lines[actual_line:actual_line] = extra
                        else:
                            del new_lines[actual_line - 1]

                elif op_type == "delete":
                    if anchor_end:
                        del new_lines[actual_line - 1:actual_end_line]
                    else:
                        del new_lines[actual_line - 1]

                elif op_type == "insert_after":
                    insert_lines = [
                        l + ("\n" if not l.endswith("\n") else "")
                        for l in op.get("lines", [])
                    ]
                    new_lines[actual_line:actual_line] = insert_lines

                elif op_type == "insert_before":
                    insert_lines = [
                        l + ("\n" if not l.endswith("\n") else "")
                        for l in op.get("lines", [])
                    ]
                    new_lines[actual_line - 1:actual_line - 1] = insert_lines

                else:
                    return ToolResult(False, error=f"Unknown operation type: {op_type}")

                applied_count += 1

            # 写出文件
            full_path.write_text("".join(new_lines), encoding="utf-8")
            self._file_mtimes[str(full_path)] = full_path.stat().st_mtime

            return ToolResult(
                True,
                content=f"Applied {applied_count} hashline edit(s) to {path}."
            )
        except Exception as e:
            import traceback
            return ToolResult(False, error=f"Hashline edit error: {str(e)}\n{traceback.format_exc()}")

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

    # multi_edit 和 apply_patch 已移除，已被 hashline edit 替代

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

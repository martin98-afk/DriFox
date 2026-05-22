# -*- coding: utf-8 -*-
"""
文件工具集 - 提供文件读写和编辑功能

支持：
- 读取：read（hashline 格式，每行标注 LINE+HASH|content）
- 写入：write, write_file
- 编辑：edit（基于 hashline LINE+HASH 锚点定位，替代旧版 str_replace）
- 目录：list, mkdir, delete_file
- 搜索：grep (异步), scan_repo (异步）
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
import difflib
from app.tools.result import ToolResult

MAX_GREP_CONTENT_LENGTH = 15000

# ========== 性能优化：模块级别常量和缓存 ==========
_GREP_EXCLUDE_DIRS = frozenset({
    '.drifox', '.mypy_cache', '.git', 'node_modules', '__pycache__',
    'venv', '.venv', 'dist', 'build', '.idea', '.vscode'
})

# ========== BPE Bigram Hash 空间（来自 oh-my-pi） ==========
_HL_BIGRAMS = (
    "aa", "ab", "ac", "ad", "ae", "af", "ag", "ah", "ai", "aj", "ak", "al", "am",
    "an", "ao", "ap", "aq", "ar", "as", "at", "au", "av", "aw", "ax", "ay", "az",
    "ba", "bb", "bc", "bd", "be", "bf", "bg", "bh", "bi", "bj", "bk", "bl", "bm",
    "bn", "bo", "bp", "br", "bs", "bt", "bu", "bv", "bw", "bx", "by", "bz",
    "ca", "cb", "cc", "cd", "ce", "cf", "cg", "ch", "ci", "cj", "ck", "cl", "cm",
    "cn", "co", "cp", "cq", "cr", "cs", "ct", "cu", "cv", "cw", "cx", "cy", "cz",
    "da", "db", "dc", "dd", "de", "df", "dg", "dh", "di", "dj", "dk", "dl", "dm",
    "dn", "do", "dp", "dq", "dr", "ds", "dt", "du", "dv", "dw", "dx", "dy", "dz",
    "ea", "eb", "ec", "ed", "ee", "ef", "eg", "eh", "ei", "ej", "ek", "el", "em",
    "en", "eo", "ep", "eq", "er", "es", "et", "eu", "ev", "ew", "ex", "ey", "ez",
    "fa", "fb", "fc", "fd", "fe", "ff", "fg", "fh", "fi", "fj", "fk", "fl", "fm",
    "fn", "fo", "fp", "fq", "fr", "fs", "ft", "fu", "fv", "fw", "fx", "fy", "fz",
    "ga", "gb", "gc", "gd", "ge", "gf", "gg", "gh", "gi", "gj", "gl", "gm", "gn",
    "go", "gp", "gr", "gs", "gt", "gu", "gv", "gw", "gx", "gy", "gz",
    "ha", "hb", "hc", "hd", "he", "hf", "hg", "hh", "hi", "hj", "hk", "hl", "hm",
    "hn", "ho", "hp", "hq", "hr", "hs", "ht", "hu", "hv", "hw", "hx", "hy", "hz",
    "ia", "ib", "ic", "id", "ie", "if", "ig", "ih", "ii", "ij", "ik", "il", "im",
    "in", "io", "ip", "iq", "ir", "is", "it", "iu", "iv", "iw", "ix", "iy", "iz",
    "ja", "jb", "jc", "jd", "je", "jf", "jg", "jh", "ji", "jj", "jk", "jl", "jm",
    "jn", "jo", "jp", "jq", "jr", "js", "jt", "ju", "jw", "jx", "jy",
    "ka", "kb", "kc", "kd", "ke", "kf", "kg", "kh", "ki", "kj", "kk", "kl", "km",
    "kn", "ko", "kp", "kr", "ks", "kt", "ku", "kv", "kw", "kx", "ky",
    "la", "lb", "lc", "ld", "le", "lf", "lg", "lh", "li", "lj", "lk", "ll", "lm",
    "ln", "lo", "lp", "lr", "ls", "lt", "lu", "lv", "lw", "lx", "ly", "lz",
    "ma", "mb", "mc", "md", "me", "mf", "mg", "mh", "mi", "mj", "mk", "ml", "mm",
    "mn", "mo", "mp", "mq", "mr", "ms", "mt", "mu", "mv", "mw", "mx", "my", "mz",
    "na", "nb", "nc", "nd", "ne", "nf", "ng", "nh", "ni", "nj", "nk", "nl", "nm",
    "nn", "no", "np", "nr", "ns", "nt", "nu", "nv", "nw", "nx", "ny", "nz",
    "oa", "ob", "oc", "od", "oe", "of", "og", "oh", "oi", "oj", "ok", "ol", "om",
    "on", "oo", "op", "oq", "or", "os", "ot", "ou", "ov", "ow", "ox", "oy", "oz",
    "pa", "pb", "pc", "pd", "pe", "pf", "pg", "ph", "pi", "pj", "pk", "pl", "pm",
    "pn", "po", "pp", "pq", "pr", "ps", "pt", "pu", "pv", "pw", "px", "py", "pz",
    "qa", "qb", "qc", "qd", "qe", "qh", "qi", "ql", "qm", "qn", "qo", "qp", "qq",
    "qr", "qs", "qt", "qu", "qw", "qx", "qy",
    "ra", "rb", "rc", "rd", "re", "rf", "rg", "rh", "ri", "rk", "rl", "rm", "rn",
    "ro", "rp", "rq", "rr", "rs", "rt", "ru", "rv", "rw", "rx", "ry", "rz",
    "sa", "sb", "sc", "sd", "se", "sf", "sg", "sh", "si", "sj", "sk", "sl", "sm",
    "sn", "so", "sp", "sq", "sr", "ss", "st", "su", "sv", "sw", "sx", "sy", "sz",
    "ta", "tb", "tc", "td", "te", "tf", "tg", "th", "ti", "tj", "tk", "tl", "tm",
    "tn", "to", "tp", "tr", "ts", "tt", "tu", "tv", "tw", "tx", "ty", "tz",
    "ua", "ub", "uc", "ud", "ue", "uf", "ug", "uh", "ui", "uj", "uk", "ul", "um",
    "un", "uo", "up", "uq", "ur", "us", "ut", "uu", "uv", "uw", "ux", "uy", "uz",
    "va", "vb", "vc", "vd", "ve", "vf", "vg", "vh", "vi", "vj", "vk", "vl", "vm",
    "vn", "vo", "vp", "vq", "vr", "vs", "vt", "vu", "vv", "vw", "vx", "vy", "vz",
    "wa", "wb", "wc", "wd", "we", "wf", "wg", "wh", "wi", "wj", "wk", "wl", "wm",
    "wn", "wo", "wp", "wr", "ws", "wt", "wu", "wv", "ww", "wx", "wy",
    "xa", "xb", "xc", "xd", "xe", "xf", "xh", "xi", "xl", "xm", "xn", "xo", "xp",
    "xr", "xs", "xt", "xu", "xx", "xy", "xz",
    "ya", "yb", "yc", "yd", "ye", "yf", "yg", "yh", "yi", "yj", "yk", "yl", "ym",
    "yn", "yo", "yp", "yr", "ys", "yt", "yu", "yv", "yw", "yx", "yy", "yz",
    "za", "zb", "zc", "zd", "ze", "zf", "zg", "zh", "zi", "zk", "zl", "zm", "zn",
    "zo", "zp", "zr", "zs", "zt", "zu", "zw", "zx", "zy", "zz",
)
_HL_BIGRAMS_COUNT = 647


# ========== Hashline 核心函数 ==========

def _line_hash(line: str, line_num: int = 0) -> str:
    normalized = line.rstrip('\n\r ')
    digest = hashlib.md5(normalized.encode("utf-8")).hexdigest()
    idx = int(digest[:8], 16) % _HL_BIGRAMS_COUNT
    return _HL_BIGRAMS[idx]


_HL_ANCHOR_RE = re.compile(r'^(\d+)([a-z]{2})$')


def _parse_anchor(anchor: str) -> tuple:
    m = _HL_ANCHOR_RE.match(anchor.strip())
    if not m:
        raise ValueError(
            f"Invalid anchor format: {anchor}, expected LINE+HASH "
            f"(e.g. '12sr', '160ab', '3th')"
        )
    return int(m.group(1)), m.group(2)


def _format_hashline(lines: List[str], start_line: int = 1) -> str:
    result = []
    for i, line in enumerate(lines, start=start_line):
        content = line.rstrip('\n')
        h = _line_hash(content)
        result.append(f"{i}{h}|{content}")
    return "\n".join(result)


# hashline 前缀正则：LINE+HASH|content 中的前缀部分
_HL_PREFIX_RE = re.compile(r'^\s*(?:>>>|>>)?\s*(?:[*+]\s*)?\d*[a-z]{2}[:|]')
_HL_TRUNCATION_NOTICE_RE = re.compile(
    r'^\[(?:Showing lines \d+-\d+ of \d+|\d+ more lines? in '
    r'(?:file|\S+))\b.*\bUse :L?\d+'
)


def _strip_hashline_prefixes(line: str) -> str:
    """递归清理行首 hashline 前缀（处理嵌套污染）。"""
    result = line
    while True:
        prev = result
        result = _HL_PREFIX_RE.sub('', result)
        if result == prev:
            break
    return result


def _clean_hashline_from_lines(lines: List[str]) -> List[str]:
    """
    逐行清理 hashline 前缀。
    对每行独立判断：仅当行首匹配前缀模式时剥离，不匹配的行原样保留。
    同时过滤截断提示行（defense-in-depth）。
    """
    if not lines:
        return lines

    result = []
    for l in lines:
        if _HL_TRUNCATION_NOTICE_RE.search(l):
            continue
        result.append(_strip_hashline_prefixes(l))
    return result


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
        内容级修改检测。

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
        读取文件内容，返回 hashline 格式（每行标注 LINE+HASH|content）。

        Hashline 使用 2-字符 BPE bigram 内容哈希作为稳定锚点，
        编辑时通过 LINE+HASH 定位，无需精确匹配旧文本。

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

    def _build_hashline_rejection(self, mismatches: List[Dict],
                                  file_lines: List[str], path: str) -> ToolResult:
        """
        构建 hashline 硬拒绝错误信息。
        显示每个失败锚点的详细信息：expected vs actual hash，以及上下文。
        """
        lines = []
        noun = "anchor does" if len(mismatches) == 1 else "anchors do"
        lines.append(
            f"Edit rejected: {len(mismatches)} {noun} not match the current file."
        )
        lines.append(
            "The edit was NOT applied. Please use the updated file content below, "
            "and issue another edit tool-call with correct anchors."
        )
        lines.append("")

        # 显示每个失败锚点的详细信息
        for m in mismatches:
            ln = m["line"]
            expected = m.get("expected", "?")
            actual = m.get("actual", "?")
            op_idx = m.get("op_idx")
            idx_info = f" (operation index {op_idx})" if op_idx is not None else ""
            lines.append(f"  Line {ln}{idx_info}: expected hash '{expected}', actual hash '{actual}'")
        lines.append("")

        # 标出不匹配行 ±2 行上下文
        mismatch_lines = {m["line"] for m in mismatches}
        show_lines = set()
        for m in mismatches:
            show_lines.add(m["line"])
            for offset in range(1, 3):
                if m["line"] - offset >= 1:
                    show_lines.add(m["line"] - offset)
                if m["line"] + offset <= len(file_lines):
                    show_lines.add(m["line"] + offset)

        prev = -1
        for ln in sorted(show_lines):
            if prev != -1 and ln > prev + 1:
                lines.append("...")
            prev = ln
            content = file_lines[ln - 1].rstrip('\n')
            h = _line_hash(content, ln)
            marker = "**" if ln in mismatch_lines else "  "
            lines.append(f"{marker}{ln}{h}|{content}")

        return ToolResult(False, error="\n".join(lines))

    def edit_file(self, path: str, operations: List[Dict]) -> ToolResult:
        """
        通过 hashline LINE+HASH 锚点编辑文件。

        替代旧版 str_replace 编辑方式。每行通过 'LINE+HASH' 锚点定位，
        支持批量操作，编辑从文件底部向上执行以避免行号偏移。

        硬拒绝策略：锚点哈希不匹配时，直接拒绝编辑并返回当前文件锚点块，
        让 LLM 重新读取文件。不做邻近搜索，不做 remaps 自动修正。

        Args:
            path: 文件路径
            operations: 编辑操作列表，每个操作包含：
                - op: 操作类型
                    "replace"    替换单行或范围
                    "insert_after"  在锚点后插入
                    "insert_before" 在锚点前插入
                    "delete"     删除单行或范围
                - anchor: "LINE+HASH" 锚点（起始行/定位行）
                - anchor_end: "LINE+HASH"（可选，范围操作的结束行）
                - lines: 新内容行列表（delete 操作不需要）

        示例：
            [{"op": "replace", "anchor": "12sr", "lines": ["new content"]}]
            [{"op": "replace", "anchor": "12sr", "anchor_end": "15th", "lines": ["a", "b"]}]
            [{"op": "insert_after", "anchor": "11ab", "lines": ["inserted"]}]
            [{"op": "delete", "anchor": "12sr"}]
        """
        try:
            full_path = self._resolve_path(path)
            if not full_path.exists():
                return ToolResult(False, error=f"File not found: {path}")

            with open(full_path, "r", encoding="utf-8") as f:
                all_lines = f.readlines()

            _VALID_OP_TYPES = frozenset({"replace", "insert_after", "insert_before", "delete"})

            # ── 第一遍：校验所有锚点，收集所有错误而非逐个早退 ──
            resolved_ops = []  # [(anchor_line, end_line, op, orig_idx), ...]
            mismatches = []     # 收集所有哈希不匹配的锚点

            for idx, op in enumerate(operations):
                if not isinstance(op, dict):
                    return ToolResult(
                        False,
                        error=(
                            f"Edit rejected: operation {idx} is not a valid object "
                            f"(got {type(op).__name__}). Each operation must be an object "
                            f"with 'anchor' and 'op' fields."
                        )
                    )

                op_type = op.get("op", "replace")
                if op_type not in _VALID_OP_TYPES:
                    return ToolResult(
                        False,
                        error=(
                            f"Edit rejected: unknown operation type '{op_type}' "
                            f"at index {idx}. Valid types: {', '.join(sorted(_VALID_OP_TYPES))}."
                        )
                    )

                anchor_str = op.get("anchor", "")

                # 解析 anchor
                try:
                    orig_line, expected_hash = _parse_anchor(anchor_str)
                except ValueError as e:
                    return ToolResult(False, error=str(e))

                # 精确校验：行号必须在范围内
                if not (1 <= orig_line <= len(all_lines)):
                    return ToolResult(
                        False,
                        error=(
                            f"Edit rejected: line {orig_line} does not exist "
                            f"(file has {len(all_lines)} lines). "
                            f"Please re-read the file with `read` to get current anchors."
                        )
                    )

                # 精确校验：哈希必须完全匹配
                actual_content = all_lines[orig_line - 1].rstrip('\n')
                actual_hash = _line_hash(actual_content, orig_line)

                if actual_hash != expected_hash:
                    mismatches.append({"line": orig_line, "expected": expected_hash, "actual": actual_hash, "op_idx": idx})
                    continue  # 跳过此操作，继续校验后续操作

                actual_line = orig_line

                # 解析 anchor_end（如果有）
                anchor_end_str = op.get("anchor_end")
                actual_end_line = actual_line
                if anchor_end_str:
                    try:
                        orig_end_line, end_hash = _parse_anchor(anchor_end_str)
                    except ValueError as e:
                        return ToolResult(False, error=str(e))

                    if not (1 <= orig_end_line <= len(all_lines)):
                        return ToolResult(
                            False,
                            error=(
                                f"Edit rejected: end line {orig_end_line} does not exist "
                                f"(file has {len(all_lines)} lines). "
                                f"Please re-read the file with `read` to get current anchors."
                            )
                        )

                    end_content = all_lines[orig_end_line - 1].rstrip('\n')
                    end_actual_hash = _line_hash(end_content, orig_end_line)

                    if end_actual_hash != end_hash:
                        mismatches.append({"line": orig_end_line, "expected": end_hash, "actual": end_actual_hash, "op_idx": idx})
                        continue  # 跳过此操作，继续校验后续操作

                    actual_end_line = orig_end_line

                    if actual_end_line < actual_line:
                        return ToolResult(
                            False,
                            error=f"End line {actual_end_line} is before start line {actual_line}"
                        )

                resolved_ops.append((actual_line, actual_end_line, op, idx))

            # 如果有任何锚点不匹配，一次性报告所有错误
            if mismatches:
                return self._build_hashline_rejection(mismatches, all_lines, path)

            # ── 清洗 operations 中 lines 的 hashline 前缀 ──
            for _, _, op, _ in resolved_ops:
                if "lines" in op and op["lines"]:
                    op["lines"] = _clean_hashline_from_lines(op["lines"])

            # ── 第二遍：应用操作（从底部向上，同锚点按原顺序倒序处理） ──
            sorted_ops = sorted(
                resolved_ops,
                key=lambda x: (-x[1], -x[0], -x[3])
            )

            new_lines = list(all_lines)
            applied_count = 0

            for actual_line, actual_end_line, op, _ in sorted_ops:
                op_type = op.get("op", "replace")
                anchor_end = op.get("anchor_end")

                if op_type == "replace":
                    if anchor_end:
                        insert_lines = [
                            l + ("\n" if not l.endswith("\n") else "")
                            for l in op.get("lines", [])
                        ]
                        new_lines[actual_line - 1:actual_end_line] = insert_lines
                    else:
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

            # ── 计算 diff + 锚点块 ──
            old_text = "".join(all_lines)
            new_text = "".join(new_lines)

            # 生成 unified diff
            diff_lines = list(difflib.unified_diff(
                old_text.splitlines(),
                new_text.splitlines(),
                fromfile=path, tofile=path,
                lineterm=''
            ))
            diff_str = "\n".join(diff_lines) if diff_lines else ""

            # 从 diff header 中提取首末变更行
            first_changed = None
            last_changed = None
            _HUNK_RE = re.compile(r'@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')
            for line in diff_lines:
                m = _HUNK_RE.match(line)
                if m:
                    start = int(m.group(1))
                    count = int(m.group(2)) if m.group(2) else 1
                    end_line = start + count - 1
                    if first_changed is None or start < first_changed:
                        first_changed = start
                    if last_changed is None or end_line > last_changed:
                        last_changed = end_line

            # 构建锚点块（±2 行上下文）
            anchors = None
            if first_changed is not None and last_changed is not None:
                anchor_start = max(1, first_changed - 2)
                anchor_end_num = min(len(new_lines), last_changed + 2)
                anchor_lines = new_lines[anchor_start - 1:anchor_end_num]
                anchors = _format_hashline(anchor_lines, anchor_start)

            # 构建结果文本
            result_parts = [f"Applied {applied_count} hashline edit(s) to {path}."]
            if anchors:
                result_parts.append("")
                result_parts.append(f"--- Anchors {anchor_start}-{anchor_end_num} ---")
                result_parts.append(anchors)

            return ToolResult(
                True,
                content="\n".join(result_parts),
                diff=diff_str,
                anchors=anchors,
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

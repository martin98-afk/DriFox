# -*- coding: utf-8 -*-
"""同步内容指纹与规范化打包工具。

云同步的「是否真的变了」不能靠 mtime，也不能靠 ZIP 字节：同一份内容每次
打包，ZIP 里的 mtime/顺序都不同，字节必然不同。本模块提供两级判据：

1. ``dir_fingerprint`` / ``file_fingerprint``：纯本地清单摘要，用于「内容没变
   就不要打包」的快速短路（watchfiles 会因 touch、编辑器临时文件误报变更）。
2. ``git_blob_sha``：与 Gitee / GitHub 返回的 ``sha`` 同一算法。配合
   ``build_stable_zip`` 的确定性打包，可在零网络请求的情况下判断「远端已经是
   这份内容」，从而彻底消除无变更上传。

``build_stable_zip`` 保证「同内容 → 同字节」：固定 mtime、固定条目顺序、
固定压缩参数与权限位，并排除 ``__pycache__`` 等运行时垃圾。
"""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import zipfile
from pathlib import Path
from typing import Dict, Iterator, Optional

# 打包/指纹一律排除的目录名（运行时产物，跨设备同步无意义且会互相污染）
EXCLUDE_DIR_NAMES = frozenset(
    {
        "__pycache__",
        ".git",
        ".svn",
        ".hg",
        "node_modules",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".idea",
        ".vscode",
    }
)

# 排除的文件名（系统垃圾文件）
EXCLUDE_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})

# 排除的后缀（编译产物/临时文件/日志）
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".tmp", ".temp", ".swp", ".log", ".bak")

# 确定性 ZIP 的固定时间戳（ZIP 纪元下限）
_FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)

# 旧版冲突副本命名（<原名>.remote-<设备>-<时间戳>）。历史版本把它写在业务目录里，
# 这里继续排除，避免残留副本被当成正常内容参与同步。
_LEGACY_CONFLICT_RE = re.compile(r"\.remote-.+?-\d{8}-\d{6}")


def _is_excluded(rel: Path) -> bool:
    """按相对路径判断是否排除（任一父目录命中排除名单即排除）"""
    if rel.name in EXCLUDE_FILE_NAMES:
        return True
    if rel.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    if _LEGACY_CONFLICT_RE.search(rel.name):
        return True
    return any(part in EXCLUDE_DIR_NAMES for part in rel.parts[:-1])


def iter_files(root: Path) -> Iterator[Path]:
    """按相对路径字典序遍历目录下的有效文件（排除运行时垃圾）"""
    if not root.exists() or not root.is_dir():
        return
    items = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if _is_excluded(rel):
            continue
        items.append((rel.as_posix(), p))
    items.sort(key=lambda it: it[0])
    for _, p in items:
        yield p


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_fingerprint(path: Path) -> str:
    """单文件内容指纹（文件不存在返回固定空标记）"""
    if not path.exists() or not path.is_file():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def dir_manifest(root: Path) -> Dict[str, str]:
    """目录清单：{相对路径: 文件内容 sha256}（已按路径排序语义）"""
    manifest: Dict[str, str] = {}
    base = Path(root)
    for p in iter_files(base):
        rel = p.relative_to(base).as_posix()
        try:
            manifest[rel] = file_fingerprint(p)
        except OSError:
            # 读取失败（被占用/权限）→ 记为不稳定标记，避免误判"没变化"
            manifest[rel] = "unreadable"
    return manifest


def manifest_digest(manifest: Dict[str, str]) -> str:
    """清单摘要：{路径: 内容 sha} 的确定性哈希"""
    h = hashlib.sha256()
    for rel in sorted(manifest):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(str(manifest[rel]).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def dir_fingerprint(root: Path) -> str:
    """目录内容指纹（空目录/不存在返回固定值）"""
    return manifest_digest(dir_manifest(root))


def git_blob_sha(data: bytes) -> str:
    """git blob sha1（与 Gitee/GitHub contents API 的 sha 字段同算法）"""
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def build_stable_zip(root: Path, arc_prefix: str = "", extra_dirs: Optional[Dict[str, Path]] = None) -> bytes:
    """把目录打成确定性 ZIP，返回字节内容。

    Args:
        root: 目录根
        arc_prefix: ZIP 内顶层目录名（空则条目直接位于根）
        extra_dirs: 额外附带的外部目录 {arcname: path}（不参与排除规则之外的特殊处理）

    确定性保证：条目按 arcname 排序、固定 date_time、固定权限位与压缩级别。
    因此同一份内容每次产出的字节完全相同，可直接用 git_blob_sha 与远端 sha 比对。
    """
    entries: list[tuple[str, Path]] = []
    base = Path(root)
    if base.exists() and base.is_dir():
        for p in iter_files(base):
            rel = p.relative_to(base).as_posix()
            arcname = f"{arc_prefix}/{rel}" if arc_prefix else rel
            entries.append((arcname, p))
    if extra_dirs:
        for arcname, path in extra_dirs.items():
            entries.append((arcname, Path(path)))
    entries.sort(key=lambda it: it[0])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for arcname, path in entries:
            try:
                data = path.read_bytes()
            except OSError:
                continue
            info = zipfile.ZipInfo(filename=arcname, date_time=_FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # 固定为 Unix，避免跨平台字节差异
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    return buf.getvalue()


# ── 本地快照 ──────────────────────────────────────────────


def snapshot_tree(src: Path, dst: Path) -> bool:
    """把目录完整复制到 dst（覆盖式）。目录不存在时返回 False。"""
    if not src.exists() or not src.is_dir():
        return False
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.rmtree(str(dst), ignore_errors=True)
        shutil.copytree(str(src), str(dst))
        return True
    except Exception:
        return False


def prune_snapshot_dirs(root: Path, keep: int = 5) -> None:
    """只保留 root 下最新的 keep 个快照目录（按名称排序，名称为时间戳）"""
    if not root.exists() or not root.is_dir():
        return
    dirs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    for old in dirs[: max(0, len(dirs) - keep)]:
        shutil.rmtree(str(old), ignore_errors=True)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描 PyInstaller onedir 输出目录，生成文件清单 manifest.json。

用法:
    python scripts/generate_manifest.py dist/Drifox v0.3.9 [--output manifest.json]

CI 集成:
    python scripts/generate_manifest.py dist/Drifox $(git describe --tags --abbrev=0)

输出 manifest.json 结构:
    {
      "version": "v0.3.9",
      "build_time": "2026-07-14T16:00:00Z",
      "total_size": 142000000,
      "file_count": 2847,
      "files": {
        "Drifox.exe":       {"sha256": "abc123...", "size": 5242880},
        "_internal/python.exe": {"sha256": "def456...", "size": 1048576},
        ...
      }
    }
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 跳过的目录名
SKIP_DIRS = {"__pycache__", ".mypy_cache", ".git", ".svn", ".hg"}

# 大文件流式读取的缓冲区大小
BUF_SIZE = 64 * 1024  # 64KB


def sha256_file(filepath: str) -> str:
    """流式计算文件 SHA256，不将整个文件加载到内存。"""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(BUF_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def scan_directory(root: str) -> dict[str, dict]:
    """递归扫描目录，返回 {相对路径: {sha256, size}} 字典。

    路径统一使用正斜杠 /，确保跨平台一致。
    按路径排序返回，确保输出确定性。
    """
    root_path = Path(root).resolve()
    files = {}

    for dirpath, dirnames, filenames in os.walk(root_path):
        # 跳过不需要的目录（原地修改 dirnames 以阻止 os.walk 进入）
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        for filename in filenames:
            full_path = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(full_path, root_path)
            # 统一使用正斜杠
            rel_path = rel_path.replace("\\", "/")

            try:
                size = os.path.getsize(full_path)
                file_hash = sha256_file(full_path)
                files[rel_path] = {"sha256": file_hash, "size": size}
            except (OSError, PermissionError) as e:
                print(f"[WARN] 跳过无法读取的文件: {rel_path} ({e})", file=sys.stderr)
                continue

    # 按路径排序
    return dict(sorted(files.items()))


def generate_manifest(onedir: str, version: str) -> dict:
    """生成完整的 manifest 字典。"""
    files = scan_directory(onedir)

    total_size = sum(f["size"] for f in files.values())
    file_count = len(files)

    manifest = {
        "version": version,
        "build_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_size": total_size,
        "file_count": file_count,
        "files": files,
    }
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="扫描 PyInstaller onedir 输出，生成 manifest.json"
    )
    parser.add_argument(
        "onedir",
        help="onedir 输出目录路径，如 dist/Drifox",
    )
    parser.add_argument(
        "version",
        help="版本号，如 v0.3.9",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="输出文件路径（默认输出到 onedir 同级目录的 <name>_<version>_manifest.json）",
    )

    args = parser.parse_args()

    onedir_path = Path(args.onedir)
    if not onedir_path.is_dir():
        print(f"[ERROR] 目录不存在: {onedir_path}", file=sys.stderr)
        sys.exit(1)

    # 确定输出路径
    if args.output:
        output_path = Path(args.output)
    else:
        name = onedir_path.name  # e.g. "Drifox"
        parent = onedir_path.parent  # e.g. "dist"
        output_path = parent / f"{name}_{args.version}_manifest.json"

    print(f"[INFO] 扫描目录: {onedir_path}")
    manifest = generate_manifest(str(onedir_path), args.version)

    print(f"[INFO] 文件数: {manifest['file_count']}")
    print(f"[INFO] 总大小: {manifest['total_size']:,} 字节 ({manifest['total_size'] / 1024 / 1024:.1f} MB)")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"[INFO] 已输出: {output_path}")


if __name__ == "__main__":
    main()

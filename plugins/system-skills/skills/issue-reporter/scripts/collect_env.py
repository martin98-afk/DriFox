# -*- coding: utf-8 -*-
"""collect_env.py — 收集 DriFox 环境信息（issue 报告用）

输出 JSON：version / os / python / qt / recent_errors。
- recent_errors 取最新日志文件尾部的 ERROR/WARNING 行（最多 20 条），已做密钥掩码。
- 单项失败不阻塞：对应字段填错误说明，整体退出码恒为 0。

用法：
    py -3 collect_env.py [--project D:/work/DriFox] [--log-dir logs] [--tail 500]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
from pathlib import Path

# 密钥掩码：sk- 开头串 / 常见 token、key、secret 赋值形式
_SENSITIVE_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password|authorization)\s*[=:]\s*)\S+"),
]


_SKILL_SCRIPTS_DIR = Path(__file__).resolve().parent  # .../skills/issue-reporter/scripts


def _mask(text: str) -> str:
    for pat in _SENSITIVE_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) or "") + "***", text)
    return text


def _app_version(project: Path) -> str:
    """版本号：源码仓读 pyproject.toml；打包版从锚定目录的 app 源码挖 current_version 常量

    打包目录无 pyproject.toml（build.py 未打包），版本常量在 app/utils/config.py；
    脚本由外置 python 运行（sys.frozen 不可靠），改用脚本自身路径上渊 4 级锚定根目录：
    源码仓 → DriFox 根；打包版 datas 将 plugins 整目录拷入 _internal → _internal。
    """
    app_root = _SKILL_SCRIPTS_DIR.parents[3]  # scripts → issue-reporter → skills → system-skills → 根
    candidates = [
        app_root / "pyproject.toml",
        project / "pyproject.toml",
    ]
    config_py = [
        app_root / "app" / "utils" / "config.py",
        project / "app" / "utils" / "config.py",
    ]
    try:
        import tomllib

        for pyproject in candidates:
            try:
                with open(pyproject, "rb") as f:
                    return str(tomllib.load(f)["project"]["version"])
            except Exception:
                continue
    except Exception:
        pass
    for path in config_py:
        try:
            m = re.search(r'current_version\s*=\s*"(v?[\d.]+)"', path.read_text(encoding="utf-8", errors="ignore"))
            if m:
                return m.group(1)
        except Exception:
            continue
    return "unknown"


def _qt_version() -> str:
    try:
        from PyQt5.QtCore import qVersion

        return qVersion()
    except Exception:
        return "unavailable"


def _recent_errors(log_dir: Path, tail: int, limit: int) -> list[str]:
    """最新日志文件尾部的 ERROR/WARNING 行（新→旧排序，掩码后返回）"""
    if not log_dir.is_dir():
        return [f"(日志目录不存在: {log_dir})"]
    log_files = sorted(
        (p for p in log_dir.iterdir() if p.suffix.lower() in (".log", ".txt")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not log_files:
        return ["(无日志文件)"]

    hits: list[str] = []
    for log_file in log_files:
        try:
            lines = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()[-tail:]
        except Exception as e:
            hits.append(f"(读取 {log_file.name} 失败: {e})")
            continue
        for line in reversed(lines):  # 新→旧
            if "| ERROR" in line or "| WARNING" in line:
                hits.append(_mask(line.strip())[:300])
                if len(hits) >= limit:
                    return hits
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description="收集 DriFox 环境信息（issue 报告用）")
    parser.add_argument("--project", default=os.getcwd(), help="项目根目录（默认当前目录）")
    parser.add_argument("--log-dir", default=None, help="日志目录（默认 <project>/logs）")
    parser.add_argument("--tail", type=int, default=500, help="每个日志文件读取的尾部行数")
    parser.add_argument("--limit", type=int, default=20, help="recent_errors 最多条数")
    args = parser.parse_args()

    project = Path(args.project)
    log_dir = Path(args.log_dir) if args.log_dir else project / "logs"

    info: dict[str, object] = {
        "version": _app_version(project),
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "python": sys.version.split()[0],
        "qt": _qt_version(),
        "recent_errors": _recent_errors(log_dir, args.tail, args.limit),
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

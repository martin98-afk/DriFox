# -*- coding: utf-8 -*-
"""能力池审计差集报告：实际使用 vs manifest 声明。

数据源：
- 审计日志：[CapabilityAudit] plugin=X cap=Y level=Z（每插件×surface×能力只记一次）
- 插件 manifest：plugins 目录下各 plugin.json 的 capabilities 声明

输出三类差集：
- 兼容期插件（未声明 capabilities）→ 建议声明清单（实际使用 - 恒放行项）
- 已声明插件 → 声明未用（可从清单删）
- 已声明插件用到了声明外能力 → 标记异常（理论上不可达，出现说明有绕过路径）

用途：给插件作者一份可直接照抄的声明清单，推动兼容期收敛（api_version=2）。
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# 与 CapabilityRegistry.UNGATED 一致：恒放行，不算「使用」
UNGATED = frozenset({"state.kv", "plugin.log"})
BUILTIN = "(builtin)"

_AUDIT_RE = re.compile(r"(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}.*\[CapabilityAudit\] plugin=(\S+) cap=(\S+) level=(\S+)")
_MANIFEST_DIR = ".drifox-plugin"


def default_log_paths() -> list:
    """源码版与打包版日志位置（与 crash 日志同款分家约定）。"""
    root = Path(__file__).resolve().parent.parent
    home = Path.home() / ".drifox"
    return [root / ".drifox" / "logs", home / "logs"]


def parse_audits(log_paths, since: str = "") -> dict:
    """扫日志，返回 {插件名: {能力ID: 次数}}。(builtin) 单独归桶不参与差集。

    since: ``YYYY-MM-DD``，只统计该日期（含）之后的审计行。日志跨插件升级
    前后时必须收窄：兼容期（全量放行）时期的使用记录会污染差集判定。
    """
    used: dict = defaultdict(lambda: defaultdict(int))
    for path in log_paths:
        for log_file in sorted(glob.glob(str(Path(path) / "all*.log"))):
            try:
                text = Path(log_file).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line in text.splitlines():
                m = _AUDIT_RE.search(line)
                if m:
                    if since and m.group(1) < since:
                        continue
                    plugin, cap = m.group(2), m.group(3)
                    used[plugin][cap] += 1
    used.pop(BUILTIN, None)
    return used


def load_manifests(plugin_dirs) -> dict:
    """返回 {插件名: {"capabilities": list|None, "api_version": int, "path": str}}。"""
    manifests: dict = {}
    for root in plugin_dirs:
        root = Path(root)
        if not root.is_dir():
            continue
        for manifest_path in root.glob(f"*/{_MANIFEST_DIR}/plugin.json"):
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            name = str(data.get("name") or manifest_path.parent.parent.name)
            caps = data.get("capabilities")
            manifests[name] = {
                "capabilities": caps if isinstance(caps, list) else None,
                "api_version": int(data.get("api_version") or 1),
                "path": str(manifest_path.parent.parent),
            }
    return manifests


def build_report(used: dict, manifests: dict) -> dict:
    """组装差集报告（dict 结构，终端打印与 --json 共用）。"""
    report: dict = {
        "legacy_suggest": {},
        "unused": {},
        "unauthorized": {},
        "no_usage": [],
        "orphan_usage": {},
    }
    for name, info in sorted(manifests.items()):
        caps_used = {c for c in used.get(name, {}) if c not in UNGATED}
        declared = info["capabilities"]
        if declared is None:
            # 兼容期：全量放行，实际使用即建议清单
            if caps_used:
                report["legacy_suggest"][name] = sorted(caps_used)
            else:
                report["no_usage"].append(name)
            continue
        declared_set = {str(x) for x in declared} - UNGATED
        unused = sorted(declared_set - caps_used)
        if unused:
            report["unused"][name] = unused
        exceeded = sorted(caps_used - declared_set)
        if exceeded:
            report["unauthorized"][name] = exceeded
        if not caps_used and not unused:
            report["no_usage"].append(name)
    # 日志里有但本地未安装的插件（已卸载 / 第三方来源）
    for name in sorted(set(used) - set(manifests)):
        report["orphan_usage"][name] = sorted(set(used[name]) - UNGATED)
    return report


def print_report(report: dict, args_since: str = "") -> None:
    has_content = False

    if report.get("legacy_suggest"):
        has_content = True
        print(f"== 兼容期插件建议声明（{len(report['legacy_suggest'])} 个）==")
        for name, caps in report["legacy_suggest"].items():
            items = ", ".join(f'"{c}"' for c in caps)
            print(f"  {name}:")
            print(f'    "capabilities": [{items}]')
        print()

    if report.get("unused"):
        has_content = True
        print(f"== 已声明但从未使用（{len(report['unused'])} 个，可从清单删）==")
        for name, caps in report["unused"].items():
            print(f"  {name}: {', '.join(caps)}")
        print()

    if report.get("unauthorized"):
        has_content = True
        hint = "" if args_since else "（可能为升级 v2 前历史，用 --since 收窄）"
        print(f"== [异常] 用到声明外能力（{len(report['unauthorized'])} 个{hint}）==")
        for name, caps in report["unauthorized"].items():
            print(f"  {name}: {', '.join(caps)}")
        print()

    if report.get("orphan_usage"):
        has_content = True
        print(f"== 本地未安装但日志有使用记录（{len(report['orphan_usage'])} 个）==")
        for name, caps in report["orphan_usage"].items():
            print(f"  {name}: {', '.join(caps)}")
        print()

    if not has_content:
        print("[capability-report] 无差集：所有插件声明与实际使用一致")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="能力池审计差集报告（实际使用 vs manifest 声明）")
    ap.add_argument("--log", action="append", default=[],
                    help="日志目录或文件（可多次传；缺省扫源码版与打包版 all*.log）")
    ap.add_argument("--since", default="", help="只统计 YYYY-MM-DD（含）之后的审计行，排除升级前历史")
    ap.add_argument("--plugins-dir", action="append", default=[],
                    help="插件根目录（可多次传；缺省仓库 plugins/ + 开发/用户级 .drifox/plugins/）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    args = ap.parse_args(argv)

    root = Path(__file__).resolve().parent.parent
    log_paths = args.log or [str(p) for p in default_log_paths() if p.exists()]
    plugin_dirs = args.plugins_dir or [
        str(root / "plugins"),
        str(root / ".drifox" / "plugins"),
        str(Path.home() / ".drifox" / "plugins"),
    ]

    used = parse_audits(log_paths, args.since)
    manifests = load_manifests(plugin_dirs)
    report = build_report(used, manifests)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"[capability-report] 日志源 {len(log_paths)} 个，插件 {len(manifests)} 个，"
            f"审计覆盖 {len(used)} 个\n"
        )
        print_report(report, args.since)
    return 0


if __name__ == "__main__":
    sys.exit(main())

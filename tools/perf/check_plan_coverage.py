#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提交计划覆盖核对：把 `COMMIT_PLAN.md` 声明的文件与 git 实际输出逐个比对。

存在理由（2026-11-27 实证）：同一天内三处「人工处理列表」都出错 ——
  ① 转述数据串了成员    ② 命令数重复计数    ③ 提交清单漏了 1 个文件
而机器核对（本脚本的做法）一次就对。**人工清点的清单会漏，机器核对的覆盖不会。**

判据（不是提示）：
  · 计划里未提及的实际改动文件 = 0  → 通过
  · 计划里提及但工作树并不存在的文件 > 0 → 疑似漏改/漏跑（例如某个修复没做完）
  · 退出码：0 = 覆盖完整；1 = 有未覆盖文件；2 = 计划提及了不存在的文件

**检测边界（已知弱点，必须知道）**：
  本脚本按「文件名是否在计划正文任意位置出现」判定覆盖，**不校验它是否出现在
  某个 commit 的文件清单代码块里**。所以「只在备注/前置确认/原因说明里提过、
  但没列进任何 commit 文件列表」的文件会被判为已覆盖。
  实证（2026-11-27）：删掉 Commit 5 文件清单里 test_storage_engine.py 那一行，
  脚本仍报 PASS —— 因为后文「执行前置确认」和「storage 修正的真实原因」两处
  仍提到该文件名。此时退出码 0，**但实际提交时会漏 add 这个文件**。
  → 因此本脚本能防「整个文件被忘掉」，不能防「文件被移出清单但仍被提及」。
     执行前请人眼确认各 commit 文件清单代码块的行数与 `git diff --numstat` 对得上，
     或后续把判定改为「只认 commit 代码块内的路径」(见 --strict-blocks 待实现)。

允许例外（不算失败）：
  `SELF_ENTRIES` 里的文件 —— 计划自身与其配套工具，不需要写进自己的文件清单。

用法：
  .venv/bin/python tools/perf/check_plan_coverage.py
  .venv/bin/python tools/perf/check_plan_coverage.py --plan tools/perf/COMMIT_PLAN.md
  .venv/bin/python tools/perf/check_plan_coverage.py --strict-untracked   # 未跟踪也算必须覆盖
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLAN = PROJECT_ROOT / "tools" / "perf" / "COMMIT_PLAN.md"

# 计划文件自身与其配套工具：不要求出现在 commit 的文件列表里
SELF_ENTRIES = {
    "tools/perf/COMMIT_PLAN.md",
    "tools/perf/check_plan_coverage.py",
}


def _git(*args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *args],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {out.stderr.strip()}")
    return out.stdout


def actual_modified() -> dict[str, tuple[int, int]]:
    """工作树已修改文件 → (新增行, 删除行)。"""
    rows: dict[str, tuple[int, int]] = {}
    for line in _git("diff", "--numstat").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        # 二进制文件的行数是 "-"
        rows[path] = (
            int(added) if added.isdigit() else -1,
            int(deleted) if deleted.isdigit() else -1,
        )
    return rows


def actual_untracked() -> list[str]:
    """未跟踪文件（已展开目录，逐个文件）。"""
    out = _git("ls-files", "--others", "--exclude-standard")
    return [p for p in out.splitlines() if p.strip()]


def mentioned_in_plan(plan_text: str) -> set[str]:
    """从计划正文抽出所有文件路径（含根目录下的单段路径，如 pyproject.toml）。

    两段式匹配：
      · 多段路径  a/b/c.py
      · 根目录文件 pyproject.toml（仅限已知的根级配置文件，避免把裸词当文件）
    """
    pat_multi = re.compile(
        r"(?:^|[\s`'\"(（:：])((?:[\w.\-]+/)+[\w.\-]+\.(?:py|md|toml|json|txt|cfg|yaml|yml))",
        re.M,
    )
    pat_root = re.compile(
        r"(?:^|[\s`'\"(（:：])((?:pyproject\.toml|setup\.py|pytest\.ini|tox\.ini|"
        r"requirements[\w.\-]*\.txt|\.gitignore|AGENTS\.md|README\.md))\b",
        re.M,
    )
    found = {m.group(1) for m in pat_multi.finditer(plan_text)}
    found |= {m.group(1) for m in pat_root.finditer(plan_text)}
    return found


def commit_blocks(plan_text: str) -> dict[str, set[str]]:
    """抽出每个 commit 的 ``` 文件清单代码块 → {commit 号: 路径集合}。"""
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"## Commit (\d)[^\n]*\n+```[^\n]*\n(.*?)```", plan_text, re.S):
        out[m.group(1)] = mentioned_in_plan(m.group(2))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="提交计划覆盖核对")
    ap.add_argument("--plan", default=str(DEFAULT_PLAN), help="计划文件路径")
    ap.add_argument("--strict-blocks", action="store_true",
                    help="严格模式：只认 commit 代码块内的文件清单（默认只要正文任意处提及即算覆盖）")
    args = ap.parse_args(argv)

    plan_path = Path(args.plan)
    if not plan_path.exists():
        print(f"[FAIL] 计划文件不存在: {plan_path}")
        return 2

    plan_text = plan_path.read_text(encoding="utf-8", errors="replace")
    mentioned = mentioned_in_plan(plan_text)

    modified = actual_modified()
    untracked = actual_untracked()
    actual = set(modified) | set(untracked)

    blocks = commit_blocks(plan_text)
    declared_in_blocks = set().union(*blocks.values()) if blocks else set()

    # 1) 实际有改动但计划没提 → 真漏项
    if args.strict_blocks:
        covered = declared_in_blocks
        mode = "严格（只认 commit 代码块）"
    else:
        covered = mentioned
        mode = "宽松（正文任意处提及即算）"
    uncovered = sorted(actual - covered - SELF_ENTRIES)
    # 2) 计划提了但工作树没有 → 可能是「该改的没改」（如某个修复未完成）
    phantom = sorted(
        m for m in mentioned - actual - SELF_ENTRIES
        if (PROJECT_ROOT / m).exists() is False and m not in modified
    )

    print("=" * 68)
    print(f"计划文件：{plan_path.relative_to(PROJECT_ROOT)}")
    print(f"判定模式：{mode}")
    print(f"计划提及路径：{len(mentioned)} 个   其中在 commit 代码块内：{len(declared_in_blocks)} 个")
    print(f"实际已修改：{len(modified)} 个   实际未跟踪：{len(untracked)} 个")
    print(f"（豁免：{len(SELF_ENTRIES)} 个计划自身/配套文件）")
    print("=" * 68)

    ok = True

    if uncovered:
        ok = False
        print(f"\n[FAIL] 有改动但计划{'代码块' if args.strict_blocks else ''}未提及：{len(uncovered)} 个")
        for f in uncovered:
            stat = modified.get(f)
            if stat:
                print(f"   M {f}  (+{stat[0]}/-{stat[1]})")
            else:
                print(f"   ?? {f}  (未跟踪)")
        print("  → 补进计划对应 commit 的文件清单，或确认它不该被提交")
    else:
        print(f"\n[PASS] 所有工作树改动都已在计划{'代码块' if args.strict_blocks else ''}中列出")

    # 宽松模式下额外提示：正文提过但没进任何 commit 代码块的文件
    if not args.strict_blocks and blocks:
        only_in_text = sorted((mentioned & actual) - declared_in_blocks - SELF_ENTRIES)
        if only_in_text:
            print(f"\n[提示] 正文提及但未进任何 commit 代码块：{len(only_in_text)} 个")
            for f in only_in_text:
                print(f"   ? {f}")
            print("  → 这些文件提交时会漏 add。加 --strict-blocks 可把它们判为失败")

    if phantom:
        ok = False
        print(f"\n[WARN] 计划提及但工作树无此改动：{len(phantom)} 个")
        for f in phantom:
            print(f"   - {f}")
        print("  → 可能是「该改的还没改」（如依赖的修复未跑完），执行前确认")

    # 分组统计：只取每个 commit 的 ``` 文件清单代码块（避免把后文的自检表算进去）
    print("\n各 commit 声明的文件数（取其 ``` 代码块内的路径）：")
    for num in sorted(blocks, key=lambda x: int(x)):
        found = blocks[num]
        declared = found & actual
        extra = found - actual
        note = f"（其中 {len(extra)} 个当前工作树不存在）" if extra else ""
        print(f"   Commit {num}: 声明 {len(found)} 个，实际存在 {len(declared)} 个{note}")

    print()
    if ok:
        print("结论：覆盖完整")
        return 0
    print("结论：存在问题，见上方 [FAIL]/[WARN]")
    return 1 if uncovered else 2


if __name__ == "__main__":
    sys.exit(main())

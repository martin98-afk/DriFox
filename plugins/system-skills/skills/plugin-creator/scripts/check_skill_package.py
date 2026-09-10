#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""技能包结构自检脚本（plugin-creator / ui-plugin-creator 通用，参数化）。

用法（在技能包目录或指向技能包路径执行）：
    python scripts/check_skill_package.py .
    python scripts/check_skill_package.py ../ui-plugin-creator
    python scripts/check_skill_package.py <skill-dir> --max-lines 200

校验项：
  1. frontmatter name/description 非空且 description ≥ 20 字
  2. SKILL.md 行数上限（默认按技能包名：plugin-creator→200 / ui-plugin-creator→230，
     可用 --max-lines 覆盖）
  3. SKILL.md 中引用的 references/ 与 assets/ 文件全部存在；references/ 无孤儿文件
  4. evals/*.json 为合法 JSON；cases ≥13；正例 ≥6；反例 ≥6；
     near-miss ≥1；ambiguous ≥1；id 唯一
  5. SKILL.md 存在硬停止段与闭环段
  6. 退出码：0=通过 / 1=结构错误 / 2=警告（结构可过但有告警）

纯标准库（json / re / pathlib / argparse），Python 3.10+ 跨平台。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 各技能包默认行数上限（--max-lines 可覆盖）
DEFAULT_LINE_LIMITS = {
    "plugin-creator": 200,
    "ui-plugin-creator": 230,
}
FALLBACK_LINE_LIMIT = 230

REQUIRED_SECTIONS = ("硬停止", "闭环")


class Report:
    """收集错误（exit 1）与警告（exit 2）。"""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def exit_code(self) -> int:
        if self.errors:
            return 1
        if self.warnings:
            return 2
        return 0


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """解析 YAML frontmatter（简单 key: value 形式），返回 (fields, body)。"""
    fields: dict[str, str] = {}
    body = text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return fields, body
    block = m.group(1)
    body = text[m.end():]
    current_key = None
    for line in block.splitlines():
        kv = re.match(r"^(\w[\w-]*):\s*(.*)$", line)
        if kv:
            current_key = kv.group(1)
            fields[current_key] = kv.group(2).strip().strip('"')
        elif line.startswith((" ", "\t")) and current_key:
            # 多行值续行（如 description 跨行）
            fields[current_key] += " " + line.strip().strip('"')
    return fields, body


def check_frontmatter(fields: dict[str, str], rep: Report) -> None:
    name = fields.get("name", "").strip()
    desc = fields.get("description", "").strip()
    if not name:
        rep.error("frontmatter 缺少 name")
    if not desc:
        rep.error("frontmatter 缺少 description")
    elif len(desc) < 20:
        rep.error(f"frontmatter description 过短（{len(desc)} 字 < 20）")
    if not fields.get("license", "").strip():
        rep.warn("frontmatter 建议声明 license")


def check_line_limit(n_lines: int, limit: int, rep: Report) -> None:
    if n_lines > limit:
        rep.error(f"SKILL.md 行数 {n_lines} 超上限 {limit}")
    else:
        print(f"  SKILL.md 行数: {n_lines} / 上限 {limit} ✓")


def check_references(skill_dir: Path, body: str, rep: Report) -> None:
    """SKILL.md 引用的 references/、assets/ 文件存在 + references 无孤儿。"""
    # 1) 引用完整性
    referenced: set[Path] = set()
    for m in re.finditer(r"(?:references|assets)/[\w\-./]+", body):
        rel = Path(m.group(0).rstrip(".,:)】」"))
        path = skill_dir / rel
        referenced.add(rel)
        if not path.exists():
            rep.error(f"SKILL.md 引用不存在: {rel.as_posix()}")

    # 2) 孤儿 references（存在但 SKILL.md 从未提及；examples/ 不做孤儿检查）
    refs_dir = skill_dir / "references"
    if refs_dir.is_dir():
        for f in sorted(refs_dir.rglob("*.md")):
            rel = f.relative_to(skill_dir)
            if rel not in referenced:
                # 容忍子目录索引文件被父文件覆盖提及
                covered = any(
                    str(r).startswith(str(rel.parent.as_posix()) + "/")
                    or Path(str(r)).parent == rel.parent
                    for r in referenced
                )
                if not covered and rel.as_posix() not in {r.as_posix() for r in referenced}:
                    rep.warn(f"references 孤儿文件（SKILL.md 未引用）: {rel.as_posix()}")


def check_evals(skill_dir: Path, rep: Report) -> None:
    evals_dir = skill_dir / "evals"
    if not evals_dir.is_dir():
        rep.error("缺少 evals/ 目录")
        return
    json_files = sorted(evals_dir.glob("*.json"))
    if not json_files:
        rep.error("evals/ 下没有 JSON 评测文件")
        return

    for jf in json_files:
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            rep.error(f"evals/{jf.name} 不是合法 JSON: {e}")
            continue
        cases = data.get("cases", [])
        ids = [c.get("id", "") for c in cases]
        types = [c.get("type", "") for c in cases]

        if len(ids) != len(set(ids)):
            dup = {i for i in ids if ids.count(i) > 1}
            rep.error(f"evals/{jf.name} id 重复: {sorted(dup)}")
        if len(cases) < 13:
            rep.error(f"evals/{jf.name} cases={len(cases)} < 13")
        if types.count("positive") < 6:
            rep.error(f"evals/{jf.name} 正例 {types.count('positive')} < 6")
        if types.count("negative") < 6:
            rep.error(f"evals/{jf.name} 反例 {types.count('negative')} < 6")
        if types.count("near-miss") < 1:
            rep.error(f"evals/{jf.name} near-miss {types.count('near-miss')} < 1")
        if types.count("ambiguous") < 1:
            rep.error(f"evals/{jf.name} ambiguous {types.count('ambiguous')} < 1")
        print(f"  evals/{jf.name}: {len(cases)} cases "
              f"(正 {types.count('positive')} / 反 {types.count('negative')} / "
              f"边界 {types.count('near-miss') + types.count('ambiguous')}) ✓")


def check_required_sections(body: str, rep: Report) -> None:
    for sec in REQUIRED_SECTIONS:
        if sec not in body:
            rep.error(f"SKILL.md 缺少「{sec}」段")
        else:
            print(f"  段落「{sec}」存在 ✓")


def main() -> int:
    parser = argparse.ArgumentParser(description="技能包结构自检")
    parser.add_argument("skill_dir", help="技能包目录（含 SKILL.md）")
    parser.add_argument(
        "--max-lines", type=int, default=None,
        help="SKILL.md 行数上限（默认按技能包名取 200/230）",
    )
    args = parser.parse_args()

    skill_dir = Path(args.skill_dir).resolve()
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        print(f"[结构错误] 未找到 {skill_md}", file=sys.stderr)
        return 1

    rep = Report()
    raw = skill_md.read_text(encoding="utf-8")
    fields, body = parse_frontmatter(raw)

    print(f"检查 {skill_dir.name} @ {skill_dir}")

    # 1) frontmatter
    check_frontmatter(fields, rep)
    print("  frontmatter name/description ✓" if not any("frontmatter" in e for e in rep.errors)
          else "  frontmatter 存在问题（见下）")

    # 2) 行数上限
    n_lines = len(raw.splitlines())
    limit = args.max_lines or DEFAULT_LINE_LIMITS.get(
        fields.get("name", skill_dir.name), FALLBACK_LINE_LIMIT
    )
    check_line_limit(n_lines, limit, rep)

    # 3) references/assets 引用完整性 + 孤儿
    check_references(skill_dir, body, rep)

    # 4) evals
    check_evals(skill_dir, rep)

    # 5) 必需段落
    check_required_sections(body, rep)

    # 6) 汇总
    code = rep.exit_code()
    for w in rep.warnings:
        print(f"[警告] {w}")
    for e in rep.errors:
        print(f"[结构错误] {e}")
    print(f"结果: {'通过' if code == 0 else ('有警告' if code == 2 else '失败')} (exit {code})")
    return code


if __name__ == "__main__":
    sys.exit(main())

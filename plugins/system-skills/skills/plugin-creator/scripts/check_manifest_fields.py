#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_manifest_fields.py — manifest 文档字段表 vs 宿主 schema 一致性。

为什么存在：references/manifest.md 的「字段总表」是插件作者唯一的字段依据，
但它靠手维护。宿主 app/plugins/contracts/manifest_schema.py 加字段（如
display_name / min_host_version / platforms）后文档不跟，作者就查不到这些字段，
只能去翻源码或凭猜——2026-11 发 context-insight 时实测踩到。

用法（在 DriFox 仓库内）：
    python scripts/check_manifest_fields.py
    python scripts/check_manifest_fields.py --doc references/manifest.md

退出码：0 一致 / 1 有漂移 / 2 找不到宿主 schema（非仓库环境，跳过）
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

REPO_ROOT_CANDIDATES = [
    Path(__file__).resolve().parents[5],  # <repo>/plugins/system-skills/skills/plugin-creator/scripts/
    Path.cwd(),
]
SCHEMA_PY = Path("app/plugins/contracts/manifest_schema.py")
SCHEMA_JSON = Path("schemas/plugin.schema.json")

# 表格首列形如：| `field` | ...
ROW_RE = re.compile(r"^\|\s*`([A-Za-z_][A-Za-z0-9_]*)`\s*\|")


def find_repo_root() -> Path | None:
    for cand in REPO_ROOT_CANDIDATES:
        if (cand / SCHEMA_PY).is_file():
            return cand
    return None


def host_fields(repo: Path) -> tuple[set[str], list[str]]:
    """从宿主 schema 源码里取 SPEC 字段名；JSON schema 的 properties 作为补充。"""
    fields: set[str] = set()
    sources: list[str] = []
    src = repo / SCHEMA_PY
    tree = ast.parse(src.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        else:
            continue
        if "SPEC" not in targets or not isinstance(node.value, ast.Dict):
            continue
        for k in node.value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                fields.add(k.value)
        sources.append(SCHEMA_PY.as_posix())
    js = repo / SCHEMA_JSON
    if js.is_file():
        try:
            props = json.loads(js.read_text(encoding="utf-8")).get("properties") or {}
            fields.update(props.keys())
            sources.append(SCHEMA_JSON.as_posix())
        except (json.JSONDecodeError, AttributeError):
            pass
    return fields, sources


def documented_fields(doc: Path) -> set[str]:
    found: set[str] = set()
    for line in doc.read_text(encoding="utf-8").splitlines():
        m = ROW_RE.match(line)
        if m:
            found.add(m.group(1))
    return found


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="校验 manifest 字段表与宿主 schema 一致")
    parser.add_argument("--doc", type=Path, default=here / "references/manifest.md")
    args = parser.parse_args(argv)

    repo = find_repo_root()
    if repo is None:
        print("[skip] 未找到宿主仓库（app/plugins/contracts/manifest_schema.py），跳过字段一致性检查")
        return 2
    if not args.doc.is_file():
        print(f"[error] 文档不存在: {args.doc}")
        return 1

    host, sources = host_fields(repo)
    doc = documented_fields(args.doc)
    missing = sorted(host - doc)

    print(f"宿主字段 {len(host)} 个（来源 {', '.join(sources)}）")
    print(f"文档字段 {len(doc)} 个（{args.doc}）")
    if not missing:
        print("[OK] 字段表与宿主 schema 一致")
        return 0

    print(f"[FAIL] 文档缺 {len(missing)} 个宿主字段，作者按文档写会漏掉它们：")
    for name in missing:
        print(f"  - {name}")
    print("修法：在 references/manifest.md 字段总表补行（类型/默认值以宿主 schema 为准）")
    return 1


if __name__ == "__main__":
    sys.exit(main())

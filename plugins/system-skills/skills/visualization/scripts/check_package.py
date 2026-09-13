"""visualization 技能包结构校验。

检查项：
1. SKILL.md 存在且 frontmatter 含 name/description
2. SKILL.md 引用的 references/*.md 文件都存在
3. references 内无悬空引用（引用不存在的同级文件）
4. evals/trigger-eval.json 可解析且用例字段完整

用法：python scripts/check_package.py <技能目录>
"""
import json
import re
import sys
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent.parent
    errors: list[str] = []

    skill_md = root / "SKILL.md"
    if not skill_md.exists():
        print(f"FAIL: {skill_md} 不存在")
        return 1

    text = skill_md.read_text(encoding="utf-8")

    # 1. frontmatter
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        errors.append("SKILL.md 缺 frontmatter")
    else:
        fm = m.group(1)
        if not re.search(r"^name:\s*\S", fm, re.MULTILINE):
            errors.append("frontmatter 缺 name")
        if not re.search(r"^description:\s*\S", fm, re.MULTILINE):
            errors.append("frontmatter 缺 description")

    # 2. SKILL.md 引用的 references 必须存在
    refs = set(re.findall(r"references/([\w-]+\.md)", text))
    for r in sorted(refs):
        if not (root / "references" / r).exists():
            errors.append(f"SKILL.md 引用的 references/{r} 不存在")

    # 3. references 内部互引不悬空
    for r in sorted((root / "references").glob("*.md")):
        body = r.read_text(encoding="utf-8")
        for inner in set(re.findall(r"`?([\w-]+\.md)`?", body)):
            if inner.endswith(".md") and inner != r.name and not (root / "references" / inner).exists():
                # 引用路径形如 html-widget.md 时视为同级引用
                if f"{inner}" in body and not (root / "references" / inner).exists():
                    errors.append(f"references/{r.name} 悬空引用 {inner}")

    # 4. evals 用例字段完整
    eval_path = root / "evals" / "trigger-eval.json"
    if eval_path.exists():
        try:
            data = json.loads(eval_path.read_text(encoding="utf-8"))
            cases = data.get("trigger_cases", [])
            if len(cases) < 5:
                errors.append(f"评测用例过少: {len(cases)} < 5")
            for c in cases:
                for field in ("id", "input", "expect_visualization", "reason"):
                    if field not in c:
                        errors.append(f"用例 {c.get('id', '?')} 缺字段 {field}")
            # 正反例都要有
            if not any(c.get("expect_visualization") for c in cases):
                errors.append("缺正例（expect_visualization=true）")
            if not any(not c.get("expect_visualization") for c in cases):
                errors.append("缺反例（expect_visualization=false）")
        except json.JSONDecodeError as e:
            errors.append(f"trigger-eval.json 解析失败: {e}")
    else:
        errors.append("evals/trigger-eval.json 不存在")

    if errors:
        for e in errors:
            print(f"FAIL: {e}")
        return 1

    print(f"PASS: structure_status=artifact_consistent")
    print(f"  SKILL.md 路由面完整")
    print(f"  references: {sorted(refs)}")
    print(f"  evals: {len(json.loads(eval_path.read_text(encoding='utf-8'))['trigger_cases'])} 用例（含正反例）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

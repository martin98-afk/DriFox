"""临时脚本：从 message_card.py 骨架 f-string 提取 JS 做语法检查。

ast 解析 JoinedStr：Constant 原样保留（{{}} 已被 Python 反转义），
FormattedValue（插值）替换为 0，拼出静态骨架文本，抽 <script> 交给 node --check。
"""
import ast
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path("app/widgets/message_card.py")
tree = ast.parse(SRC.read_text(encoding="utf-8"))


def join_joined_str(node: ast.JoinedStr) -> str:
    parts = []
    for v in node.values:
        if isinstance(v, ast.Constant):
            parts.append(str(v.value))
        else:  # FormattedValue → 占位（URL/颜色/数字位置的插值都兼容 0）
            parts.append("0")
    return "".join(parts)


# 收集文件中所有 JoinedStr（骨架是最大的 f-string）
candidates: list[tuple[int, str]] = []
for node in ast.walk(tree):
    if isinstance(node, ast.JoinedStr):
        text = join_joined_str(node)
        candidates.append((len(text), text))

candidates.sort(reverse=True)
print(f"共 {len(candidates)} 个 f-string，最大 {candidates[0][0]} 字符")

html = candidates[0][1]
if "<script>" not in html:
    print("FAIL: 最大 f-string 不含 <script>，骨架定位错误")
    sys.exit(1)

# 提取最后一个 <script> 到 </script>（骨架主 JS）
start = html.rfind("<script>")
end = html.find("</script>", start)
js = html[start + len("<script>") : end]
print(f"提取 JS {len(js)} 字符")

with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
    f.write(js)
    tmp = f.name

r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
if r.returncode == 0:
    print("JS SYNTAX OK")
else:
    print("JS SYNTAX ERROR:")
    print(r.stderr[:3000])
    sys.exit(1)

# -*- coding: utf-8 -*-
"""一次性修复：worktree 占位页文案（用后即删）"""
from pathlib import Path
import ast

p = Path('app/widgets/workbench_panel.py')
src = p.read_text(encoding='utf-8')
good = '_PagePlaceholder("工作树页未加载\\n\\n插件未注册或已卸载", parent=self._stack)'
if '_PagePlaceholder("工作树页未加载' in src and '插件未注册或已卸载", parent=self._stack)' not in src:
    # 上一条命令写坏了文件：替换坏行及其续行
    lines = src.split('\n')
    out = []
    skip = False
    for l in lines:
        if skip:
            skip = False
            continue
        if '_PagePlaceholder("工作树页未加载' in l:
            indent = l[: len(l) - len(l.lstrip())]
            out.append(indent + good)
            skip = True
            continue
        out.append(l)
    src = '\n'.join(out)
else:
    old = '        self._worktree_placeholder = _PagePlaceholder(parent=self._stack)'
    assert old in src, 'anchor missing'
    src = src.replace(old, '        ' + good)
p.write_text(src, encoding='utf-8')
ast.parse(src)
print('placeholder text fixed')

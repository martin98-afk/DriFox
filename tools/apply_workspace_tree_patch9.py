# -*- coding: utf-8 -*-
"""一次性幂等补丁（第九部分）：test_tab_panel_compact.py 也固定列表模式

同第五部分的理由：TabPanel 会恢复持久化的显示模式（Settings.tab_panel_mode），
本文件断言的是列表布局 + 折叠态（_list_layout / 紧凑图标），本地存了 tree 时
5 个用例会被误判成回归（mode=list 时 35/35 全过）。
"""

import io
import os
import sys
from typing import List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PATCHES: List[Tuple[str, List[Tuple[str, str]]]] = []


def edit(rel_path: str, pairs: List[Tuple[str, str]]):
    PATCHES.append((rel_path, pairs))


edit(
    "tests/widgets/test_tab_panel_compact.py",
    [
        (
            "    with patch(\"app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync\"):\n"
            "        p = TabPanel()\n"
            "    qtbot.addWidget(p)\n"
            "    return p\n",
            "    with patch(\"app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync\"):\n"
            "        p = TabPanel()\n"
            "    # 固定为列表模式：本文件断言的是列表布局 + 折叠态（_list_layout /\n"
            "    # 紧凑图标）。TabPanel 会恢复上次持久化的模式（Settings.tab_panel_mode），\n"
            "    # 本地存了 tree 时整组用例会被误判成回归。\n"
            "    p.set_mode(\"list\", persist=False)\n"
            "    qtbot.addWidget(p)\n"
            "    return p\n",
        ),
    ],
)


def _read_lf(rel_path: str) -> Tuple[str, bool]:
    abs_path = os.path.join(ROOT, rel_path)
    with io.open(abs_path, "r", encoding="utf-8", newline="") as f:
        raw = f.read()
    crlf = raw.count("\r\n") == raw.count("\n") and raw.count("\n") > 0
    return raw.replace("\r\n", "\n"), crlf


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    errors: List[str] = []
    for rel_path, pairs in PATCHES:
        abs_path = os.path.join(ROOT, rel_path)
        if not os.path.isfile(abs_path):
            errors.append(f"[缺失文件] {rel_path}")
            continue
        text, _crlf = _read_lf(rel_path)
        for idx, (old, _new) in enumerate(pairs):
            count = text.count(old)
            if count != 1:
                errors.append(f"[命中 {count} 次，应为 1] {rel_path} #{idx}: {old[:70]!r}")
    if errors:
        print("补丁校验失败，未写入任何文件：")
        for e in errors:
            print("  " + e)
        return 1

    for rel_path, pairs in PATCHES:
        abs_path = os.path.join(ROOT, rel_path)
        text, crlf = _read_lf(rel_path)
        for old, new in pairs:
            text = text.replace(old, new, 1)
        if crlf:
            text = text.replace("\n", "\r\n")
        with io.open(abs_path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        print(f"已写入 {rel_path}（{len(pairs)} 处）")
    print("全部补丁应用成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())

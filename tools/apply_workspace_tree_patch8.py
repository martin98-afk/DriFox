# -*- coding: utf-8 -*-
"""一次性幂等补丁（第八部分）：徽标可见性必须由内容驱动

发现：_count_label / _active_label 只 setText 不 setVisible。
- _active_label 在 __init__ 里 setVisible(False)，之后没人再打开 → 胶囊永不显示。
- 更糟的是 set_compact(False) 会无条件 setVisible(True)：文本为空时会渲染出一枚
  空的信息色/灰色小药丸。

改法：把可见性收敛到 _sync_badges()，判据 = 「有文本 且 非紧凑态」，
apply_spec 与 set_compact 都只调它。
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
    "app/widgets/workspace_tree.py",
    [
        (
            "        self._count_label.setVisible(not compact)\n"
            "        self._active_label.setVisible(not compact)\n"
            "        self._arrow.setVisible(not compact)\n",
            "        self._sync_badges()\n"
            "        self._arrow.setVisible(not compact)\n",
        ),
        (
            "        count_text = str(spec.count) if spec.count > 0 else \"\"\n"
            "        if self._count_label.text() != count_text:\n"
            "            self._count_label.setText(count_text)\n"
            "        active_text = f\"{spec.active_count} 激活\" if spec.active_count > 0 else \"\"\n"
            "        if self._active_label.text() != active_text:\n"
            "            self._active_label.setText(active_text)\n",
            "        count_text = str(spec.count) if spec.count > 0 else \"\"\n"
            "        if self._count_label.text() != count_text:\n"
            "            self._count_label.setText(count_text)\n"
            "        self._active_count = int(spec.active_count or 0)\n"
            "        active_text = f\"{self._active_count} 激活\" if self._active_count > 0 else \"\"\n"
            "        if self._active_label.text() != active_text:\n"
            "            self._active_label.setText(active_text)\n"
            "        self._sync_badges()\n",
        ),
        (
            "        self._spec_bold = False\n"
            "        self._icon_px = scale_icon_size(14)\n",
            "        self._spec_bold = False\n"
            "        self._active_count = 0\n"
            "        self._icon_px = scale_icon_size(14)\n",
        ),
        (
            "    # ── 状态 ─────────────────────────────────────────────────────\n"
            "    def set_expanded(self, expanded: bool, animate: bool = True):\n",
            "    def _sync_badges(self):\n"
            "        \"\"\"计数徽标 / 已激活胶囊的可见性：有文本 且 非紧凑态才显示\n"
            "\n"
            "        只setText不setVisible 会让胶囊永远不出现；而 set_compact(False) 无条件\n"
            "        setVisible(True) 又会在文本为空时渲染出空药丸。统一收敛到这里。\n"
            "        \"\"\"\n"
            "        shown = not self._compact\n"
            "        self._count_label.setVisible(bool(self._count_label.text()) and shown)\n"
            "        self._active_label.setVisible(bool(self._active_label.text()) and shown)\n"
            "\n"
            "    # ── 状态 ─────────────────────────────────────────────────────\n"
            "    def set_expanded(self, expanded: bool, animate: bool = True):\n",
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

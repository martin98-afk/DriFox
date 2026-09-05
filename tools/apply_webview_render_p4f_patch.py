# -*- coding: utf-8 -*-
"""WebView 渲染增强 P4f —— 修复桥装配晚于插件脚本执行的时序 bug。

现象：插件 fence 的按钮点了没反应。

根因：插件脚本末尾常有"首帧兜底"的主动初始化（脚本一执行就跑），而宿主的
_syncFenceBridge() 原先只在 assets **onload 回调之后**才执行。于是插件第一次
初始化时看到的是空桥 → 按"未授权"兜底把按钮 disabled 并打上幂等标记；
等桥装配好、宿主再来调 init 时，幂等标记让它直接 return，状态永久锁死。

修复：桥不依赖 assets 加载，**在发起加载之前就先装配一次**（加载完成后再装配
一次，覆盖 assets 注入期间新增的 fence lang）。

用法：
    .venv/Scripts/python.exe tools/apply_webview_render_p4f_patch.py [--check]
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MC = "app/widgets/message_card.py"

OLD_RUN = """                window._runFenceAssets = function () {{
                    var _flangs = _scanFenceLangs();
                    _ensureFenceAssets(_flangs, function () {{
                        try {{ window._syncFenceBridge(); }} catch (e) {{
                            console.error('[fence] bridge sync:', e);
                        }}"""

NEW_RUN = """                function _syncFenceBridgeSafe() {{
                    try {{ window._syncFenceBridge(); }} catch (e) {{
                        console.error('[fence] bridge sync:', e);
                    }}
                }}
                window._runFenceAssets = function () {{
                    var _flangs = _scanFenceLangs();
                    // 桥必须先于 assets 装配：插件脚本末尾常有"首帧兜底"的主动初始化
                    // （一执行就跑），那时若桥还是空的，插件会把"未授权"状态写死在
                    // 节点上，之后再装配也救不回来（幂等标记已打）。
                    _syncFenceBridgeSafe();
                    _ensureFenceAssets(_flangs, function () {{
                        // 加载完成后再装配一次，覆盖注入期间新增的 fence lang
                        _syncFenceBridgeSafe();"""

PATCHES = [
    (MC, "桥先于 assets 装配", OLD_RUN, NEW_RUN, 1),
]


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def main() -> int:
    check_only = "--check" in sys.argv
    by_file: dict[str, list[tuple[str, str, str, int]]] = {}
    for rel, desc, old, new, expect in PATCHES:
        by_file.setdefault(rel, []).append((desc, old, new, expect))

    errors: list[str] = []
    plans: dict[str, list[tuple[str, str]]] = {}

    for rel, items in by_file.items():
        path = os.path.join(ROOT, rel)
        text = _read(path)
        nl = "\r\n" if "\r\n" in text else "\n"
        for desc, old, new, expect in items:
            old_n = old.replace("\n", nl)
            new_n = new.replace("\n", nl)
            hits = text.count(old_n)
            if hits != expect:
                errors.append(f"[HIT {hits}/{expect}] {rel} :: {desc}")
            else:
                plans.setdefault(rel, []).append((old_n, new_n))
                text = text.replace(old_n, new_n, 1)

    if errors:
        print("校验失败，未写入任何文件：")
        for e in errors:
            print("  " + e)
        return 1

    print(f"校验通过：{sum(len(v) for v in plans.values())} 处改动全部命中。")
    if check_only:
        return 0

    for rel, pairs in plans.items():
        path = os.path.join(ROOT, rel)
        text = _read(path)
        for old_n, new_n in pairs:
            text = text.replace(old_n, new_n, 1)
        _write(path, text)
        print(f"  写入 {rel}（{len(pairs)} 处）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

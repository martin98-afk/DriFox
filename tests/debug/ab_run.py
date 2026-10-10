# -*- coding: utf-8 -*-
"""严格 A/B 对照运行器：同探针、同采样率，对比「当前工作区」与「HEAD」两版代码。

为什么需要它
------------
流畅度指标（高度步进 p50 / 文字台阶 / 主线程 jank）对**采样率**极敏感：
改了追踪节拍却不改采样率，会得到方向相反的假结论（实测 16ms 拍在 50ms
采样下显示幅度 4→11px 的「恶化」，实际是采样把多个追踪步合并了）。
本脚本保证两版跑的是**同一份探针、同一套采样参数**，只换被测源码。

用法
----
python tests/debug/ab_run.py <probe.py> [--files a.py,b.py]

- probe.py：待跑探针（相对本文件或绝对路径）
- --files：需要用 HEAD 版本覆盖做对照的源码文件，默认三个流式热文件
输出：两版原始输出 + 关键指标提取并排对照。
"""

import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_FILES = [
    "app/widgets/card_render_core.py",
    "app/widgets/card_viewers.py",
    "app/widgets/message_card.py",
    "app/main_widget.py",
]

ENV_EXTRA = {
    "QTWEBENGINE_CHROMIUM_FLAGS": (
        "--disable-gpu --no-sandbox --disable-dev-shm-usage "
        "--disable-software-rasterizer --disable-smooth-scrolling "
        "--enable-low-end-device-mode"
    ),
}


def run_probe(probe, tag):
    env = dict(os.environ)
    env.update(ENV_EXTRA)
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, probe), tag],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
        timeout=600,
    )
    out = r.stdout or ""
    return out


# 关键指标提取：把探针输出里的数值行抽成 (场景, 指标key) -> 值
_PATTERNS = [
    (re.compile(r"文字增长台阶: 采样增量 p50=(\d+) p90=(\d+) max=(\d+)"), "文字台阶", ("p50", "p90", "max")),
    (
        re.compile(r"卡片高度: 变化 (\d+) 次, 幅度 p50=(\d+), max=(\d+), 方向翻转 (\d+) 次"),
        "卡片高度",
        ("变化次数", "幅度p50", "max", "翻转"),
    ),
    (re.compile(r"_twReset (\d+) 次, 其中丢弃缓冲 (\d+) 次, 累计丢弃 (\d+) 字符"), "打字机", ("reset", "丢弃次数", "丢弃字符")),
    (re.compile(r"主线程帧间隔: p50=(\d+) p90=(\d+) p99=(\d+) max=(\d+)"), "主线程帧", ("p50", "p90", "p99", "max")),
    (re.compile(r"页面 rAF 帧间隔: p50=(\d+) p90=(\d+) p99=(\d+) max=(\d+)"), "页面rAF", ("p50", "p90", "p99", "max")),
]


def extract(out):
    """输出 -> {(场景, 指标): {name: value}}"""
    res = {}
    scene = ""
    for line in out.splitlines():
        m = re.match(r"===== (?:场景 )?(.+?) =====", line.strip())
        if m:
            scene = m.group(1)
            continue
        for pat, key, names in _PATTERNS:
            mm = pat.search(line)
            if mm:
                res[(scene, key)] = dict(zip(names, [int(x) for x in mm.groups()]))
    return res


def main():
    argv = [a for a in sys.argv[1:]]
    files = DEFAULT_FILES
    if "--files" in argv:
        i = argv.index("--files")
        files = argv[i + 1].split(",")
        argv = argv[:i] + argv[i + 2 :]
    probe = argv[0] if argv else "tests/debug/stream_smoothness_metrics.py"

    print(f"[A] 当前工作区 — {probe}")
    out_a = run_probe(probe, "A")
    print(out_a)

    backups = []
    try:
        for f in files:
            p = os.path.join(ROOT, f)
            bak = p + ".ab_bak"
            shutil.copy(p, bak)
            backups.append((p, bak))
            raw = subprocess.run(["git", "show", f"HEAD:{f}"], capture_output=True, cwd=ROOT).stdout
            if raw:
                with open(p, "wb") as fh:
                    fh.write(raw)
        print(f"\n[B] HEAD 基线 — {probe}")
        out_b = run_probe(probe, "B")
        print(out_b)
    finally:
        for p, bak in backups:
            shutil.copy(bak, p)
            os.remove(bak)

    a, b = extract(out_a), extract(out_b)
    print("\n" + "=" * 72)
    print("对照（A=当前工作区 / B=HEAD）")
    print("=" * 72)
    keys = sorted(set(a) | set(b), key=lambda k: (str(k[0]), str(k[1])))
    for k in keys:
        va, vb = a.get(k), b.get(k)
        if not va or not vb:
            print(f"  {k[0]} | {k[1]}: 仅一侧有数据")
            continue
        parts = []
        for name in va:
            x, y = va[name], vb.get(name)
            mark = "" if x == y else ("↓" if x < y else "↑")
            parts.append(f"{name}: {y}→{x}{mark}")
        print(f"  {k[0]} | {k[1]}: " + "  ".join(parts))
    print("=" * 72)


main()

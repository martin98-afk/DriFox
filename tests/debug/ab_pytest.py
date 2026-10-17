# -*- coding: utf-8 -*-
"""失败集合 A/B 对照：只比「哪些测试失败」，不看绝对数。

项目里存在一批预存失败（历史原因），跑全量看绝对数毫无意义。本脚本把
「当前工作区」与「HEAD」两版源码分别跑同一批测试，输出：
  - NEW FAIL：只有工作区失败的（本次改动引入，必须修）
  - FIXED：只有 HEAD 失败的（本次改动顺带修好）
  - BOTH：两版都失败（预存噪声，忽略）

用法：python tests/debug/ab_pytest.py <test路径...>
"""

import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FILES = [
    "app/widgets/card_render_core.py",
    "app/widgets/card_viewers.py",
    "app/widgets/message_card.py",
    "app/main_widget.py",
]

ENV_EXTRA = {
    "QT_QPA_PLATFORM": "offscreen",
    # 不带这组 flags 时 WebEngine 进程在多测试连跑中随机 segfault（无 FAILED 行
    # 直接死掉），会把「崩溃」误判成「全过」。必须与探针环境一致。
    "QTWEBENGINE_CHROMIUM_FLAGS": (
        "--disable-gpu --no-sandbox --disable-dev-shm-usage "
        "--disable-software-rasterizer --disable-smooth-scrolling "
        "--enable-low-end-device-mode"
    ),
}


def run(tests):
    env = dict(os.environ)
    env.update(ENV_EXTRA)
    r = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q", "-p", "no:randomly"],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
        timeout=1800,
    )
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    fails = set(re.findall(r"^(?:FAILED|ERROR) (\S+)", out, re.M))
    # 崩溃守卫：非 0 退出且没有 summary 行 = 进程中途死掉，结果不可信
    crashed = r.returncode not in (0, 1) or ("passed" not in out and "no tests ran" not in out)
    return fails, out, crashed, r.returncode


def main():
    tests = sys.argv[1:]
    if not tests:
        print("用法: python tests/debug/ab_pytest.py <test路径...>")
        return

    print("[A] 当前工作区 ...", flush=True)
    A, out_a, crash_a, rc_a = run(tests)
    if crash_a:
        print(f"  ⚠️ A 版进程异常退出 rc={rc_a}，结果不可信", flush=True)
    backups = []
    try:
        for f in FILES:
            p = os.path.join(ROOT, f)
            bak = p + ".ab_bak"
            shutil.copy(p, bak)
            backups.append((p, bak))
            raw = subprocess.run(["git", "show", f"HEAD:{f}"], capture_output=True, cwd=ROOT).stdout
            if raw:
                with open(p, "wb") as fh:
                    fh.write(raw)
        print("[B] HEAD 基线 ...", flush=True)
        B, out_b, crash_b, rc_b = run(tests)
        if crash_b:
            print(f"  ⚠️ B 版进程异常退出 rc={rc_b}，结果不可信", flush=True)
    finally:
        for p, bak in backups:
            shutil.copy(bak, p)
            os.remove(bak)

    new = sorted(A - B)
    fixed = sorted(B - A)
    both = sorted(A & B)
    print(f"\nA(工作区) 失败 {len(A)} | B(HEAD) 失败 {len(B)}")
    print(f"\n★ NEW FAIL（本次引入，必须修）: {len(new)}")
    for t in new:
        print("   ", t)
    print(f"\n✓ FIXED（本次修好）: {len(fixed)}")
    for t in fixed:
        print("   ", t)
    print(f"\n· 两版均失败（预存噪声，忽略）: {len(both)}")
    for t in both[:30]:
        print("   ", t)
    if len(both) > 30:
        print(f"    ... 另 {len(both)-30} 项")


main()

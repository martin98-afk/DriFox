# -*- coding: utf-8 -*-
"""`_token_slot_spans` O(N^2)→O(N) 重构的等价性验证（一次性）。

做法：从 ``git show HEAD`` 取出改动前的函数源码，在独立命名空间里 exec 成
可调用对象，与当前实现逐点对比输出。纯性能重构不允许有任何数值差异。

跑法：``python tests/debug/verify_token_slot_spans_equiv.py``

⚠️ 一次性脚本：对照版本取自 ``git show HEAD``，所以**本次改动提交后**再跑就
变成新旧自比对（恒等，无意义）。要在下次重构该函数时复用它，先把 ``_FILE``
的取值改成带父提交的 rev（如 ``HEAD^:plugins/...``）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import typing

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_FILE = "plugins/agent_trace/ui/timeline_panel.py"


def _load_old():
    """从 HEAD 版本抽出 `_token_slot_spans` 函数体，exec 成可调用对象。"""
    src = subprocess.check_output(
        ["git", "show", f"HEAD:{_FILE}"], cwd=_ROOT, text=True, encoding="utf-8"
    )
    start = src.index("def _token_slot_spans(")
    end = src.index("def _tok_label(")
    body = src[start:end]
    ns = {
        "_MIN_BAR_PX": 5.0,
        "List": typing.List,
        "Dict": typing.Dict,
        "Tuple": typing.Tuple,
        "float": float,
        "int": int,
        "set": set,
        "sum": sum,
        "len": len,
        "range": range,
        "enumerate": enumerate,
        "max": max,
        "min": min,
    }
    exec(compile(body, "<old>", "exec"), ns)  # noqa: S102 — 一次性验证脚本
    return ns["_token_slot_spans"]


def _load_new():
    import importlib
    import importlib.util

    ui = os.path.join(_ROOT, "plugins", "agent_trace", "ui")
    for p in (ui, _ROOT):
        if p not in sys.path:
            sys.path.insert(0, p)
    name = "equiv_tl_pkg"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(ui, "__init__.py"), submodule_search_locations=[ui]
        )
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
    return importlib.import_module(f"{name}.timeline_panel")._token_slot_spans


def _cases():
    """覆盖：常规分布 / 极端偏斜 / 全零 / 条目过多退化 / 缝隙开关边界。"""
    out = []
    # 常规：均匀
    out.append(("均匀-200", list(range(200)), [100] * 200, 1600.0))
    # 常规：随机
    import random

    rnd = random.Random(20260918)
    vals = [rnd.randint(1, 9000) for _ in range(500)]
    out.append(("随机-500", list(range(500)), vals, 1600.0))
    # 极端偏斜：一条巨值 + 一堆碎值（water-filling 上限触发）
    vals = [100000] + [1] * 300
    out.append(("偏斜-301", list(range(301)), vals, 1600.0))
    # 全零权重（退化均分）
    out.append(("全零-120", list(range(120)), [0] * 120, 1600.0))
    # 条目过多：n*(min_px+gap) > lane_w → gap=0 且退化为等分
    out.append(("过密-800", list(range(800)), [50] * 800, 1600.0))
    # 缝隙边界：刚好卡在 avail 附近
    out.append(("边界-266", list(range(266)), [100] * 266, 1600.0))
    # 空 / 极小轨道
    out.append(("空", [], [], 1600.0))
    out.append(("窄轨-40", list(range(50)), [10] * 50, 40.0))
    # 负值权重（防御：max(0, v) 分支）
    out.append(("负值-60", list(range(60)), [-5, 100] * 30, 1600.0))
    return out


def main() -> int:
    old = _load_old()
    new = _load_new()
    bad = 0
    for name, order, vals, lane_w in _cases():
        a = old(order, vals, lane_w)
        b = new(order, vals, lane_w)
        if a == b:
            print(f"  OK    {name}  ({len(order)} 条, lane_w={lane_w})")
            continue
        bad += 1
        print(f"  DIFF  {name}  ({len(order)} 条, lane_w={lane_w})")
        keys = set(a) | set(b)
        shown = 0
        for k in sorted(keys):
            if a.get(k) != b.get(k):
                print(f"        idx={k}  old={a.get(k)}  new={b.get(k)}")
                shown += 1
                if shown >= 5:
                    break
    print(f"\n{'全部等价' if bad == 0 else f'{bad} 个用例不一致'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

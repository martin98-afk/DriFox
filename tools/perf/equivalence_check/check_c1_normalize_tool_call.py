# -*- coding: utf-8 -*-
"""对拍脚本：候选 1 — normalize_tool_call（等价验证）

reference = 改前实现（2026-11-14 从 app/core/conversation/message_content.py:697
逐字复制，含 orjson as json 的序列化语义）。
被测 = 当前产品实现（真导入）。

优化方向（落地后由本脚本终验）：dict 入参时消除 dumps→loads→dumps 三连中
冗余的 loads+dumps（预期 json.dumps 调用计数 2→1、json.loads 计数 1→0）。

用法：.venv/Scripts/python.exe tools/perf/equivalence_check/check_c1_normalize_tool_call.py
退出码：0=PASS 1=FAIL 2=SKIP（被测导入失败）
"""
import os
import random
import sys
import time
import traceback
from typing import Any, Dict, Optional

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import orjson as json  # noqa: E402  与产品模块同序列化语义（bytes + UTF-8 原文）


# ── reference：改前实现（逐字复制自 message_content.py:697-736）──────────────
def reference_normalize_tool_call(tool_call: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(tool_call, dict):
        return None

    function = tool_call.get("function", {}) or {}
    function_name = str(function.get("name", "") or "").strip()
    function_arguments = function.get("arguments", "{}")
    if isinstance(function_arguments, dict):
        function_arguments = json.dumps(function_arguments).decode("utf-8")
    else:
        function_arguments = str(function_arguments or "{}")

    try:
        parsed_arguments = json.loads(function_arguments)
    except Exception:
        parsed_arguments = {}

    if not isinstance(parsed_arguments, dict):
        parsed_arguments = {}

    normalized = {
        "id": str(tool_call.get("id", "") or ""),
        "type": str(tool_call.get("type", "function") or "function"),
        "function": {
            "name": function_name,
            "arguments": json.dumps(parsed_arguments).decode("utf-8"),
        },
    }
    # 透传 Gemini thought_signature（历史存档/加载也走 normalize_message，必须保留）
    sig = tool_call.get("thought_signature")
    if sig:
        normalized["thought_signature"] = sig
    return normalized


# ── 被测：当前产品实现 ──────────────────────────────────────────────────────
def load_current():
    from app.core.conversation.message_content import normalize_tool_call

    return normalize_tool_call


# ── fuzz 语料生成（覆盖 #10 用例 1 边界）────────────────────────────────────
_NAME_POOL = ["read", "write", "edit", "bash", "question", "websearch", "glob"]
_PATH_POOL = [
    "D:/work/a.py",
    "C:/Users/黑 箱/config.json",
    "相对/路径/文件.md",
    "E:\\反斜杠\\path.txt",
    "",
    "a" * 300,
]
_UNSERIALIZABLE = [set(), {1, 2}, object()]


def _rand_value(rng: random.Random, depth: int) -> Any:
    pick = rng.randrange(8 if depth <= 0 else 10)
    if pick == 0:
        return rng.choice(_PATH_POOL)
    if pick == 1:
        return rng.randrange(-100, 10000)
    if pick == 2:
        return round(rng.uniform(-1.5, 999.99), 4)
    if pick == 3:
        return rng.choice([True, False])
    if pick == 4:
        return None
    if pick == 5:
        return ["item", 2, 3.5, True, None][: rng.randrange(1, 6)]
    if pick == 6:
        return {"嵌套键": "中文值", "n": rng.randrange(100)}
    return {"inner": _rand_value(rng, depth - 1), "列表": [1, "二", 3.0]}


def _rand_arguments(rng: random.Random) -> Any:
    kind = rng.randrange(10)
    if kind <= 3:  # dict 直给（#7 触发形态）
        n = rng.randrange(0, 6)
        return {f"key{i}": _rand_value(rng, 2) for i in range(n)}
    if kind == 4:  # 合法 JSON str，带装饰空白
        obj = {f"k{i}": _rand_value(rng, 1) for i in range(rng.randrange(1, 4))}
        raw = json.dumps(obj).decode("utf-8")
        return "  " + raw.replace(",", " , ").replace(":", " : ") + "\n"
    if kind == 5:  # 非法 JSON 串
        return rng.choice(
            ["{path: 'a.py'}", "not json at all", "", "   ", '{"a": 1,}', "[1, 2, 3"]
        )
    if kind == 6:
        return None
    if kind == 7:
        return rng.choice([123, 4.5, True])
    if kind == 8:  # list（str(list) 后 loads 失败 → {}）
        return ["x", 1, 2]
    # dict 含不可序列化值：orjson 对 set/object 抛 TypeError（双方须同抛）
    return {"bad": rng.choice(_UNSERIALIZABLE), "ok": 1}


def _rand_tool_call(rng: random.Random) -> Any:
    outer = rng.randrange(12)
    if outer == 0:
        return rng.choice(["not-a-dict", 42, None, ["list"], ""])
    tc: Dict[str, Any] = {
        "function": {
            "name": rng.choice(_NAME_POOL)
            if rng.random() > 0.1
            else rng.choice(["", None, "  空格名 "]),
            "arguments": _rand_arguments(rng),
        }
    }
    if rng.random() > 0.2:
        tc["id"] = f"call_{rng.getrandbits(48):012x}"
    else:
        tc["id"] = rng.choice(["", None])
    if rng.random() > 0.3:
        tc["type"] = "function"
    if rng.random() > 0.7:
        tc["thought_signature"] = rng.choice(["sig-中文", "x" * 64, 12345])
    if rng.random() > 0.9:
        tc["function"] = None  # → {} or {} 分支
    return tc


# ── 对拍 ────────────────────────────────────────────────────────────────────
def _run_one(fn, tc):
    """返回 (ok, result) / (False, 异常类型:消息)——异常也参与对拍（双方须同抛同类）"""
    try:
        return True, fn(tc)
    except Exception as e:
        return False, f"{type(e).__name__}:{e}"


def main() -> int:
    print("=" * 64)
    print("[对拍] 候选 1 — normalize_tool_call")
    print("reference: app/core/conversation/message_content.py:697 (改前实现内嵌)")
    try:
        current = load_current()
    except Exception:
        print("[SKIP] 被测导入失败，不判定：")
        traceback.print_exc()
        return 2
    print("被测    : app.core.conversation.message_content.normalize_tool_call (真导入)")

    rng = random.Random(20261114)
    total, passed = 1000, 0
    t0 = time.perf_counter()
    failures = []
    for i in range(total):
        tc = _rand_tool_call(rng)
        ok_ref, out_ref = _run_one(reference_normalize_tool_call, tc)
        ok_cur, out_cur = _run_one(current, tc)
        if (ok_ref, out_ref) == (ok_cur, out_cur):
            passed += 1
        else:
            if len(failures) < 5:
                failures.append((i, tc, (ok_ref, out_ref), (ok_cur, out_cur)))
    elapsed = time.perf_counter() - t0

    # 差异化断言（[INFO] 基线，不参与 PASS 判定）：dict 入参下 json 调用计数
    real_dumps, real_loads = json.dumps, json.loads
    counts = {"dumps": 0, "loads": 0}

    def spy_dumps(*a, **kw):
        counts["dumps"] += 1
        return real_dumps(*a, **kw)

    def spy_loads(*a, **kw):
        counts["loads"] += 1
        return real_loads(*a, **kw)

    json.dumps, json.loads = spy_dumps, spy_loads
    try:
        reference_normalize_tool_call({"function": {"name": "x", "arguments": {"a": 1}}})
        ref_counts = dict(counts)
        counts["dumps"], counts["loads"] = 0, 0
        current({"function": {"name": "x", "arguments": {"a": 1}}})
        cur_counts = dict(counts)
    finally:
        json.dumps, json.loads = real_dumps, real_loads
    print(
        f"[INFO] dict 入参 json 调用计数基线：reference dumps={ref_counts['dumps']} loads={ref_counts['loads']}"
        f" | 被测(当前=改前) dumps={cur_counts['dumps']} loads={cur_counts['loads']}"
        f"（修复落地后预期被测 dumps=1 loads=0）"
    )

    print(f"语料    : {total} 组（dict 直给/str 合法/非法/None/标量/list/不可序列化 set/thought_signature 透传）")
    print(f"通过    : {passed}/{total}")
    print(f"对拍耗时: {elapsed:.3f} s")
    if failures:
        print("反例（前 5）:")
        for i, tc, r, c in failures:
            print(f"  #{i}: ref={r!r} cur={c!r} input={str(tc)[:200]}")
    status = "PASS" if passed == total else "FAIL"
    print(f"结果    : {status}")
    print("=" * 64)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""对拍脚本：候选 6 — _compute_tool_call_signature（等价验证）

reference = 改前实现（2026-11-14 从 app/core/workers/chat_worker.py:1843-1868
逐字复制，注意函数体内 `import json` 是标准库（遮蔽模块级 orjson as json），
reference 保留同样结构以维持序列化语义）。被测 = 当前产品实现（真导入）。

优化方向（落地后由本脚本终验）：函数内 import 提升到模块级 + args 原串
规范化 memo（纯函数确定性，同 args 重复签名计算 json.loads 计数 100→1）。

用法：.venv/Scripts/python.exe tools/perf/equivalence_check/check_c6_tool_signature.py
退出码：0=PASS 1=FAIL 2=SKIP（被测导入失败）
"""
import random
import sys
import time
import traceback
from typing import Dict, List

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── reference：改前实现（逐字复制自 chat_worker.py:1843-1868）────────────────
def reference_compute_tool_call_signature(tool_calls: List[Dict]) -> str:
    import hashlib
    import re

    def _normalize_json_whitespace(s: str) -> str:
        # 去掉所有空白（包含换行）后重新插入：
        # 1. JSON token 周围不留空格
        # 2. 字符串 value 内部保留原始字符（如 "hello world"）
        # 简化版：把所有空白压成单空格，再去掉 `,` `:` `{` `}` `[` `]` 前后的空格
        # 用状态机判断是否在字符串内部太复杂，这里采用保守策略：
        # 先尝试用 json 解析，解析成功则重 dump 规范化；失败则退到"压缩连续空白"。
        try:
            import json

            obj = json.loads(s)
            return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            # 退化：去掉所有空白（包括换行/制表）
            return re.sub(r"\s+", "", s)

    parts = []
    for tc in tool_calls:
        func = tc.get("function") or {}
        name = (func.get("name") or "").strip()
        args = (func.get("arguments") or "").strip()
        args = _normalize_json_whitespace(args)
        parts.append(f"{name}|{args}")
    parts.sort()
    canonical = "\n".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── 被测：当前产品实现 ──────────────────────────────────────────────────────
def load_current():
    from app.core.workers.chat_worker import OpenAIChatWorker

    return OpenAIChatWorker._compute_tool_call_signature


# ── fuzz 语料生成（覆盖 #10 用例 6 边界）────────────────────────────────────
_NAME_POOL = ["read", "write", "edit", "bash", "subagent_para", "websearch"]
_JSON_BODIES = [
    '{"path": "D:/a.py",  "line" : 42}',
    '{  "query" : "中文 搜索 词", "limit": 10 }',
    '{"nested": {"deep": [1, 2, {"x": "y"}]}, "flag": true}',
    '{"emoji": "✅→①", "blank": "a  b"}',
    "[1, 2, 3]",
    '"bare string"',
    "42",
    "true",
]
_NON_JSON = ["path = a.py", "{path: 'a.py'}", "", "   ", "\n\n\t  ", "中文没有json", '{"a": 1,}']
_BIG_JSON_TMPL = '{"items": [%s], "meta": {"count": %d}}'


def _rand_args(rng: random.Random, big: bool = False) -> str:
    if big:
        items = ", ".join(f'{{"id": {i}, "path": "file_{i}.py", "lines": "1-{rng.randrange(10, 500)}"}}' for i in range(150))
        return _BIG_JSON_TMPL % (items, 150)
    kind = rng.randrange(10)
    if kind <= 5:
        body = rng.choice(_JSON_BODIES)
        if rng.random() > 0.5:  # 换行/制表装饰
            body = body.replace(",", ",\n\t").replace(":", " : ")
        return body
    return rng.choice(_NON_JSON)


def _rand_tool_calls(rng: random.Random, big: bool = False) -> List[Dict]:
    n = rng.randrange(0, 5)
    out = []
    for _ in range(n):
        tc = {
            "function": {
                "name": rng.choice(_NAME_POOL) if rng.random() > 0.1 else "",
                "arguments": _rand_args(rng, big),
            }
        }
        if rng.random() > 0.9:
            tc["function"] = None  # → {} or {} 分支
        out.append(tc)
    return out


def _run_one(fn, tcs):
    try:
        return True, fn(tcs)
    except Exception as e:
        return False, f"{type(e).__name__}:{e}"


def main() -> int:
    print("=" * 64)
    print("[对拍] 候选 6 — OpenAIChatWorker._compute_tool_call_signature")
    print("reference: app/core/workers/chat_worker.py:1843 (改前实现内嵌)")
    try:
        current = load_current()
    except Exception:
        print("[SKIP] 被测导入失败，不判定：")
        traceback.print_exc()
        return 2
    print("被测    : app.core.workers.chat_worker.OpenAIChatWorker._compute_tool_call_signature (真导入)")

    rng = random.Random(20261114)
    total, passed = 1000, 0
    t0 = time.perf_counter()
    failures = []
    for i in range(total):
        big = i % 50 == 0  # 每 50 组掺一组 10KB 级大 JSON
        tcs = _rand_tool_calls(rng, big)
        shuffled = list(tcs)
        rng.shuffle(shuffled)  # 排序稳定性：乱序输入须同签名
        r_ref = _run_one(reference_compute_tool_call_signature, tcs)
        r_cur = _run_one(current, tcs)
        r_ref_s = _run_one(reference_compute_tool_call_signature, shuffled)
        r_cur_s = _run_one(current, shuffled)
        if r_ref == r_cur == r_ref_s == r_cur_s:
            passed += 1
        else:
            if len(failures) < 5:
                failures.append((i, big, tcs, r_ref, r_cur, r_ref_s, r_cur_s))
    elapsed = time.perf_counter() - t0

    # [INFO] 差异化基线（不参与 PASS 判定）：同大 args 重复计算 100 次
    big_args = _rand_args(rng, big=True)
    tcs_big = [{"function": {"name": "read", "arguments": big_args}}]
    current(tcs_big)  # 预热（若已落 memo 则填充缓存）
    t1 = time.perf_counter()
    for _ in range(100):
        current(tcs_big)
    cur_ms = (time.perf_counter() - t1) / 100 * 1000
    print(
        f"[INFO] 同 10KB args 重复签名 100 次平均 {cur_ms:.3f} ms/次"
        f"（落地 memo 后重复调用应接近 0；当前=改前基线）"
    )

    print(f"语料    : {total} 组（装饰空白 JSON/中文/嵌套/非 JSON 退化/空列表/None function/乱序排序稳定性/20 组 10KB 大 JSON）")
    print(f"通过    : {passed}/{total}")
    print(f"对拍耗时: {elapsed:.3f} s")
    if failures:
        print("反例（前 5）:")
        for i, big, tcs, a, b, c, d in failures:
            print(f"  #{i} big={big}: ref={a!r} cur={b!r} ref_shuf={c!r} cur_shuf={d!r}")
    status = "PASS" if passed == total else "FAIL"
    print(f"结果    : {status}")
    print("=" * 64)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

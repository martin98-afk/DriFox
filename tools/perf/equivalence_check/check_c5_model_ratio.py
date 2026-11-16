# -*- coding: utf-8 -*-
"""对拍脚本：候选 5 — _get_model_token_ratio（等价验证）

reference = 改前实现（2026-11-14 从 app/core/infra/token_estimator.py:109-115
逐字复制，含 _MODEL_TOKEN_RATIOS 依赖常量）。被测 = 当前产品实现（真导入）。

优化方向（落地后由本脚本终验）：@lru_cache 包 model 参数（消除每次调用的
11 项子串线性扫描 + model.lower()）。

用法：.venv/Scripts/python.exe tools/perf/equivalence_check/check_c5_model_ratio.py
退出码：0=PASS 1=FAIL 2=SKIP（被测导入失败）
"""
import random
import sys
import time
import traceback

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── reference 依赖常量（逐字复制自 token_estimator.py）───────────────────────
_MODEL_TOKEN_RATIOS = {
    "minimax": 1.00,
    "qwen": 1.00,
    "deepseek": 1.00,
    "kimi": 1.00,
    "glm": 1.00,
    "claude": 1.00,
    "gemini": 1.00,
    "gpt-4": 1.00,
    "gpt-3.5": 1.00,
    "gpt-3": 1.00,
    "default": 1.00,
}


# ── reference：改前实现（逐字复制自 token_estimator.py:109-115）──────────────
def reference_get_model_token_ratio(model: str) -> float:
    """获取模型 token 校正系数"""
    model_lower = model.lower()
    for key, ratio in _MODEL_TOKEN_RATIOS.items():
        if key in model_lower:
            return ratio
    return _MODEL_TOKEN_RATIOS["default"]


# ── 被测：当前产品实现 ──────────────────────────────────────────────────────
def load_current():
    from app.core.infra.token_estimator import _get_model_token_ratio

    return _get_model_token_ratio


# ── fuzz 语料生成（覆盖 #10 用例 5 边界）────────────────────────────────────
_PREFIXES = [
    "gpt-4", "gpt-3.5", "gpt-3", "claude", "gemini", "deepseek",
    "qwen", "kimi", "glm", "minimax", "o1", "llama",
]
_SUFFIXES = ["", "-20241120", "-latest", ":free", "-preview", "-0828", "@openai", "/beta"]
_WRAPPERS = ["", "openai/", "opencode/", "accounts/"]


def _rand_model(rng: random.Random) -> str:
    kind = rng.randrange(12)
    if kind == 0:
        return ""  # 空串
    if kind == 1:  # 大小写变换
        return rng.choice(_PREFIXES).upper() + rng.choice(_SUFFIXES)
    if kind == 2:  # 多 key 子串（dict 顺序首个命中语义：gpt-3.5 先于 gpt-4）
        return rng.choice(["gpt-3.5-turbo-gpt-4", "gpt-4-gpt-3.5", "claude-gpt-4-kimi"])
    if kind == 3:  # 前缀包裹
        return rng.choice(_WRAPPERS) + rng.choice(_PREFIXES) + rng.choice(_SUFFIXES)
    # 常规拼接
    return rng.choice(_WRAPPERS) + rng.choice(_PREFIXES) + rng.choice(_SUFFIXES) + rng.choice(["", "-beta"])


def main() -> int:
    print("=" * 64)
    print("[对拍] 候选 5 — _get_model_token_ratio")
    print("reference: app/core/infra/token_estimator.py:109 (改前实现内嵌)")
    try:
        current = load_current()
    except Exception:
        print("[SKIP] 被测导入失败，不判定：")
        traceback.print_exc()
        return 2
    print("被测    : app.core.infra.token_estimator._get_model_token_ratio (真导入)")

    rng = random.Random(20261114)
    total, passed = 500, 0
    t0 = time.perf_counter()
    failures = []
    for i in range(total):
        model = _rand_model(rng)
        try:
            ref = reference_get_model_token_ratio(model)
            ok_ref, res_ref = True, ref
        except Exception as e:
            ok_ref, res_ref = False, f"{type(e).__name__}:{e}"
        try:
            cur = current(model)
            ok_cur, res_cur = True, cur
        except Exception as e:
            ok_cur, res_cur = False, f"{type(e).__name__}:{e}"
        if (ok_ref, res_ref) == (ok_cur, res_cur):
            passed += 1
        else:
            if len(failures) < 5:
                failures.append((i, model, res_ref, res_cur))
    elapsed = time.perf_counter() - t0

    # [INFO] 差异化基线（不参与 PASS 判定）：同 model 重复调用计数/cache 命中
    cache_info = getattr(current, "cache_info", None)
    if cache_info is not None:
        model = "deepseek-chat"
        for _ in range(100):
            current(model)
        info = cache_info()
        print(f"[INFO] 重复调用 100 次 cache：hits={info.hits} misses={info.misses}"
              f"（落地 lru_cache 后预期 hits>=99；当前未缓存 hits=0 属改前基线）")
    else:
        for _ in range(100):
            current("deepseek-chat")
        print("[INFO] 被测当前无 lru_cache（=改前基线）；落地后本脚本将自动打印 hits 统计")

    print(f"语料    : {total} 组（空串/大写/多 key 子串命中序/provider 包裹/日期后缀）")
    print(f"通过    : {passed}/{total}")
    print(f"对拍耗时: {elapsed:.3f} s")
    if failures:
        print("反例（前 5）:")
        for i, model, r, c in failures:
            print(f"  #{i}: ref={r!r} cur={c!r} model={model!r}")
    status = "PASS" if passed == total else "FAIL"
    print(f"结果    : {status}")
    print("=" * 64)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

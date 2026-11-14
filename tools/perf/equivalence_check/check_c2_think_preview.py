# -*- coding: utf-8 -*-
"""对拍脚本：候选 2 — _get_think_preview（等价验证）

reference = 改前实现（2026-11-14 从 app/widgets/card_render_core.py:1165-1259
逐字复制，含 _CONCLUSION_MARKERS/_PREVIEW_TARGET_LEN/_PREVIEW_MIN_CONCLUSION_BODY
依赖常量）。被测 = 当前产品实现（真导入，QT_QPA_PLATFORM=offscreen）。

优化方向（落地后由本脚本终验）：CJK 统计换正则 len(findall)、中文切句
逐字符拼接换正则切分（O(n²)→O(n)）。PASS 判据仅=输出逐字符相等；
性能对比为 [INFO] 基线（当前两实现相同，落地后才产生差异）。

用法：.venv/Scripts/python.exe tools/perf/equivalence_check/check_c2_think_preview.py
退出码：0=PASS 1=FAIL 2=SKIP（被测导入失败）
"""
import os
import random
import re
import sys
import time
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── reference 依赖常量（逐字复制自 card_render_core.py）─────────────────────
_PREVIEW_TARGET_LEN: int = 60
_PREVIEW_MIN_CONCLUSION_BODY: int = 8

# 结论标记（中英文，优先级最高）
_CONCLUSION_MARKERS = (
    "因此",
    "所以",
    "综上",
    "综上所述",
    "总而言之",
    "总的来说",
    "建议",
    "推荐",
    "结论是",
    "答案是",
    "总结一下",
    "也就是说",
    "最终",
    "therefore",
    "in conclusion",
    "overall",
    "the answer is",
    "the solution is",
    "i recommend",
    "i suggest",
    "so the answer",
)


# ── reference：改前实现（逐字复制自 card_render_core.py:1165-1259）───────────
def reference_get_think_preview(content: str, max_length: int = 160) -> str:
    if not content:
        return ""

    text = content.strip()
    if not text:
        return ""

    # 预览单行显示（CSS nowrap），换行塌成空格
    flat = text.replace("\n", " ")
    norm_len = len(flat.replace(" ", ""))

    def _is_full(preview_len: int) -> bool:
        """预览是否已覆盖完整内容（忽略空白、换行差异）"""
        return preview_len >= norm_len

    def _finish(preview: str) -> str:
        """收尾：超长则从句边界回退截断，未覆盖全文则补省略号"""
        if len(preview) > max_length:
            for i in range(max_length, 0, -1):
                if preview[i - 1] in "。！？.!?；;":
                    preview = preview[:i]
                    break
            else:
                preview = preview[:max_length]
        return preview if _is_full(len(preview)) else preview + "..."

    # ── 策略1: 结论优先（按文中最早出现的标记，而非 markers 元组顺序）──
    best_idx = -1
    best_marker = ""
    for marker in _CONCLUSION_MARKERS:
        idx = flat.find(marker)
        if idx != -1 and (best_idx == -1 or idx < best_idx):
            best_idx, best_marker = idx, marker
    if best_idx != -1:
        conclusion = flat[best_idx:].strip()
        # 标记后必须有实质内容才认（排除「…最终。」这类紧贴文末的退化命中）
        if len(conclusion) - len(best_marker) >= _PREVIEW_MIN_CONCLUSION_BODY:
            return _finish(conclusion)

    # ── 策略2: 从开头取连续句 ──
    # 中文按句末标点切；英文为主时按「标点 + 空格 + 大写」切，避免 1. / U.S. / v2.5
    # 被误判成句子边界
    cjk_count = sum(1 for c in flat if "\u4e00" <= c <= "\u9fff")
    is_english_heavy = cjk_count < len(flat) * 0.3
    if is_english_heavy:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", flat) if s.strip()]
    else:
        sentences = []
        current = ""
        for ch in flat:
            current += ch
            if ch in "。！？.!?；;":
                if current.strip():
                    sentences.append(current.strip())
                current = ""
        if current.strip():
            sentences.append(current.strip())

    if not sentences:
        # 无句边界（纯短语 / 列表式思考）：直接按字符截断
        if len(flat) <= max_length:
            return flat
        for i in range(max_length, 0, -1):
            if flat[i - 1] in " ，,、；;：:.":
                return flat[:i].rstrip(" ，,、.") + "..."
        return flat[:max_length] + "..."

    preview = ""
    for s in sentences:
        # 英文句子的分隔符（空格）已被 strip 吃掉，拼回来否则 "a.B" 粘连
        preview += (s if not preview else (s if not is_english_heavy else " " + s))
        if len(preview) >= _PREVIEW_TARGET_LEN or len(preview) >= max_length:
            break

    return _finish(preview)


# ── 被测：当前产品实现 ──────────────────────────────────────────────────────
def load_current():
    from app.widgets.card_render_core import _get_think_preview

    return _get_think_preview


# ── fuzz 语料生成（覆盖 #10 用例 2 边界）────────────────────────────────────
_CJK_CHARS = "在分析这个问题的时候需要先理解用户的核心诉求然后逐步拆解成可执行的步骤最终得到一个合理的方案这是一个典型的多阶段推理过程"
_EN_WORDS = ["the", "quick", "brown", "fox", "jumps", "over", "lazy", "dog", "Analysis", "Result", "Step"]
_CN_PUNCT = "。！？；;，,、：:"
_EN_PUNCT = ".!?;"
_MARKERS = ["最终", "所以", "综上", "因此", "总结一下", "最终", "therefore", "in conclusion"]
_EMOJI = ["✅", "🤔", "→", "①", "»"]


def _rand_text(rng: random.Random) -> str:
    kind = rng.randrange(10)
    length = rng.randrange(0, 600)
    if kind == 0:  # 空串 / 纯空白
        return rng.choice(["", " ", "  \n  ", "\n\n"])
    if kind <= 4:  # 中文为主：句末标点分隔
        parts = []
        for _ in range(rng.randrange(1, 25)):
            seg = "".join(rng.choice(_CJK_CHARS) for _ in range(rng.randrange(1, 40)))
            if rng.random() > 0.85:
                seg = rng.choice(_MARKERS) + seg
            parts.append(seg + rng.choice(_CN_PUNCT))
        text = "".join(parts)
        if rng.random() > 0.8:
            text = text.replace("。", "。\n", 1)
        return text
    if kind <= 7:  # 英文为主：「标点+空格+大写」句切
        parts = []
        for _ in range(rng.randrange(1, 25)):
            seg = " ".join(rng.choice(_EN_WORDS) for _ in range(rng.randrange(1, 12)))
            parts.append(seg + rng.choice(_EN_PUNCT) + " ")
        text = "".join(parts)
        if rng.random() > 0.7:
            text += rng.choice(_MARKERS) + " " + " ".join(rng.choice(_EN_WORDS) for _ in range(6))
        return text
    if kind == 8:  # 混排 + emoji + 恰 30% cjk 边界区
        base = "".join(rng.choice(_EN_WORDS + list(_EN_PUNCT) + [" "]) for _ in range(length))
        n_cjk = int(len(base) * rng.uniform(0.0, 0.5))
        inject = "".join(rng.choice(_CJK_CHARS) for _ in range(n_cjk))
        pos = rng.randrange(0, max(1, len(base) - 1))
        return base[:pos] + inject + rng.choice(_EMOJI) + base[pos:]
    # 无句边界超长（纯截断路径）/ 纯标点 / 标记紧贴文末
    sub = rng.randrange(3)
    if sub == 0:
        return "".join(rng.choice(_CJK_CHARS) for _ in range(rng.randrange(200, 2000)))
    if sub == 1:
        return "。！？.!?；;" * rng.randrange(1, 80)
    return "分析了很久" + rng.choice(_MARKERS)  # 标记紧贴文末（退化命中）


def main() -> int:
    print("=" * 64)
    print("[对拍] 候选 2 — _get_think_preview")
    print("reference: app/widgets/card_render_core.py:1165 (改前实现内嵌)")
    try:
        current = load_current()
    except Exception:
        print("[SKIP] 被测导入失败，不判定：")
        traceback.print_exc()
        return 2
    print("被测    : app.widgets.card_render_core._get_think_preview (真导入, offscreen)")

    rng = random.Random(20261114)
    total, passed = 1000, 0
    max_lengths = [40, 80, 160, 300]
    t0 = time.perf_counter()
    failures = []
    for i in range(total):
        text = _rand_text(rng)
        ml = rng.choice(max_lengths)
        try:
            out_ref = reference_get_think_preview(text, ml)
            ok_ref, res_ref = True, out_ref
        except Exception as e:
            ok_ref, res_ref = False, f"{type(e).__name__}:{e}"
        try:
            out_cur = current(text, ml)
            ok_cur, res_cur = True, out_cur
        except Exception as e:
            ok_cur, res_cur = False, f"{type(e).__name__}:{e}"
        if (ok_ref, res_ref) == (ok_cur, res_cur):
            passed += 1
        else:
            if len(failures) < 5:
                failures.append((i, ml, text, res_ref, res_cur))
    elapsed = time.perf_counter() - t0

    # 默认参数一致性（max_length 缺省 160 路径）
    default_ok = all(
        reference_get_think_preview(t) == current(t)
        for t in ["最终答案是", "第一句。第二句。" * 30, "hello World. next Sentence."]
    )

    # [INFO] 性能基线（不参与 PASS 判定）：1 万字无标点中文（O(n²) 最坏路径）
    long_text = "思" * 10000
    t1 = time.perf_counter()
    for _ in range(3):
        reference_get_think_preview(long_text)
    ref_ms = (time.perf_counter() - t1) / 3 * 1000
    t2 = time.perf_counter()
    for _ in range(3):
        current(long_text)
    cur_ms = (time.perf_counter() - t2) / 3 * 1000
    print(
        f"[INFO] 1 万字无标点耗时基线：reference={ref_ms:.2f}ms 被测={cur_ms:.2f}ms"
        f"（当前两实现相同；落地正则切分后预期被测显著下降）"
    )

    print(f"语料    : {total} 组（中文句切/英文句切/混排+emoji/恰30%cjk/无边界纯截断/纯标点/标记紧贴文末 × 4 档 max_length）")
    print(f"通过    : {passed}/{total}（默认参数抽查 {'OK' if default_ok else 'FAIL'}）")
    print(f"对拍耗时: {elapsed:.3f} s")
    if failures:
        print("反例（前 5）:")
        for i, ml, text, r, c in failures:
            print(f"  #{i} max_length={ml}: ref={r!r} cur={c!r} input={text[:120]!r}")
    status = "PASS" if passed == total and default_ok else "FAIL"
    print(f"结果    : {status}")
    print("=" * 64)
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

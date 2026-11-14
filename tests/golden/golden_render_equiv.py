# -*- coding: utf-8 -*-
"""渲染管线 golden 采集与行为等价比对（子任务 #17，两段式）。

用途（对应重构候选 #5 清单 1/2/3/6/7/8 的等价验证，方案见子任务 #12）：
    collect  : 重放全部场景，渲染产物落盘 golden JSON（正式基线，等统一信号执行）
    collect --dry-run : 全场景跑通但**不写任何文件**（自测 / 冒烟）
    compare  : 重放全部场景，与已有 golden 逐场景比对，产出 verdict + 降档清单

等价口径（leader 裁决①，硬约束）：
    1. final HTML **逐字节相等**为第一优先（verdict=exact，判据 sha256+长度）；
    2. 若拼接点空白差异不可避免，允许「空白归一化后比对」兜底（verdict=normalized）。
       实现口径：非 pre 区连续空白折叠单空格；<pre> 区**不参与折叠**、
       以 pre 块逐字节 sha 单独比对（空白有语义）。
       判据：采集时对每帧产物同时落 sha256 / 归一化 sha / pre 块 sha，
       compare 仅比三级哈希，不做全文复现（避免「改造后代码复现 golden 侧全文」的
       伪等价——复现出的是改造后产物，两侧必然相同，属断言失效）。
    3. normalized 场景逐条写入降档清单（DEGRADED，附双侧 head/len 供定位），
       汇总阶段供 leader 审计；
    4. 禁止再放宽：DOM 语义等价不可作为断言口径。

纯 Python 离屏可跑（无 QApplication、无 QWebEngineView 实例化）：
    - app.widgets.card_render_core._render_markdown_to_html_worker(snapshot)
      线程池 worker，纯 CPU，生产异步渲染的实际路径（候选 6 流式分支改动面）
    - CodeWebViewer._render_markdown_to_html(stub_host, raw_md)
      主线程入口（候选 1 主题检测链 + 候选 6 主线程分支改动面）；
      stub 只提供实例字段，绝不实例化 QWebEngineView（offscreen 实例化必崩，
      见 tests/perf/test_stream_render_bench.py 头注）
两份实现共享同一管线语义，golden 双通道并采。

运行（必须项目 venv）：
    uv run python tests/golden/golden_render_equiv.py collect --dry-run   # 自测
    uv run python tests/golden/golden_render_equiv.py collect             # 正式落盘（等信号）
    uv run python tests/golden/golden_render_equiv.py compare             # 改造后比对
    uv run python tests/golden/golden_render_equiv.py compare --baseline <path>

输出：
    collect        → tests/golden/golden_render_baseline.json（sha256+长度+首尾 256 字符
                     + 归一化 sha + pre 块 sha）
    compare        → tests/golden/golden_compare_report.json（verdict / 降档清单 / FAIL 详情）

候选 3（hash/startswith 判等替换）专用约定：
    改造时把新判等实现以 CodeWebViewer 静态方法暴露（名字约定）：
        _equiv_cache_hit(cur_md: str, cached_hash: int | None) -> bool
        _equiv_baseline_ok(cur_md: str, last_rendered: str) -> bool
    compare 检测到即启用「新实现 vs golden(old 判定序列)」比对；
    未检测到则该段 verdict=SKIPPED_NEW_IMPL_MISSING（不影响其他段）。
    golden 只记录判定布尔序列：Python str hash 进程内随机化，数值不可跨进程比较；
    「hash(cur)==hash(prev)」的判定布尔在 prev≠cur 时恒 False（碰撞概率忽略），
    进程间可比。

场景覆盖（#12 方案⑤ 9 项 + 方案① 3 类输入×2 主题 + 方案⑥ 边界 5 项 + tail 前缀）：
    A. think_flip / chart_fence_close / tool_pending / long_table / long_code /
       think_unclosed_silent / mood_flip / baseline_reset / kb300_mixed
    B. 主题矩阵：{code_py, diff_block, plain_text} × {light, dark}（mock 注入主题态）
    C. _extract_closed_segments 边界 5 项 + _render_inline_tail 前缀序列 + think 守卫
"""

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from app.widgets import card_render_core as crc  # noqa: E402
from app.widgets import card_viewers as cv  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINE_PATH = os.path.join(HERE, "golden_render_baseline.json")
REPORT_PATH = os.path.join(HERE, "golden_compare_report.json")

CHUNK_CHARS = 40
SAMPLE_HEAD_TAIL = 256
PRE_BLOCK_RE = re.compile(r"<pre\b.*?</pre>", re.DOTALL | re.IGNORECASE)


# ────────────────────────── 哈希与记录 ──────────────────────────

def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def normalize_ws(html: str) -> str:
    """裁决①归一化口径：<pre> 区保护原样，其余连续空白折叠单空格、去首尾。"""
    parts, stubs, last = [], [], 0
    for i, m in enumerate(PRE_BLOCK_RE.finditer(html)):
        parts.append(html[last:m.start()])
        stubs.append(m.group(0))
        parts.append(f"\x00PRE{i}\x00")
        last = m.end()
    parts.append(html[last:])
    folded = re.sub(r"\s+", " ", "".join(parts)).strip()
    for i, s_ in enumerate(stubs):
        folded = folded.replace(f"\x00PRE{i}\x00", s_)
    return folded


def record(html: str) -> dict:
    pres = PRE_BLOCK_RE.findall(html)
    return {
        "sha256": sha(html),
        "nsha256": sha(normalize_ws(html)),
        "psha256": sha("\x00".join(pres)) if pres else "",
        "len": len(html),
        "head": html[:SAMPLE_HEAD_TAIL],
        "tail": html[-SAMPLE_HEAD_TAIL:],
    }


def verdict_of(grec: dict, nrec: dict, section: str, report: dict) -> str:
    """三级判定：exact → normalized（降档入清单）→ FAIL。"""
    if grec["sha256"] == nrec["sha256"] and grec["len"] == nrec["len"]:
        return "exact"
    if (
        grec["nsha256"] == nrec["nsha256"]
        and grec["psha256"] == nrec["psha256"]
        and grec.get("psha256") is not None
    ):
        report["degraded"].append({
            "section": section,
            "note": "空白归一化+pre 逐字节 均相等（裁决①兜底口径命中）",
            "golden": {"len": grec["len"], "head": grec["head"], "tail": grec["tail"]},
            "now": {"len": nrec["len"], "head": nrec["head"], "tail": nrec["tail"]},
        })
        return "normalized"
    report["failed"].append({"section": section, "golden": grec, "now": nrec})
    return "FAIL"


def git_rev() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=os.path.join(HERE, "..", ".."),
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode().strip()
    except Exception:
        return "unknown"


# ────────────────────────── 采集 host（stub）──────────────────────────

class StubHost:
    """CodeWebViewer._render_markdown_to_html 的最小宿主：只提供其读取的字段。"""

    def __init__(self, streaming=True, thinking_finalized=True,
                 compact=True, lazy_body=False, tool_fingerprints=()):
        self._streaming = streaming
        self._thinking_finalized = thinking_finalized
        self._tool_compact_mode = compact
        self._tool_lazy_body = lazy_body
        self._finished_tool_fingerprints = frozenset(tool_fingerprints)
        self._processed_md_hash = None
        self._cached_streaming_html = None
        self._cached_raw_md_hash = None

    def _refresh_viewer_font_css(self):
        pass

    # [P0-③] 主题链缓存化后渲染入口会调 self._ensure_theme_chain；
    # CodeWebViewer 在下方 import 后再绑定（见 _bind_helpers）


def _bind_helpers():
    """类体不能引用后置导入名，import 后补绑定主题链方法到 StubHost。"""
    StubHost._ensure_theme_chain = cv.CodeWebViewer._ensure_theme_chain


_bind_helpers()


def render_mainline(host, raw_md: str) -> str:
    return cv.CodeWebViewer._render_markdown_to_html(host, raw_md)


def render_worker(raw_md: str, streaming: bool, thinking_finalized: bool = True,
                  compact: bool = True, lazy_body: bool = False,
                  tool_fingerprints=()) -> str:
    snap = {
        "md": raw_md,
        "streaming": streaming,
        "thinking_finalized": thinking_finalized,
        "compact": compact,
        "pygments_style": "friendly",
        "icon_prefix": "\x00ICON\x00",  # 固定常量：等价比对只要求前后一致，不要求生产真值
        "code_font_size": 14,
        "tool_fingerprints": tuple(tool_fingerprints),
        "lazy_body": lazy_body,
    }
    return crc._render_markdown_to_html_worker(snap)


# ────────────────────────── 场景语料 ──────────────────────────

PARA = ("这是一段用于验证渲染管线行为等价的中文正文，句子以句号分隔，"
        "段落之间保留空行以触发自然边界全量渲染，用于捕捉差量与全量两条路径的产物差异。")
LIST_BLK = ("- 列表项一：块级渲染按 key 指纹命中复用\n"
            "- 列表项二：增量注入经节流合并，避免逐帧打穿渲染层\n"
            "- 列表项三：收尾全量渲染保证最终态一致")
CODE_BLK = ("```python\n"
            "def golden_check(cards: list[str], expected: str) -> bool:\n"
            "    rendered = pipeline(cards)\n"
            "    return rendered == expected  # 逐字节比对\n"
            "```\n\n")
THINK_BLK = "<think>先拆解前提：等价性指最终产物一致；中间态允许实现差异，但同输入同输出。</think>\n\n"
TOOL_SEG = ('<tool>{"name": "read_file", "arguments": {"path": "src/main.py"},'
            '"result": "ok", "success": true, "tool_call_id": "call_001"}</tool>\n\n')
MOOD_SEG = "<mood>今天心情不错，适合做等价验证。</mood>\n\n"
ECHARTS_BLK = ('```echarts\n{"title": {"text": "销量"}, "xAxis": {"type": "category",'
               ' "data": ["A", "B"]}, "yAxis": {"type": "value"},'
               ' "series": [{"type": "bar", "data": [5, 9]}]}\n```\n\n')


def scenario_think_flip():
    return "## 验证标题\n\n" + PARA + "\n\n" + THINK_BLK + PARA + "\n\n" + CODE_BLK, {"flip_think": True}


def scenario_chart_fence_close():
    return PARA + "\n\n" + ECHARTS_BLK + "图表闭合后继续输出正文，句号结尾。\n\n", {}


def scenario_tool_pending():
    fp = "read_file:src/main.py"  # 值不影响断言（前后一致即可），仅驱动 suppress 分支
    return PARA + "\n\n" + TOOL_SEG + PARA + "\n\n", {"tool_fingerprints_states": [(), (fp,)]}


def scenario_long_table():
    rows = "\n".join(f"| 项目{i} | 数值{i} | 备注{i}：连续单换行无空行边界 |" for i in range(300))
    tbl = "| 列A | 列B | 列C |\n|---|---|---|\n" + rows + "\n"
    return PARA + "\n\n" + tbl, {"milestone": True}


def scenario_long_code():
    body = "\n".join(
        f"def fn_{i}(x):\n    # 注释 {i}：模拟长代码块流式输出\n    return x * {i} + len('常量{i}')"
        for i in range(400)
    )
    return PARA + "\n\n```python\n" + body + "\n```\n\n", {"milestone": True}


def scenario_think_unclosed_silent():
    return "<think>这段思考始终未闭合，流式期间应静默累积，" + PARA * 6, {}


def scenario_mood_flip():
    return (PARA + "\n\n" + MOOD_SEG + "人格块闭合后接正文，句号。\n\n",
            {"environment_note": "mood 注入依赖 UIPluginRegistry 注册状态，采集与比对须同环境"})


def scenario_baseline_reset():
    a = (PARA + "\n\n") * 3 + LIST_BLK + "\n\n"
    b = ("替代内容开头完全不同。\n\n" + CODE_BLK) * 2
    return a + "\n\n替代内容开头完全不同的分界段。\n\n" + b, {}


def scenario_kb300_mixed():
    unit = (PARA + "\n\n" + LIST_BLK + "\n\n" + CODE_BLK + THINK_BLK
            + "| 键 | 值 |\n|---|---|\n| k1 | v1 |\n\n")
    text = unit * (300 * 1024 // len(unit) + 1)
    return text[:300 * 1024], {"milestone": True}


SCENARIOS = [
    ("think_flip", scenario_think_flip),
    ("chart_fence_close", scenario_chart_fence_close),
    ("tool_pending", scenario_tool_pending),
    ("long_table", scenario_long_table),
    ("long_code", scenario_long_code),
    ("think_unclosed_silent", scenario_think_unclosed_silent),
    ("mood_flip", scenario_mood_flip),
    ("baseline_reset", scenario_baseline_reset),
    ("kb300_mixed", scenario_kb300_mixed),
]
SCENARIO_FN = dict(SCENARIOS)


# ────────────────────────── 流式轨迹重放 ──────────────────────────

def chunk_list(text: str, size: int = CHUNK_CHARS):
    return [text[i: i + size] for i in range(0, len(text), size)]


def _sample_set(n_frames: int, milestone: bool) -> set:
    if n_frames <= 30 or not milestone:
        return set(range(n_frames))
    # milestone 场景（300KB 级）：每帧全管线 O(n)，步长 250 控采集耗时（实测 127s→约 45s）
    return set(range(20)) | set(range(20, n_frames, 250)) | {n_frames - 1}


def _flip_point(chunks) -> "int | None":
    """</think> 闭合所在帧号（闭合一到即翻转 thinking_finalized）。"""
    j = ""
    for i, c in enumerate(chunks):
        j += c
        if "</think>" in j:
            return i + 1
    return None


def replay_stream(md_text: str, opts: dict) -> dict:
    """逐 chunk 追加，采样帧双通道渲染；终态按 tool_fingerprints 各态再采。"""
    chunks = chunk_list(md_text)
    flip_at = _flip_point(chunks) if opts.get("flip_think") else None
    sampled = _sample_set(len(chunks), bool(opts.get("milestone")))
    frames_main, frames_worker = {}, {}
    cur, thinking_finalized = "", True
    for i, c in enumerate(chunks):
        cur += c
        if flip_at is not None:
            thinking_finalized = i >= flip_at
        if i in sampled:
            frames_main[i] = record(render_mainline(
                StubHost(streaming=True, thinking_finalized=thinking_finalized), cur))
            # worker 与 mainline 是两份同构管线拷贝，改动面可能只动一份，双通道必须并采
            frames_worker[i] = record(render_worker(
                cur, streaming=True, thinking_finalized=thinking_finalized))
    final = {}
    for k, fps in enumerate(opts.get("tool_fingerprints_states", [()])):
        final[f"mainline_fp{k}"] = record(render_mainline(
            StubHost(streaming=False, tool_fingerprints=fps), cur))
        final[f"worker_fp{k}"] = record(render_worker(
            cur, streaming=False, tool_fingerprints=fps))
    return {"n_frames": len(chunks), "sampled": sorted(sampled),
            "frames_mainline": frames_main, "frames_worker": frames_worker, "final": final}


# ────────────────────────── 方案① 主题矩阵 ──────────────────────────

def theme_inputs():
    return {
        "code_py": "# 代码标题\n\n" + CODE_BLK * 3,
        "diff_block": "```diff\n--- a/app.py\n+++ b/app.py\n@@ -1,3 +1,3 @@\n-old = 1\n+new = 2\n```\n\n",
        "plain_text": PARA + "\n\n第二段正文，不含任何代码与图标语法。\n\n",
    }


def collect_theme_matrix() -> dict:
    """主线程入口含候选 1 主题检测链（每帧 import + is_light_theme + pygments 链）。

    主题态用 mock 注入 is_light_theme（不依赖本机配置），入口自身按返回值切
    pygments style；golden 捕捉「同一主题态下的渲染结果」，候选 1 缓存化后必须不变。

    ⚠️ 每格必须双 mock + 清缓存：
    a) mock ThemeRefreshCoordinator.get_version 每格递增 —— lru_cache key 含 theme_ver，
       mock is_light_theme 不递增 version 时深格永远命中浅格缓存（自测实证：两态 sha
       相同），测的是缓存复用路径而非渲染路径；
    b) 清 _render_markdown_to_html_cached_impl.cache_clear 兜底；
    c) 若两态 sha 仍相同，如实记录：说明该输入产物与主题链无关（这是真实基线），
       等价断言依旧成立（改造后仍须两态相同）。
    """
    from unittest.mock import patch
    from app.utils import theme_manager as tm
    from app.utils import theme_refresh as tr
    impl = crc._render_markdown_to_html_cached_impl
    out = {}
    ver = iter(range(1000, 1000 + 12))
    for in_name, text in theme_inputs().items():
        for theme, ret in (("light", True), ("dark", False)):
            impl.cache_clear()
            with patch.object(tm.theme_manager, "is_light_theme", return_value=ret), \
                 patch.object(tr.ThemeRefreshCoordinator, "get_version", staticmethod(lambda: next(ver))):
                html = render_mainline(StubHost(streaming=False), text)
            out[f"{in_name}__{theme}"] = record(html)
    impl.cache_clear()
    return out


# ────────────────────────── 方案② 边界判定（纯函数层）──────────────────────────

BOUNDARY_TEXTS = {
    "para_end": PARA + "\n\n",
    "code_closed": "```python\nx = 1\n```\n",
    "think_closed": "<think>思考</think>\n",
    "sentence_end": "这是一句完整的话。",
    "empty": "",
    "blank_only": "  \n\t  ",
    "no_boundary": PARA,
}


def collect_boundary() -> dict:
    f_clean = cv.CodeWebViewer._has_reached_clean_boundary
    f_soft = cv.CodeWebViewer._has_reached_soft_boundary
    return {k: {"clean": bool(f_clean(v)), "soft": bool(f_soft(v))}
            for k, v in BOUNDARY_TEXTS.items()}


# ────────────────────────── 方案③ hash/startswith 判定序列 ──────────────────────────

def _prefix_corpus() -> dict:
    rnd = random.Random(20261114)
    corpora = {
        "cn_para": PARA * 30,
        "with_code": (PARA + "\n\n" + CODE_BLK) * 12,
        "with_think": (THINK_BLK + PARA + "\n\n") * 10,
        "long_table": SCENARIO_FN["long_table"]()[0],
    }
    out = {}
    for name, text in corpora.items():
        cuts = sorted(rnd.sample(range(200, len(text)), min(120, len(text) - 200)))
        seq, prev = [], 0
        for c in cuts:
            seq.append(text[prev:c])
            prev = c
        out[name] = seq
    return out


def old_cache_hit(cur: str, cached_hash) -> bool:
    return cached_hash is not None and hash(cur) == cached_hash


def old_baseline_ok(cur: str, last_rendered: str) -> bool:
    return bool(last_rendered) and cur.startswith(last_rendered)


def _replay_judgement(hit_fn, ok_fn, seq):
    acc, cached_hash, last_rendered = "", None, ""
    hits, baselines = [], []
    for seg in seq:
        acc += seg
        hits.append(bool(hit_fn(acc, cached_hash)))
        baselines.append(bool(ok_fn(acc, last_rendered)))
        cached_hash, last_rendered = hash(acc), acc  # 模拟「上次全量渲染」写缓存/基线推进
    return hits, baselines


def collect_hash_equiv() -> dict:
    out = {}
    for name, seq in _prefix_corpus().items():
        hits, baselines = _replay_judgement(old_cache_hit, old_baseline_ok, seq)
        out[name] = {"n": len(seq), "hit_seq": hits, "baseline_seq": baselines}
    return out


def compare_hash_equiv(golden: dict) -> dict:
    f_hit = getattr(cv.CodeWebViewer, "_equiv_cache_hit", None)
    f_ok = getattr(cv.CodeWebViewer, "_equiv_baseline_ok", None)
    if f_hit is None or f_ok is None:
        return {"__skipped__": {"verdict": "SKIPPED_NEW_IMPL_MISSING",
                                "note": "产品侧未暴露 _equiv_cache_hit/_equiv_baseline_ok（见文件头注约定）"}}
    out = {}
    corpus = _prefix_corpus()
    for name, g in golden.items():
        hits, baselines = _replay_judgement(f_hit, f_ok, corpus[name])
        bad = [i for i, (a, b) in enumerate(zip(hits, g["hit_seq"])) if a != b]
        bad += [i for i, (a, b) in enumerate(zip(baselines, g["baseline_seq"])) if a != b]
        out[name] = {"verdict": "exact" if not bad else "FAIL", "mismatches": bad[:10]}
    return out


# ────────────────────────── 方案⑥ 边界与 tail ──────────────────────────

BOUNDARY_SEGS = {
    "start_0_full": (PARA + "\n\n" + LIST_BLK + "\n\n" + CODE_BLK, 0),
    "start_at_len": (PARA + "\n\n", 10 ** 9),
    "cut_on_separator": (PARA + "\n\n" + PARA, len(PARA)),
    "cut_in_fence": (CODE_BLK + PARA + "\n\n",
                     len("```python\ndef golden_check(cards: list[str], expected: str) -> bool:\n")),
    "empty_input": ("", 0),
}


def collect_segments_boundary() -> dict:
    out = {}
    for name, (md, start) in BOUNDARY_SEGS.items():
        slice_md = md[start:]
        stable_len, segs = crc._extract_closed_segments(slice_md)
        out[name] = {"input_len": len(slice_md), "stable_len": stable_len, "segs": segs}
    return out


TAIL_BODY = ("**加粗开头**的一段行内语法语料，含 `行内代码` 与 [链接](https://example.com) 以及"
             "*斜体*，用于 tail 行内渲染前缀序列采集，无空行边界。") * 8
TAIL_WITH_THINK = "<think>未闭合" + PARA


def collect_tail_prefixes() -> dict:
    seq = chunk_list(TAIL_BODY, 12)
    acc, frames = "", []
    for c in seq:
        acc += c
        frames.append(record(crc._render_inline_tail(acc, compact=True)))
    guard = crc._render_inline_tail(TAIL_WITH_THINK, compact=True)
    return {
        "syntax_prefixes": {"n": len(seq), "frames": frames},
        "think_guard": {"input_len": len(TAIL_WITH_THINK), "output_len": len(guard),
                        "is_empty": guard == "", "sha256": sha(guard)},
    }


# ────────────────────────── collect / compare ──────────────────────────

def run_all() -> dict:
    result = {"stream": {}}
    for name, fn in SCENARIOS:
        md_text, opts = fn()
        result["stream"][name] = replay_stream(md_text, opts)
    result["theme_matrix"] = collect_theme_matrix()
    result["boundary"] = collect_boundary()
    result["hash_equiv"] = collect_hash_equiv()
    result["segments_boundary"] = collect_segments_boundary()
    result["tail"] = collect_tail_prefixes()
    return result


def cmd_collect(dry_run: bool, out_path: str) -> int:
    t0 = time.perf_counter()
    result = run_all()
    elapsed = round(time.perf_counter() - t0, 1)
    print(f"[collect] 场景 {len(result['stream'])} 个，总耗时 {elapsed}s")
    for name, r in result["stream"].items():
        fk = sorted(r["final"])[0]
        print(f"  - {name:22s} frames={r['n_frames']:5d} sampled={len(r['sampled']):3d} "
              f"final[{fk}] sha={r['final'][fk]['sha256'][:16]} len={r['final'][fk]['len']}")
    tmx = result["theme_matrix"]
    print("[collect] 主题矩阵: " + " ".join(f"{k}={v['sha256'][:8]}" for k, v in tmx.items()))
    print(f"[collect] 边界判定 {len(result['boundary'])} 项；segments 边界 {len(result['segments_boundary'])} 项；"
          f"hash_equiv 语料 {len(result['hash_equiv'])} 组；tail 前缀 {result['tail']['syntax_prefixes']['n']} 帧")
    if dry_run:
        print("[collect] --dry-run：不写任何文件（自测模式）")
        return 0
    payload = {"meta": {"git_rev": git_rev(), "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "python": sys.version.split()[0], "elapsed_s": elapsed,
                        "assertion_policy": ("exact-first; ws-normalized fallback with pre-block "
                                             "byte-exact; no DOM-semantic; hash-numeric never persisted")}}
    payload.update(result)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"[collect] golden 已落盘：{out_path}")
    return 0


def cmd_compare(baseline_path: str) -> int:
    if not os.path.exists(baseline_path):
        print(f"[compare] 找不到基线：{baseline_path}（等统一信号执行 collect 正式落盘）")
        return 2
    with open(baseline_path, "r", encoding="utf-8") as f:
        golden = json.load(f)
    t0 = time.perf_counter()
    now = run_all()
    elapsed = round(time.perf_counter() - t0, 1)
    report = {"meta": {"git_rev": git_rev(), "elapsed_s": elapsed,
                       "baseline_rev": golden["meta"]["git_rev"],
                       "baseline_created": golden["meta"]["created"]},
              "counts": {"exact": 0, "normalized": 0, "FAIL": 0, "SKIPPED_NEW_IMPL_MISSING": 0},
              "degraded": [], "failed": [], "sections": {}}
    cnt = report["counts"]

    # 1) 流式轨迹：帧级（双通道）+ 终态（各指纹态）
    for name, g in golden["stream"].items():
        n = now["stream"][name]
        for key, tag in (("frames_mainline", "mainline"), ("frames_worker", "worker")):
            for idx, grec in g[key].items():
                # JSON round-trip 后帧号键变 str，now 侧是 int，统一 int 归一
                v = verdict_of(grec, n[key][int(idx)], f"stream.{name}.{tag}.frame{idx}", report)
                cnt[v] += 1
        for fk, grec in g["final"].items():
            v = verdict_of(grec, n["final"][fk], f"stream.{name}.{fk}", report)
            cnt[v] += 1

    # 2) 主题矩阵（候选 1 改动面）
    for k, grec in golden["theme_matrix"].items():
        v = verdict_of(grec, now["theme_matrix"][k], f"theme.{k}", report)
        cnt[v] += 1

    # 3) 边界判定 / segments 边界 / tail（候选 2/7/8 改动面）
    for section, gkey in (("boundary", "boundary"),
                          ("segments_boundary", "segments_boundary"),
                          ("tail", "tail")):
        same = golden[gkey] == now[gkey]
        report["sections"][section] = {"verdict": "exact" if same else "FAIL"}
        cnt["exact" if same else "FAIL"] += 1

    # 4) 候选 3 判定序列（探测约定新实现）
    heq = compare_hash_equiv(golden["hash_equiv"])
    report["sections"]["hash_equiv"] = heq
    for r in heq.values():
        cnt[r["verdict"]] = cnt.get(r["verdict"], 0) + 1

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"[compare] counts={cnt} 降档={len(report['degraded'])} 失败={len(report['failed'])} "
          f"耗时={elapsed}s")
    for d in report["degraded"]:
        print(f"  [降档] {d['section']}: {d['note']}")
    for d in report["failed"][:10]:
        print(f"  [FAIL] {d['section']} golden_len={d['golden']['len']} now_len={d['now']['len']}")
    print(f"[compare] 报告已写：{REPORT_PATH}")
    return 0 if cnt["FAIL"] == 0 else 1


def main():
    ap = argparse.ArgumentParser(description="渲染管线 golden 采集与等价比对")
    ap.add_argument("mode", choices=["collect", "compare"])
    ap.add_argument("--dry-run", action="store_true", help="collect 全场景跑通但不写文件")
    ap.add_argument("--baseline", default=BASELINE_PATH, help="compare 的基线路径")
    args = ap.parse_args()
    if args.mode == "collect":
        sys.exit(cmd_collect(args.dry_run, BASELINE_PATH))
    sys.exit(cmd_compare(args.baseline))


if __name__ == "__main__":
    main()

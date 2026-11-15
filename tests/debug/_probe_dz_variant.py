# -*- coding: utf-8 -*-
"""单变量受控实验：放大页 dataZoom inside 的滚轮行为对 wheel 帧耗时的影响（G0 恢复）。

复用 chart_viewer_perf_harness 的 A 组真窗，只在 build_chart_viewer_html 产物上做
**参数化注入**（正则匹配 + 命中计数断言），跑同一 20 次 wheel 序列，比较 p95 / long33。

修复背景：21b77723e 把生产串改成
`zoomOnMouseWheel: false, moveOnMouseMove: false, moveOnMouseWheel: false`，
旧脚本的 `str.replace("zoomOnMouseWheel: true, moveOnMouseMove: true", ...)` 静默不命中，
三组实跑同一份 HTML，产出「无差异」假结论。本版改注入式，未命中立即 AssertionError。

**反向验证口径（别搞反）**：当前生产已是 `false`（无滚轮劫持）的优化后状态；要复现
修复前的 33.7→17.8ms 差异，必须主动注入 `zoomOnMouseWheel: true` 造出劣化对照组，
再与 `false` 组对照。三组：
  A  wheel_zoom_true         强制 true（= 修复前，滚轮劫持成全量 raster）
  B  wheel_zoom_false        生产当前 false（= 修复后）
  C  wheel_zoom_false_anim300 false + animationDurationUpdate=300（验证动画键边际收益）
三组必须真的跑三份不同 HTML（两两 sha256 不等，硬断言）。

平台：真窗 offscreen 第 4 档，QT_QPA_PLATFORM=windows，禁 offscreen。
采样：预热 1s 后由 harness 的 rAF 帧间隔采样（禁首帧采样）。
收尾保留 os._exit(0)（防 Qt 退出崩溃）。

运行：python tests/debug/_probe_dz_variant.py   （真窗短暂弹 3 个窗口）
输出：tests/debug/_probe_data/dz_variant_results.json + stdout 三组对照表
"""
import hashlib
import json
import os
import re
import sys
import time

os.environ["QT_QPA_PLATFORM"] = "windows"
_PROBE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(_PROBE_DIR))))
sys.path.insert(0, _PROBE_DIR)

import chart_viewer_perf_harness as H  # noqa: E402
from app.widgets.cards.settings.chart_viewer_card import build_chart_viewer_html  # noqa: E402

# ── 注入锚点（正则，匹配具体值；命中数为 0 立即断言失败，禁静默） ─────────────
_ZOOM_RE = re.compile(r"(zoomOnMouseWheel: )(true|false)")
_ANIM_RE = re.compile(r"(option\.animationDurationUpdate = )(\d+)(;)")


def _inject(html: str, zoom: str | None, anim_ms: int | None) -> tuple[str, dict]:
    """参数化注入：按值替换，返回 (新 HTML, 命中计数)。任何请求未命中即抛断言。"""
    hits = {"zoom": 0, "anim": 0}
    if zoom is not None:
        html, n = _ZOOM_RE.subn(lambda m: m.group(1) + zoom, html)
        hits["zoom"] = n
        if n != 1:
            raise AssertionError(f"zoomOnMouseWheel 注入未命中或多次命中：hits={n}，生产串已变更？")
    if anim_ms is not None:
        html, n = _ANIM_RE.subn(lambda m: m.group(1) + str(anim_ms) + m.group(3), html)
        hits["anim"] = n
        if n != 1:
            raise AssertionError(f"animationDurationUpdate 注入未命中或多次命中：hits={n}，生产串已变更？")
    return html, hits


VARIANTS = [
    # name, zoom 注入值, anim 注入值, 说明
    ("wheel_zoom_true", "true", None, "强制滚轮劫持（修复前劣化对照组）"),
    ("wheel_zoom_false", "false", None, "生产当前（修复后，无滚轮劫持）"),
    ("wheel_zoom_false_anim300", "false", 300, "false + update 补间 300ms（动画键边际收益）"),
]


def run_variant(name: str, zoom: str | None, anim_ms: int | None, desc: str) -> dict:
    base_html = build_chart_viewer_html("echarts", H.PAYLOAD_B64, is_dark=True)
    html, hits = _inject(base_html, zoom, anim_ms)
    sha = hashlib.sha256(html.encode("utf-8")).hexdigest()

    g = H.Group("V", 1000, 700, False)
    g.win.resize(g.win_w, g.win_h)
    g.win.show()
    H.pump(300)
    load_ms, ok = H.load_html(g.view, html)
    ready = bool(H.js(g.view, H._READY_JS))
    if not ok or not ready:
        raise RuntimeError(f"页面未就绪 ok={ok} ready={ready} load={load_ms}ms")
    H.js(g.view, H._PROBE_JS)
    # 预热 1s，让入场动画/首帧布局稳定，禁首帧采样
    H.pump(1000)
    r = H.stage_wheel(g)
    g.win.close()
    H.pump(100)
    f = r["frames"]
    return {
        "name": name, "desc": desc, "html_sha256": sha, "html_len": len(html),
        "inject_hits": hits, "dz": r["dzCount"], "p95": f["p95"], "max": f["max"],
        "long33": f["long33"], "long16": f["long16"], "n": f["n"],
    }


def main():
    # 静态先验：三组 HTML 两两不同，且每条注入都命中
    pre = []
    for name, zoom, anim_ms, desc in VARIANTS:
        h, hits = _inject(build_chart_viewer_html("echarts", H.PAYLOAD_B64, is_dark=True), zoom, anim_ms)
        pre.append((name, hashlib.sha256(h.encode("utf-8")).hexdigest(), hits))
    shas = [p[1] for p in pre]
    if len(set(shas)) != len(VARIANTS):
        raise AssertionError(f"三组 HTML 未两两不同：{list(zip([p[0] for p in pre], shas))}")
    print(f"[G0] 三组 HTML 两两不同：{[(p[0], p[1][:12], p[2]) for p in pre]}", flush=True)

    out = []
    for name, zoom, anim_ms, desc in VARIANTS:
        t0 = time.perf_counter()
        r = run_variant(name, zoom, anim_ms, desc)
        r["wall_s"] = round(time.perf_counter() - t0, 1)
        out.append(r)
        print(f"[G0] {name} done in {r['wall_s']}s p95={r['p95']} long33={r['long33']}", flush=True)

    # 运行期再断言一次：实跑的三份 HTML 同样两两不同
    run_shas = [r["html_sha256"] for r in out]
    if len(set(run_shas)) != len(VARIANTS):
        raise AssertionError(f"实跑三组 HTML 未两两不同：{list(zip([r['name'] for r in out], run_shas))}")

    path = os.path.join(_PROBE_DIR, "_probe_data", "dz_variant_results.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "meta": {
            "platform": os.environ.get("QT_QPA_PLATFORM"),
            "note": "真窗 windows 档；预热 1s 后 rAF 帧间隔采样；三组 HTML 两两不同已断言",
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "variants": out,
        "pairwise_distinct": True,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    for r in out:
        print(f"{r['name']:26s} sha={r['html_sha256'][:12]} dz={r['dz']:3d} "
              f"p95={r['p95']:6.1f} max={r['max']:6.1f} "
              f"long33={r['long33']:3d} long16={r['long16']:3d} frames={r['n']}")
    a, b, c = out
    print(f"[G0] 方向校验 true vs false：p95 {a['p95']} vs {b['p95']}ms，"
          f"long33 {a['long33']} vs {b['long33']}；"
          f"true 组应显著劣于 false 组（历史参照 33.7 vs 17.8 / 11 vs 1，允许机器差异）")
    print(f"[G0] 结果已写 {path}")
    # os._exit 不 flush stdio，显式冲刷，避免末尾对照表/判定被缓冲吞掉
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FAIL {type(e).__name__}: {e}")
        os._exit(2)

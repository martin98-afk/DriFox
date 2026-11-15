# -*- coding: utf-8 -*-
"""单变量受控实验：放大页 dataZoom inside 的滚轮行为对 wheel 帧耗时的影响。

复用 chart_viewer_perf_harness 的 A 组真窗，只改 build_chart_viewer_html 产物里
dataZoom inside 的 zoomOnMouseWheel 配置，跑同一 20 次 wheel 序列，比较 p95 / long33。
"""
import json
import os
import sys
import time

os.environ["QT_QPA_PLATFORM"] = "windows"
_PROBE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(_PROBE_DIR))))
sys.path.insert(0, _PROBE_DIR)

import chart_viewer_perf_harness as H  # noqa: E402
from app.widgets.cards.settings.chart_viewer_card import build_chart_viewer_html  # noqa: E402


def run_variant(name: str, patch) -> dict:
    html = build_chart_viewer_html("echarts", H.PAYLOAD_B64, is_dark=True)
    html = patch(html)
    g = H.Group("V", 1000, 700, False)
    g.win.resize(g.win_w, g.win_h)
    g.win.show()
    H.pump(300)
    load_ms, ok = H.load_html(g.view, html)
    ready = bool(H.js(g.view, H._READY_JS))
    if not ok or not ready:
        raise RuntimeError(f"页面未就绪 ok={ok} ready={ready} load={load_ms}ms")
    H.js(g.view, H._PROBE_JS)
    H.pump(500)
    r = H.stage_wheel(g)
    g.win.close()
    H.pump(100)
    f = r["frames"]
    return {"name": name, "dz": r["dzCount"], "p95": f["p95"], "max": f["max"],
            "long33": f["long33"], "long16": f["long16"], "n": f["n"]}


def main():
    out = []
    # 基线：生产当前（rAF resize + animationDurationUpdate=0 + 默认 inside）
    out.append(run_variant("baseline_current", lambda h: h))
    # 变体 1：普通滚轮不缩放，只保留 slider 拖拽缩放
    out.append(run_variant(
        "no_wheel_zoom",
        lambda h: h.replace("zoomOnMouseWheel: true, moveOnMouseMove: true",
                            "zoomOnMouseWheel: false, moveOnMouseMove: false"),
    ))
    # 变体 2：关滚轮缩放 + 关 update 动画（确认动画键是否还有额外收益）
    out.append(run_variant(
        "no_wheel_zoom_no_anim",
        lambda h: h.replace("zoomOnMouseWheel: true, moveOnMouseMove: true",
                            "zoomOnMouseWheel: false, moveOnMouseMove: false")
                   .replace("option.animationDurationUpdate = 0;", "option.animation = false;"),
    ))
    path = os.path.join(_PROBE_DIR, "_probe_data", "dz_variant_results.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    for r in out:
        print(f"{r['name']:24s} dz={r['dz']:3d} p95={r['p95']:6.1f} max={r['max']:6.1f} "
              f"long33={r['long33']:3d} long16={r['long16']:3d} frames={r['n']}")
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FAIL {type(e).__name__}: {e}")
        os._exit(2)

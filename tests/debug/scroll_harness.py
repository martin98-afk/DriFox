# -*- coding: utf-8 -*-
"""滚动行为 harness：真实 QWebEngineView 中验证"纯几何判定滚底"语义。

运行：SCROLL_HARNESS_DEBUG=1 uv run python tests/debug/scroll_harness.py
非 pytest 用例（需要 Qt.AA_ShareOpenGLContexts 与 WebEngine 渲染进程），
退出码 0 = 全部场景通过。

骨架事实：html,body{overflow:hidden}——viewer 高度由 reportHeight→setFixedHeight
跟随内容；body 滚动条仅在内容超 MAX_HEIGHT 时出现。日常内滚容器：
坞态 #content-placeholder（max-height 限高）与 #tool-content（110px 限高）。

验证场景：
1. body 可滚（模拟超长消息）+ 底部 → _autoScrollStreamingBody 跟随
2. body 中间 → 零干预
3. 滚回底部 → 恢复跟随
4. 坞态 _cp：底部跟随 / 中间零干预
5. 工具区 tc：底部跟随 / 中间零干预
6. _nearBottom 边界：无溢出=在底部；距底超阈值=不在
"""

import functools
import os
import sys

print = functools.partial(print, flush=True)  # 全程无缓冲，崩溃前也能看到输出

os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")

from PyQt5.QtCore import Qt, QCoreApplication, QTimer

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

from PyQt5.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.core.webengine_profile import init_shared_web_profile  # noqa: E402
from app.widgets.message_card import CodeWebViewer  # noqa: E402

app = QApplication(sys.argv)
init_shared_web_profile()

viewer = CodeWebViewer(None)
viewer.resize(600, 800)

DEBUG = True  # 常开：_dbg 分支（print 后让出回调栈）实测更稳
RESULTS = {}


def run_js(js, callback):
    def _cb(res):
        # 业务回调移出 QtWebEngine 回调栈：在回调栈内直接执行链式
        # runJavaScript/复杂 Python 时会触发渲染进程 fail-fast（0xC0000409）
        QTimer.singleShot(0, lambda: callback(res))

    if DEBUG:
        def _dbg(res):
            print("  js result:", str(res)[:300])
            QTimer.singleShot(0, lambda: _cb(res))
        viewer.page().runJavaScript(js, _dbg)
    else:
        viewer.page().runJavaScript(js, _cb)


RESET = """
(function(){
  // 恢复干净滚动环境：重建两个内滚容器 + 打开 body 滚动（模拟超长消息）
  document.body.classList.remove('streaming-dock');
  document.body.style.overflowY = 'auto';
  document.body.style.height = 'auto';
  document.body.innerHTML =
    '<div id="content-placeholder" style="height:300px;max-height:300px;overflow-y:auto"></div>' +
    '<div id="tool-section"><div id="tool-content" style="height:110px;overflow-y:auto"></div></div>';
  var cp = document.getElementById('content-placeholder');
  var tc = document.getElementById('tool-content');
  var cpHtml = ''; for (var i=0;i<60;i++) cpHtml += '<p>para '+i+'</p>';
  var tcHtml = ''; for (var i=0;i<40;i++) tcHtml += '<div class="tool-block">tool '+i+'</div>';
  cp.innerHTML = cpHtml;
  tc.innerHTML = tcHtml;
  var bodyHtml = ''; for (var i=0;i<120;i++) bodyHtml += '<p>line '+i+'</p>';
  // body 直接内容撑高（追加在容器之后）
  var tail = document.createElement('div'); tail.innerHTML = bodyHtml;
  document.body.appendChild(tail);
  return 'ok';
})()
"""


def reset(done, next_step):
    def _cb(r):
        done(r == "ok") if r != "ok" else next_step()

    run_js(RESET, _cb)


SCENARIOS = []


def scenario(name):
    def deco(fn):
        SCENARIOS.append((name, fn))
        return fn

    return deco


def start():
    # 不 show 也有视口尺寸（离屏渲染）；show+close 在部分环境触发 teardown 崩溃
    viewer._load_skeleton()
    viewer.loadFinished.connect(lambda _ok: QTimer.singleShot(800, _on_ready))
    QTimer.singleShot(8000, _on_ready)


_ready_fired = False


def _on_ready(*_a):
    global _ready_fired
    if _ready_fired:
        return
    _ready_fired = True
    # --only <name>：每次进程只跑一个场景（QtWebEngine 无窗口模式下
    # 连续多场景 runJavaScript 会触发渲染进程崩溃 0xC0000409）
    only = [a for a in sys.argv[1:] if a.startswith('--only=')]
    if only:
        want = only[0].split('=', 1)[1]
        for i, (n, f) in enumerate(SCENARIOS):
            if n == want:
                _run_scenario(i)
                return
        print('no such scenario:', want)
        os._exit(2)
    _run_scenario(0)


def _run_scenario(idx):
    if idx >= len(SCENARIOS):
        _finish()
        return
    name, fn = SCENARIOS[idx]

    def done(ok, extra=None):
        print('  done() enter:', name, flush=True)
        RESULTS[name] = ok
        print(("PASS" if ok else "FAIL"), "-", name, extra or "")
        QTimer.singleShot(200, lambda: _run_scenario(idx + 1))

    def wrapped():
        print('  scenario start:', name, flush=True)
        reset(done, lambda: fn(done))

    try:
        wrapped()
    except Exception as e:  # noqa: BLE001
        print("ERROR -", name, e)
        RESULTS[name] = False
        QTimer.singleShot(200, lambda: _run_scenario(idx + 1))


def _finish():
    failed = [k for k, v in RESULTS.items() if not v]
    print("\n==== harness summary ====")
    print("total:", len(RESULTS), "failed:", len(failed), failed)
    viewer.close()
    app.quit()
    QTimer.singleShot(500, lambda: os._exit(1 if failed else 0))


@scenario("body_bottom_follows")
def _s1(done):
    run_js(
        "(function(){try{"
        "document.body.scrollTop=document.body.scrollHeight;"
        "var before=document.body.scrollTop;"
        "_autoScrollStreamingBody();"
        "return JSON.stringify({before:before, after:document.body.scrollTop,"
        " max:document.body.scrollHeight-document.body.clientHeight});"
        "}catch(e){return 'ERR:'+e.message+' @'+e.lineNumber}})()",
        lambda r: done(bool(r) and isinstance(r, str) and r.startswith("{") and abs(__import__("json").loads(r)["after"] - __import__("json").loads(r)["max"]) < 2, r),
    )


@scenario("body_middle_untouched")
def _s2(done):
    run_js(
        "document.body.scrollTop=(document.body.scrollHeight-document.body.clientHeight)/2;"
        "var before=document.body.scrollTop;"
        "_autoScrollStreamingBody();"
        "JSON.stringify({before:before, after:document.body.scrollTop})",
        lambda r: (print('  raw callback enter', flush=True), done(bool(r) and r["before"] > 10 and r["before"] == r["after"], r)),
    )


@scenario("body_return_bottom_resumes")
def _s3(done):
    run_js(
        "document.body.scrollTop=document.body.scrollHeight;"
        "document.body.scrollTop-=2;"  # 距底 2px < 阈值 → 视为在底部
        "_autoScrollStreamingBody();"
        "JSON.stringify({after:document.body.scrollTop,"
        " max:document.body.scrollHeight-document.body.clientHeight})",
        lambda r: done(bool(r) and abs(r["after"] - r["max"]) < 2, r),
    )


@scenario("body_far_from_bottom_untouched")
def _s4(done):
    run_js(
        "var max=document.body.scrollHeight-document.body.clientHeight;"
        "document.body.scrollTop=max-500;"  # 距底 500px > 阈值 80
        "var before=document.body.scrollTop;"
        "_autoScrollStreamingBody();"
        "JSON.stringify({before:before, after:document.body.scrollTop})",
        lambda r: done(bool(r) and r["before"] == r["after"], r),
    )


@scenario("dock_cp_bottom_follows")
def _s5(done):
    # 坞态：body class streaming-dock + _toolCompactMode
    run_js(
        "document.body.classList.add('streaming-dock');window._toolCompactMode=true;"
        "var cp=document.getElementById('content-placeholder');"
        "cp.scrollTop=cp.scrollHeight;"
        "_autoScrollStreamingBody();"  # 无参：允许碰 _cp（模拟正文自身更新）
        "JSON.stringify({after:cp.scrollTop, max:cp.scrollHeight-cp.clientHeight})",
        lambda r: done(bool(r) and abs(r["after"] - r["max"]) < 2, r),
    )


@scenario("dock_cp_middle_untouched")
def _s6(done):
    run_js(
        "document.body.classList.add('streaming-dock');window._toolCompactMode=true;"
        "var cp=document.getElementById('content-placeholder');"
        "cp.scrollTop=(cp.scrollHeight-cp.clientHeight)/2;"
        "var before=cp.scrollTop;"
        "_autoScrollStreamingBody(true);"  # bodyOnly：工具/思考更新不碰 _cp
        "JSON.stringify({before:before, after:cp.scrollTop})",
        lambda r: done(bool(r) and r["before"] == r["after"], r),
    )


@scenario("dock_cp_middle_bodyonly_no_touch")
def _s7(done):
    # 用户在 _cp 中间阅读时，body 底部跟随不得波及 _cp
    run_js(
        "document.body.classList.add('streaming-dock');window._toolCompactMode=true;"
        "document.body.scrollTop=document.body.scrollHeight;"
        "var cp=document.getElementById('content-placeholder');"
        "cp.scrollTop=(cp.scrollHeight-cp.clientHeight)/2;"
        "var before=cp.scrollTop;"
        "_autoScrollStreamingBody(true);"
        "JSON.stringify({before:before, after:cp.scrollTop,"
        " bodyAtBottom:document.body.scrollTop>=document.body.scrollHeight-document.body.clientHeight-2})",
        lambda r: done(bool(r) and r["before"] == r["after"] and r["bodyAtBottom"], r),
    )


@scenario("tool_bottom_follows")
def _s8(done):
    run_js(
        "var tc=document.getElementById('tool-content');"
        "tc.scrollTop=tc.scrollHeight;"
        "_scrollToolContentToBottom();"
        "JSON.stringify({after:tc.scrollTop, max:tc.scrollHeight-tc.clientHeight})",
        lambda r: done(bool(r) and abs(r["after"] - r["max"]) < 2, r),
    )


@scenario("tool_middle_untouched")
def _s9(done):
    run_js(
        "var tc=document.getElementById('tool-content');"
        "tc.scrollTop=(tc.scrollHeight-tc.clientHeight)/2;"
        "var before=tc.scrollTop;"
        "_scrollToolContentToBottom();"
        "JSON.stringify({before:before, after:tc.scrollTop})",
        lambda r: done(bool(r) and r["before"] == r["after"], r),
    )


@scenario("nearbottom_edge_cases")
def _s10(done):
    run_js(
        "var tc=document.getElementById('tool-content');"
        "var max=tc.scrollHeight-tc.clientHeight;"
        "var noOverflow = _nearBottom(document.createElement('div'), 30);"  # 无溢出 → true
        "tc.scrollTop = max - 31;"  # 距底 31 > 30 → false
        "var far = _nearBottom(tc, 30);"
        "tc.scrollTop = max - 29;"  # 距底 29 ≤ 30 → true
        "var near = _nearBottom(tc, 30);"
        "JSON.stringify({noOverflow:noOverflow, far:far, near:near})",
        lambda r: done(bool(r) and r["noOverflow"] is True and r["far"] is False and r["near"] is True, r),
    )


start()
app.exec_()

failed = [k for k, v in RESULTS.items() if not v]
print("\n==== harness summary ====")
print("total:", len(RESULTS), "failed:", len(failed), failed)
sys.exit(1 if failed else 0)

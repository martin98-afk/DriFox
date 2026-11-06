# -*- coding: utf-8 -*-
"""插件市场下载进度与取消的回归测试

覆盖三条新行为：
1. CancelToken 语义：cancel 后 check 抛 InstallCancelled；seal 后取消失效
   （落位/装依赖阶段不可中断，否则插件目录会停在半移动状态）
2. git 的 stderr 进度行解析成 0~1 分数（分阶段插值，不是裸百分比）
3. download_tarball 边下边上报进度，且取消能打断阻塞读取
"""

import os
import sys
import threading
from io import BytesIO
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))


# ── 1. CancelToken 语义 ─────────────────────────────────

def test_cancel_raises_on_check():
    from ui import cancel

    token = cancel.CancelToken()
    with cancel.bind(token, None):
        cancel.check()  # 未取消：不抛
        token.cancel()
        try:
            cancel.check()
        except cancel.InstallCancelled:
            pass
        else:
            raise AssertionError("取消后 check() 必须抛 InstallCancelled")


def test_seal_makes_cancel_inert():
    """封盘后进入落位阶段：此时取消不生效，避免目录半移动"""
    from ui import cancel

    token = cancel.CancelToken()
    token.seal()
    token.cancel()
    with cancel.bind(token, None):
        cancel.check()  # 不抛
    assert token.cancelled is False
    assert token.is_sealed is True


def test_abort_callback_fires_on_cancel():
    from ui import cancel

    token = cancel.CancelToken()
    fired = []
    token.add_abort("k", lambda: fired.append("abort"))
    token.cancel()
    assert fired == ["abort"], "cancel 必须执行已注册的中止回调"


def test_abort_callback_dropped_after_seal():
    """封盘后新注册的 abort 不执行（seal 已清空且拒绝新回调）"""
    from ui import cancel

    token = cancel.CancelToken()
    token.seal()
    fired = []
    token.add_abort("k", lambda: fired.append("abort"))
    token.cancel()
    assert fired == []


def test_report_without_binding_is_noop():
    """未绑定上下文（AI 工具同步调用 installer）不得报错"""
    from ui import cancel

    cancel.check()
    cancel.report(0.5, "x")
    assert cancel.current() is None


# ── 2. git 进度解析 ─────────────────────────────────────

def test_git_progress_line_parsed():
    from ui import cancel
    from ui.installer import _report_git_progress

    got = []
    with cancel.bind(None, lambda frac, txt: got.append((frac, txt))):
        _report_git_progress("Receiving objects:  45% (4521/10048), 3.42 MiB | 1.20 MiB/s")
    assert got, "进度行必须被解析并上报"
    frac, text = got[-1]
    assert 0.0 < frac < 1.0
    assert "45%" in text


def test_git_progress_ignores_non_progress_line():
    from ui import cancel
    from ui.installer import _report_git_progress

    got = []
    with cancel.bind(None, lambda frac, txt: got.append((frac, txt))):
        _report_git_progress("Cloning into 'x'...")
    assert got == []


def test_git_progress_stage_interpolation():
    """同阶段 100% 只占到该阶段的区间上界，不直接到 1.0"""
    from ui import cancel
    from ui.installer import _report_git_progress

    got = []
    with cancel.bind(None, lambda frac, txt: got.append(frac)):
        _report_git_progress("Receiving objects: 100% (100/100), done.")
    assert got[-1] < 1.0, "接收对象 100% 只到阶段上界，后还有解析/写文件"
    assert got[-1] >= 0.84


def test_git_progress_remote_prefix_lowercase():
    """回归：实测 git 输出 `remote: Counting objects: 5%`（前缀小写 + 阶段名无 Remote）

    早期正则写死大写 `Remote: Counting objects` → 远端统计与压缩阶段
    的进度全部漏掉，进度条长时间停在 0。
    """
    from ui import cancel
    from ui.installer import _report_git_progress

    got = []
    with cancel.bind(None, lambda frac, txt: got.append((frac, txt))):
        _report_git_progress("remote: Counting objects:   5% (15/290)")
        _report_git_progress("remote: Compressing objects:  50% (20/40)")
    assert len(got) == 2, f"小写 remote 前缀的进度行必须被解析，实际 {got!r}"
    assert got[0][0] < got[1][0], "压缩阶段整体进度应高于统计阶段"
    assert "远端" in got[0][1]


# ── 3. tarball 下载进度与取消 ───────────────────────────

class _FakeResponse:
    """最小 urllib 响应桩：按块吐数据，可被 _abort_response 打断"""

    def __init__(self, total: int, chunk: int = 1024):
        self._left = total
        self._chunk = chunk
        self.headers = {"Content-Length": str(total)}
        self.fp = None

    def read(self, n: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        take = min(self._chunk, self._left)
        self._left -= take
        return b"x" * take

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_download_tarball_reports_fraction(tmp_path, monkeypatch):
    from ui import archive_downloader as ad
    from ui import cancel

    total = 40 * 1024
    monkeypatch.setattr(
        ad, "_build_opener", lambda proxy: type("O", (), {"open": lambda self, req, timeout=None: _FakeResponse(total)})()
    )

    got = []
    token = cancel.CancelToken()
    dest = tmp_path / "pkg.tar.gz"
    with cancel.bind(token, lambda frac, txt: got.append(frac)):
        sha = ad.download_tarball(["https://example.com/pkg.tar.gz"], dest, proxy=None)

    assert len(sha) == 64
    assert dest.stat().st_size == total
    fractions = [f for f in got if f is not None]
    assert fractions and fractions[-1] == 1.0, "下载完成必须上报 1.0"
    assert any(0.0 < f < 1.0 for f in fractions), "中途必须上报 0~1 之间的进度"


def test_download_tarball_cancel_raises_install_cancelled(tmp_path, monkeypatch):
    from ui import archive_downloader as ad
    from ui import cancel

    def _slow_response():
        resp = _FakeResponse(10 * 1024 * 1024, chunk=4096)

        def _read(n=-1):
            # 模拟半开连接：数据缓慢流出，直到被取消打断
            if token.cancelled:
                raise OSError("connection aborted")
            import time

            time.sleep(0.005)
            return resp.read(n)

        resp.read = _read
        return resp

    monkeypatch.setattr(
        ad, "_build_opener", lambda proxy: type("O", (), {"open": lambda self, req, timeout=None: _slow_response()})()
    )

    token = cancel.CancelToken()
    dest = tmp_path / "pkg.tar.gz"

    def _cancel_soon():
        import time

        time.sleep(0.08)
        token.cancel()

    threading.Thread(target=_cancel_soon, daemon=True).start()

    with cancel.bind(token, lambda frac, txt: None):
        try:
            ad.download_tarball(["https://example.com/pkg.tar.gz"], dest, proxy=None)
        except cancel.InstallCancelled:
            pass
        else:
            raise AssertionError("取消后 download_tarball 必须抛 InstallCancelled")

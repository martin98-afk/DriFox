# -*- coding: utf-8 -*-
"""scene 壁纸全保真渲染抓取（Wallpaper Engine 原生渲染 → mp4）

为什么不用「解 pkg 抽静态图」：scene 的画面由多层贴图 + shader + 粒子 +
相机动画实时合成，静态图丢掉全部动态。唯一保真路径是让 WE 本体渲染。

管线：
  1. `-control openWallpaper -playInWindow` 把 scene 渲染进独立窗口
     （-width/-height 指定的是客户区尺寸，窗口会被 WE 居中）
  2. 解包 scene.pkg 成项目目录并关闭日期/时钟/媒体类属性（见 prepare_project），
     改为加载目录版 project.json —— pkg 打包态改不了属性
  3. Windows Graphics Capture 直抓窗口 HWND（抗遮挡；gdigrab 走 BitBlt 抓 D3D
     窗口是黑屏，ddagrab 抓全屏在窗口被最小化时会录到桌面）
  4. 帧管道喂 ffmpeg 编码 mp4

依赖：windows-capture（pip install windows-capture），ffmpeg。
用法：
    python capture_scene.py <scene.pkg 或 project.json> <out.mp4> [--duration 30]
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import queue
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

# pip install --target <dir> windows-capture 后把目录加进来即可，不进宿主环境
_EXTRA_LIBS = Path(__file__).parent.parent / "pylibs"
if _EXTRA_LIBS.exists():
    sys.path.insert(0, str(_EXTRA_LIBS))

WE_DIR = Path(r"G:/Steam/steamapps/common/wallpaper_engine")
TITLE = "DriFoxCap"
FPS = 15

# 壁纸自带动态信息属性：默认关闭，否则主题里会显示录制时刻的日期/时钟/歌名
DYNAMIC_PROP_HINTS = ("date", "time", "clock", "hour", "week", "calendar",
                      "media", "spectrum", "visualizer")

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM), wt.LPARAM]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]

HWND_TOPMOST = wt.HWND(-1)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
GWL_STYLE = -16
WS_MINIMIZEBOX = 0x00020000


def set_dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def find_window(title: str) -> int | None:
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _l):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == title:
                found.append(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    return found[0] if found else None


def client_size(hwnd: int) -> tuple[int, int]:
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    return r.right - r.left, r.bottom - r.top


def we_cli(*args: str) -> None:
    subprocess.run([str(WE_DIR / "wallpaper64.exe"), *args],
                   check=False, capture_output=True)


def read_pkg(data: bytes) -> tuple[dict[str, tuple[int, int]], int]:
    count = struct.unpack_from("<I", data, 12)[0]
    off, out = 16, {}
    for _ in range(count):
        nlen = struct.unpack_from("<I", data, off)[0]
        off += 4
        name = data[off:off + nlen].decode("utf-8", "replace")
        off += nlen
        rel, sz = struct.unpack_from("<II", data, off)
        off += 8
        out[name] = (rel, sz)
    return out, off


def prepare_project(pkg: Path, workdir: Path) -> tuple[Path | None, list[str]]:
    """解包 scene.pkg 成项目目录，并把日期/时钟/媒体属性默认关掉。

    WE 支持直接加载解包目录（project.json 的 file 指向 scene.json），
    而打包态 pkg 无法改属性。返回 (project.json 路径, 提示)。
    """
    data = pkg.read_bytes()
    try:
        entries, base = read_pkg(data)
    except Exception as e:
        return None, [f"pkg 解析失败 {e}"]
    dst = workdir / "proj"
    if not (dst / "project.json").exists():
        for name, (rel, sz) in entries.items():
            p = dst / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data[base + rel: base + rel + sz])
        for extra in ("project.json", "preview.jpg"):
            src = pkg.parent / extra
            if src.exists():
                (dst / extra).write_bytes(src.read_bytes())
    pj = dst / "project.json"
    if not pj.exists():
        return None, ["解包后无 project.json"]
    proj = json.loads(pj.read_text(encoding="utf-8", errors="replace"))
    props = (proj.get("general") or {}).get("properties") or {}
    changed, notes = [], []
    for key, cfg in props.items():
        if not isinstance(cfg, dict):
            continue
        hay = (key + " " + str(cfg.get("text") or "")).lower()
        if not any(h in hay for h in DYNAMIC_PROP_HINTS):
            continue
        t = cfg.get("type")
        if t == "bool" and cfg.get("value") is not False:
            cfg["value"] = False
            changed.append(key)
        elif t == "slider":
            if cfg.get("value") not in (0, 0.0, "0", None):
                cfg["value"] = 0
                changed.append(key)
        elif t == "group":
            continue
        else:
            notes.append(f"关不掉 {key}({t})={cfg.get('value')!r}")
    if changed:
        pj.write_text(json.dumps(proj, ensure_ascii=False, indent=2), encoding="utf-8")
        notes.append("off:" + ",".join(changed))
    return pj, notes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="scene.pkg（自动解包关属性）或 project.json")
    ap.add_argument("out")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--settle", type=float, default=4.0)
    ap.add_argument("--keep-open", action="store_true")
    args = ap.parse_args(argv)

    set_dpi_aware()
    try:
        from windows_capture import WindowsCapture
    except Exception as e:
        print(f"[error] 缺少 windows-capture: {e}")
        return 2

    src = Path(args.source)
    target = src
    notes: list[str] = []
    if src.suffix.lower() == ".pkg":
        target, notes = prepare_project(src, src.parent)
        for n in notes:
            print(f"[info] {n}", flush=True)
        if target is None:
            print("[error] " + "; ".join(notes))
            return 1

    ffmpeg = shutil.which("ffmpeg") or r"D:\ffmpeg\bin\ffmpeg.exe"
    we_cli("-control", "openWallpaper", "-file", str(target.resolve()),
           "-playInWindow", TITLE, "-width", str(args.width), "-height", str(args.height))

    hwnd = None
    for _ in range(80):
        time.sleep(0.25)
        hwnd = find_window(TITLE)
        if hwnd:
            break
    if not hwnd:
        print("[error] 窗口未创建（WE 是否在运行？）")
        return 1
    time.sleep(args.settle)

    # 只置顶+去最小化按钮；WGC 本身抗遮挡，置顶只是让用户看得见进度
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    user32.SetWindowLongW(hwnd, GWL_STYLE, style & ~WS_MINIMIZEBOX)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW)
    time.sleep(0.5)

    cw, ch = client_size(hwnd)
    cw, ch = cw - cw % 2, ch - ch % 2
    if cw < 64 or ch < 64:
        print(f"[error] 窗口尺寸异常 {cw}x{ch}")
        we_cli("-control", "closeWallpaper", "-location", TITLE)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    enc = subprocess.Popen(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{cw}x{ch}",
         "-r", str(FPS), "-i", "-",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
         "-pix_fmt", "yuv420p", "-r", str(FPS), str(out)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    q: queue.Queue = queue.Queue(maxsize=3)
    stats = {"last": time.time(), "shape": None}
    crop = {"dx": None, "dy": None}

    cap = WindowsCapture(cursor_capture=False, draw_border=False,
                         window_hwnd=hwnd,
                         minimum_update_interval=max(1, int(1000 / FPS)))

    @cap.event
    def on_frame_arrived(frame, control):
        buf = frame.frame_buffer
        if crop["dx"] is None:
            # 自校准：GetWindowRect 含 DWM 阴影，与实际帧对不上；
            # 帧宽-客户区宽=左右边框（对称），帧高-客户区高-边框=标题栏
            fh, fw = buf.shape[0], buf.shape[1]
            dx = max(0, (fw - cw) // 2)
            dy = max(0, fh - ch - dx)
            crop["dx"], crop["dy"] = dx, dy
            stats["shape"] = (fh, fw)
            print(f"[ok] frame={fw}x{fh} crop=({dx},{dy})", flush=True)
        dx, dy = crop["dx"], crop["dy"]
        if buf.shape[0] < dy + ch or buf.shape[1] < dx + cw:
            return
        view = buf[dy:dy + ch, dx:dx + cw]
        if q.full():
            try:
                q.get_nowait()
            except queue.Empty:
                pass
        q.put(view.copy())
        stats["last"] = time.time()

    @cap.event
    def on_closed():
        stats["last"] = 0.0

    control = cap.start_free_threaded()
    # 先等首帧：WE 加载期间窗口不重绘，WGC 不会给帧
    t_wait = time.time()
    first = None
    while time.time() - t_wait < 12:
        try:
            first = q.get(timeout=0.5)
            break
        except queue.Empty:
            continue
    if first is None:
        try:
            control.stop()
        except Exception:
            pass
        try:
            enc.stdin.close()
        except Exception:
            pass
        enc.wait(timeout=30)
        if not args.keep_open:
            we_cli("-control", "closeWallpaper", "-location", TITLE)
        print(f"[error] 12s 内未收到任何帧 (frame={stats['shape']})")
        return 1

    t0 = time.time()
    written = 0
    stalled = False
    try:
        enc.stdin.write(first.tobytes())
        written += 1
        stats["last"] = time.time()
        while time.time() - t0 < args.duration:
            try:
                frm = q.get(timeout=0.5)
            except queue.Empty:
                if time.time() - stats["last"] > 2.5:
                    stalled = True
                    break
                continue
            enc.stdin.write(frm.tobytes())
            written += 1
    finally:
        try:
            control.stop()
        except Exception:
            pass
        try:
            enc.stdin.close()
        except Exception:
            pass
        enc.wait(timeout=60)
        if not args.keep_open:
            we_cli("-control", "closeWallpaper", "-location", TITLE)

    if stalled:
        print(f"[error] 2.5s 无新帧（窗口可能被最小化）已写 {written} 帧")
        return 1
    size_mb = out.stat().st_size / 1048576 if out.exists() else 0
    print(f"[ok] {written} 帧 {written/FPS:.1f}s -> {out} ({size_mb:.1f}MB)", flush=True)
    return 0 if written > FPS * 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""
渲染配置 → 环境变量换算（运行于 Qt 之前，纯 stdlib，禁止 import Qt / app.*）

QtWebEngine 只在进程首次初始化时读取 QTWEBENGINE_CHROMIUM_FLAGS / QT_OPENGL /
QT_ANGLE_PLATFORM，之后修改无效 —— 因此本模块由 main.py 在所有 Qt import 之前
调用：裸 JSON 读取 app.config 的 [Render] 组（刻意不走 Settings 单例，避免
qfluentwidgets 全量配置拖慢启动 / 提前加载 GUI 栈），换算成环境变量交给 Qt。

档位与默认值（与 app/utils/config.py 的 Render 配置组一一对应，均重启生效）：
- RenderBackend: **software(默认, ANGLE warp)** / hardware(ANGLE d3d11) / software_gl
  / vulkan / d3d9 / swiftshader
  - software：默认档，Qt 走 ANGLE → WARP（CPU 光栅），完全不碰显卡驱动
  - hardware：保留 GPU 进程，禁 SwiftShader 兜底（驱动异常时显式失败不静默退化）；
    默认附加 --disable-gpu-compositing（GPU 光栅 + CPU 合成，规避 Qt/Chromium
    双合成器异步交换 GPU 纹理在高频 resize 下的帧序错乱闪烁，2026-09-11）
  - software_gl：Mesa llvmpipe 桌面 GL，最慢最稳
  - vulkan / d3d9 / swiftshader：排障档，见 _ANGLE_PLATFORM 注释
  - **无 auto 档**（2026-09-10 移除）：它其实不检测机器，只是读人工放的
    ~/.drifox/software_render 标记文件 / DRIFOX_SOFTWARE_RENDER 环境变量，名不副实。
    该检测链已删除；历史配置里残留的 "auto"（以及手改的非法值、缺 key）一律按
    出厂默认 software 处理。
- WebglEnabled: auto（旧检测链：DRIFOX_ENABLE_WEBGL → ~/.drifox/webgl_enabled）/ on / off
- RendererProcessLimit / JsHeapMb / LowEndDeviceMode / SmoothScrolling /
  CanvasAA / DisableBackgroundThrottling / DisabledFeatures / ExtraChromiumFlags：
  直通 Chromium 开关（DisableBackgroundThrottling 默认关，保持 Chromium 原生节流）

兼容性契约：
- 默认行为与历史 main.py 硬编码逐字一致（无配置文件 / 缺 key / 非法值均回退）
- 外部已设 QTWEBENGINE_CHROMIUM_FLAGS / QT_OPENGL 时以外部为准（setdefault，
  调试逃生门，例如 QTWEBENGINE_CHROMIUM_FLAGS="" 即完全禁用内置开关组）
- 非 Windows 不设 QT_OPENGL / QT_ANGLE_PLATFORM：ANGLE / D3D11 / WARP 是 Windows
  概念，macOS 强设 d3d11 会导致消息卡片黑屏（2026-09-10 回归教训）

历史背景（为什么默认这组开关，勿随手删除）：
- 每张消息卡片是独立 QWebEngineView，Chromium 默认为每卡派生 renderer 进程
  （各约数十 MB），长对话滚动进程数单调增长是内存溢出主因；
  --renderer-process-limit 封顶后内存曲线从线性变恒定。不用 --process-per-site：
  与「按 PID kill 离屏 renderer」回收机制冲突（kill 一个会误伤全部卡片）。
- QT_OPENGL=angle + QT_ANGLE_PLATFORM=d3d11：绕开 Intel 核显 OpenGL ICD 在
  流式渲染高频合成期的崩溃路径（igxelpicd64.dll），同时把场景图合成交还 GPU。
  不能用 QSG_RHI_BACKEND=d3d11 替代：Qt 5.15 中 WebEngine 的 GL 纹理无法与
  RHI D3D11 合成器互操作，会整卡黑屏（已实测踩坑）。
- 软件回退 warp（纯 CPU 光栅）：驱动异常 / 虚拟机 / 远程桌面兜底；仍崩可
  software_gl 退 Mesa llvmpipe（最慢最稳）。四种后端验证见
  tests/debug/angle_backend_check.py。
"""

import json
import os
import re
import sys

__all__ = [
    "apply_render_env",
    "applied_settings",
    "build_chromium_flags",
    "compute_settings",
    "default_config_path",
    "describe_applied",
]

# 后端档位 → ANGLE 平台（QT_ANGLE_PLATFORM）。不含 software_gl：它不走 ANGLE，
# 而是 QT_OPENGL=software（Mesa llvmpipe）。
# 注：vulkan / d3d9 是排障档 —— 驱动支持不全时会黑屏，Qt 也可能静默回退默认值，
# 所以只给「显卡驱动有问题」的场景试，不要当常规选项。
_ANGLE_PLATFORM = {
    "hardware": "d3d11",
    "software": "warp",
    "vulkan": "vulkan",
    "d3d9": "d3d9",
    # Qt 侧没有 swiftshader 这个 ANGLE 平台，故 Qt 仍走 WARP；
    # Chromium 侧由 build_chromium_flags 追加 --use-angle=swiftshader
    "swiftshader": "warp",
}

# 与 app/utils/config.py Render 组默认值/范围保持一致
_DEFAULT_BACKEND = "software"  # 出厂默认：软件 (WARP)，不碰显卡驱动
_DEFAULT_RENDERER_LIMIT = 6
_RENDERER_LIMIT_RANGE = (1, 32)
_DEFAULT_JS_HEAP_MB = 128
_JS_HEAP_RANGE = (64, 1024)
_DEFAULT_DISABLED_FEATURES = "Translate,MediaRouter,optimizeHints,CalculateNativeWinOcclusion"

# apply_render_env 的留档（AA_* 类设置无处反查，见 applied_settings）
_APPLIED: dict = {}


def _detect_webgl_enabled() -> bool:
    """WebGL 解禁检测链：DRIFOX_ENABLE_WEBGL 环境变量 → ~/.drifox/webgl_enabled 标记文件。"""
    if os.environ.get("DRIFOX_ENABLE_WEBGL", "").strip().lower() in ("1", "true", "on", "yes"):
        return True
    try:
        return os.path.isfile(os.path.join(os.path.expanduser("~"), ".drifox", "webgl_enabled"))
    except Exception:
        return False


def compute_settings(render: dict) -> dict:
    """[Render] 配置组 → 渲染设置（纯函数；WebGL 检测链为模块级函数，便于测试打桩）。"""
    # 缺 key（从未设置过）/ 历史 "auto" / 手改非法值 → 出厂默认：软件 (WARP)。
    # 原 auto 检测链（DRIFOX_SOFTWARE_RENDER → ~/.drifox/software_render 标记文件）
    # 已删除：默认档本身就是最保守的软件档，那条链既没有升级空间，也不是真检测。
    backend_raw = render.get("RenderBackend", "")
    if backend_raw in _ANGLE_PLATFORM or backend_raw == "software_gl":
        backend = backend_raw
    else:
        backend = _DEFAULT_BACKEND
    # software_render 只用于语义标记：凡是走 CPU 的档位都算
    software_render = backend != "hardware"

    webgl_raw = render.get("WebglEnabled", "auto")
    if webgl_raw == "on":
        webgl = True
    elif webgl_raw == "off":
        webgl = False
    else:
        webgl = _detect_webgl_enabled()

    # GPU 开关三选一（对应 build_chromium_flags 的 GPU 段）：
    # - hardware / vulkan / d3d9：用户声明走真实 GPU → 保留 GPU 进程
    # - swiftshader：Qt 侧走 WARP、Chromium 侧走自带 CPU 光栅双保险 → GPU 进程要留着
    # - WebGL 开：--disable-gpu 会连 SwiftShader 一起禁掉 → 换 swiftshader 兜底
    # - 其余（software / software_gl 且 WebGL 关）：纯 2D 正文，禁 GPU 进程省常驻内存
    if backend in ("hardware", "vulkan", "d3d9"):
        disable_gpu, enable_swiftshader, disable_sw_rasterizer = False, False, True
    elif backend == "swiftshader" or webgl:
        disable_gpu, enable_swiftshader, disable_sw_rasterizer = False, True, False
    else:
        disable_gpu, enable_swiftshader, disable_sw_rasterizer = True, False, True

    return {
        "backend": backend,
        "software_render": software_render,
        "webgl_enabled": webgl,
        "disable_gpu": disable_gpu,
        "enable_swiftshader": enable_swiftshader,
        "disable_software_rasterizer": disable_sw_rasterizer,
        "renderer_process_limit": _to_int(
            render.get("RendererProcessLimit"), _DEFAULT_RENDERER_LIMIT, *_RENDERER_LIMIT_RANGE
        ),
        "js_heap_mb": _to_int(render.get("JsHeapMb"), _DEFAULT_JS_HEAP_MB, *_JS_HEAP_RANGE),
        # 低端设备模式：为 WARP/低配机设计的降级路径（省内存）。hardware 档默认关：
        # 真实 GPU 光栅下启用降级 tile 策略只会加剧合成错位（显式设置仍被尊重）。
        "low_end_device_mode": _to_bool(render.get("LowEndDeviceMode"), backend != "hardware"),
        "smooth_scrolling": _to_bool(render.get("SmoothScrolling"), False),
        # 共享 GL 上下文（Qt.AA_ShareOpenGLContexts）：省约 12.7% per-view 常驻内存，
        # 代价是所有卡片共用一个上下文（多卡/图表闪烁的排查开关）。默认开 = 历史行为。
        "share_gl_contexts": _to_bool(render.get("ShareGLContexts"), True),
        "canvas_aa": _to_bool(render.get("CanvasAA"), False),
        # 后台节流：默认 False（Chromium 原生行为）。长对话里离屏/后台卡片会被降
        # 优先级，表现为流式渲染卡顿掉帧；开启后关掉两类节流。
        "disable_background_throttling": _to_bool(render.get("DisableBackgroundThrottling"), False),
        # Qt.AA_UseOpenGLES：不单独暴露开关，由后端档位推导（两个旋钮管同一件事
        # 容易配出矛盾）。ANGLE 档（hardware / software）本来就是 ES，需要开；
        # software_gl 走 Mesa llvmpipe 桌面 GL，强制 ES 与「最慢最稳兜底」冲突。
        "use_open_gles": backend != "software_gl",
        "disabled_features": _to_str(render.get("DisabledFeatures"), _DEFAULT_DISABLED_FEATURES),
        "extra_flags": _to_str(render.get("ExtraChromiumFlags"), ""),
    }


def build_chromium_flags(s: dict) -> str:
    """渲染设置 → QTWEBENGINE_CHROMIUM_FLAGS。

    顺序：进程上限 → GPU 段 → 通用精简 → JS 堆 → 行为开关 → 追加 flags。
    追加的重复 flag 排在后面（Chromium 同名 flag 后者覆盖前者），可覆盖内置值。
    """
    parts = [f"--renderer-process-limit={s.get('renderer_process_limit', _DEFAULT_RENDERER_LIMIT)}"]
    if s.get("disable_gpu"):
        parts.append("--disable-gpu")
    if s.get("enable_swiftshader"):
        parts.append("--enable-unsafe-swiftshader")
    # hardware 档：禁 GPU 合成、保留 GPU 光栅。Qt(ANGLE d3d11) 与 Chromium(viz)
    # 双合成器异步交换 GPU 共享纹理，折叠框 max-height 过渡等高频 resize 场景
    # 帧提交与上屏不同步 → 内容乱闪；合成收回 CPU 后每帧完整确定，速度损失小。
    # 恢复 GPU 合成：ExtraChromiumFlags 填 --enable-gpu-compositing（后者覆盖前者）。
    if s.get("backend") == "hardware":
        parts.append("--disable-gpu-compositing")
    # SwiftShader 档：让 Chromium 用自己的 CPU 光栅（不碰显卡驱动）。
    # 只有这一档显式指定 --use-angle，其余档位交给 Qt 的 QT_ANGLE_PLATFORM。
    if s.get("backend") == "swiftshader":
        parts.append("--use-angle=swiftshader")
    # 兜底推导：直接调 build（未经 compute）时按「硬件路径且 WebGL 关」禁软件光栅
    sw_rasterizer = s.get("disable_software_rasterizer")
    if sw_rasterizer is None:
        sw_rasterizer = not s.get("software_render", False) and not s.get("webgl_enabled", False)
    if sw_rasterizer:
        parts.append("--disable-software-rasterizer")
    parts += [
        "--disable-dev-shm-usage",  # 容器/小 /dev/shm 环境下的渲染异常防御
        "--disable-extensions",  # 本地 setHtml 渲染用不到扩展
        "--disable-background-networking",  # 纯本地渲染，不需要后台网络服务
        "--disable-background-timer-throttling",  # 隐藏 tab 计时器节流会拖慢流式渲染
    ]
    parts.append(f"--js-flags=--max-old-space-size={s.get('js_heap_mb', _DEFAULT_JS_HEAP_MB)}")
    if s.get("low_end_device_mode", True):
        parts.append("--enable-low-end-device-mode")
    if not s.get("smooth_scrolling", False):
        parts.append("--disable-smooth-scrolling")
    if s.get("disable_background_throttling", False):
        # 离屏/隐藏窗口的 renderer 不再被降优先级（长会话流式卡顿的一味解药）
        parts += ["--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows"]
    features = s.get("disabled_features") or ""
    if features:
        parts.append(f"--disable-features={features}")
    if not s.get("canvas_aa", False):
        parts += ["--disable-canvas-aa", "--disable-2d-canvas-clip-aa"]
    extra = (s.get("extra_flags") or "").strip()
    if extra:
        parts.append(extra)
    return " ".join(parts)


def apply_render_env(config_path) -> dict:
    """读 app.config 的 [Render] 组并写入环境变量（main.py 启动最早期调用）。

    外部已设 QTWEBENGINE_CHROMIUM_FLAGS / QT_OPENGL 时保持 setdefault 语义，
    完全尊重外部值。返回换算后的设置（供日志/诊断用）。
    """
    s = compute_settings(_read_render_group(config_path))
    # 记录本次进程实际采用的设置：AA_UseOpenGLES / AA_ShareOpenGLContexts 是 Qt 属性
    # 而非环境变量，回显时无法从 os.environ 反查，只能从这里取（见 applied_settings）
    _APPLIED.clear()
    _APPLIED.update(s)
    # 平台限定：ANGLE / D3D11 / WARP 是 Windows 概念，非 Windows 一律不设
    # （macOS 强设 QT_ANGLE_PLATFORM=d3d11 会黑屏，2026-09-10 回归教训）
    if os.name == "nt":
        if s["backend"] == "software_gl":
            os.environ.setdefault("QT_OPENGL", "software")
        else:
            os.environ.setdefault("QT_OPENGL", "angle")
            os.environ.setdefault("QT_ANGLE_PLATFORM", _ANGLE_PLATFORM.get(s["backend"], "d3d11"))
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", build_chromium_flags(s))
    return s


def applied_settings() -> dict:
    """本次进程实际采用的渲染设置（含 Qt 属性类），供设置页回显。

    与 describe_applied 的分工：后者反查环境变量（Chromium 侧），这里给出
    apply_render_env 落地到 Qt 属性的部分 —— 它们无处反查，只能留档。
    未调用过 apply_render_env 时（单测 / 非主进程）返回空 dict。
    """
    return dict(_APPLIED)


def describe_applied(env=None) -> dict:
    """从环境变量反查「本次进程实际生效」的渲染参数（设置页回显 / 排障用）。

    刻意读环境变量而不是重算 app.config：配置改了但没重启时，回显要展示的正是
    「当前真正在跑的那组值」，与配置不一致 = 变更尚未生效，用户一眼能看出来。

    Args:
        env: 默认读 os.environ；单测可传 dict。

    Returns:
        dict: backend（hardware/software/software_gl/custom/"" 非 Windows 未设）、
        opengl、angle、flags、flag_count、renderer_process_limit、js_heap_mb。
    """
    env = os.environ if env is None else env
    flags = env.get("QTWEBENGINE_CHROMIUM_FLAGS", "") or ""
    opengl = env.get("QT_OPENGL", "") or ""
    angle = env.get("QT_ANGLE_PLATFORM", "") or ""
    # SwiftShader 档的 Qt 侧仍是 warp，只能靠 Chromium flag 认出来，故先判
    if "--use-angle=swiftshader" in flags:
        backend = "swiftshader"
    elif opengl == "software":
        backend = "software_gl"
    elif angle == "warp":
        backend = "software"
    elif angle == "d3d11":
        backend = "hardware"
    elif angle in ("vulkan", "d3d9"):
        backend = angle
    elif opengl or angle:
        backend = "custom"  # 外部环境变量改过（setdefault 逃生门）
    else:
        backend = ""  # 非 Windows：不设 ANGLE，沿用 Qt 默认
    return {
        "backend": backend,
        "opengl": opengl,
        "angle": angle,
        "flags": flags,
        "flag_count": len(flags.split()) if flags else 0,
        "renderer_process_limit": _parse_int_flag(flags, "--renderer-process-limit"),
        "js_heap_mb": _parse_int_flag(flags, "--max-old-space-size"),
        # Qt 属性类：环境变量里查不到，取 apply_render_env 的留档（默认均为开，
        # 与 main.py 历史行为一致）
        "share_gl_contexts": bool(_APPLIED.get("share_gl_contexts", True)),
        "use_open_gles": bool(_APPLIED.get("use_open_gles", True)),
    }


def _parse_int_flag(flags: str, name: str) -> int:
    """从 flags 串解析 ``--name=123``；命中不到返回 0。

    ``--max-old-space-size`` 嵌在 ``--js-flags=--max-old-space-size=128`` 里，
    直接子串匹配即可，无需按 flag 边界切分。
    """
    m = re.search(re.escape(name) + r"=(\d+)", flags or "")
    return int(m.group(1)) if m else 0


def _read_render_group(config_path) -> dict:
    """裸 JSON 读取 [Render] 组；文件缺失 / JSON 损坏 / 结构不对一律回退空配置。"""
    try:
        with open(config_path, "rb") as f:
            data = json.loads(f.read())
    except Exception:
        return {}
    group = data.get("Render") if isinstance(data, dict) else None
    return group if isinstance(group, dict) else {}


def default_config_path() -> str:
    """app.config 路径，与 app.utils.utils.get_app_data_dir 对齐。

    不直接 import 它：该模块顶层拖 PyQt5 / Settings，而本模块必须能在
    Qt 加载之前独立运行。
    """
    if getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"):
        if sys.platform == "darwin":
            base = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Drifox", ".drifox")
        else:
            base = os.path.join(os.path.expanduser("~"), ".drifox")
    else:
        base = os.path.join(".drifox")
    return os.path.join(base, "app.config")


def _to_int(raw, default: int, lo: int, hi: int) -> int:
    """整型解析 + 范围钳制；bool 是 int 子类需排除，非法值回退默认。"""
    if isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except TypeError, ValueError:
        return default
    return max(lo, min(hi, value))


def _to_bool(raw, default: bool) -> bool:
    """严格布尔：非 bool 类型（"yes"/"no"/1 等垃圾值）一律回退默认。"""
    return raw if isinstance(raw, bool) else default


def _to_str(raw, default: str) -> str:
    """字符串解析 + 去首尾空白；非字符串回退默认。"""
    if not isinstance(raw, str):
        return default
    return raw.strip()

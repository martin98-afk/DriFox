# -*- coding: utf-8 -*-
import os
import platform
import shutil
from pathlib import Path

import PyInstaller.__main__
from PyInstaller.utils.hooks import collect_submodules

# 1. 基础路径配置
base_dir = os.path.dirname(os.path.abspath(__file__))
extra_modules = [
]
# 跨平台共享的冗余库（Python 包）
to_remove_common = [
    "scipy",
    "cv2",
    "cryptography",
    "pandas",
    "pyarrow",
    "jieba",
    "scipy.libs",
    "sphinx",
    "matplotlib",
    "torch",
    "tensorflow",
    "torchaudio",
    "sqlalchemy",
    "PIL",
    "ast_serialize",
    "websockets",
    "PySide6/translations",
]
# Linux 专用（.so 文件）—— PySide6 wheel 内 Qt 库位于 PySide6/Qt/lib（Qt5 时代为 PyQt5/Qt5/lib，库名 Qt5→Qt6）
to_remove_linux = [
    "PySide6/Qt/lib/libQt6Bluetooth.so*",
    "PySide6/Qt/lib/libQt6Nfc.so*",
    "PySide6/Qt/lib/libQt6RemoteObjects.so*",
    "PySide6/Qt/lib/libQt6Sensors.so*",
    "PySide6/Qt/lib/libQt6SerialPort.so*",
    "PySide6/Qt/lib/libQt6Sql.so*",
    "PySide6/Qt/lib/libQt6Test.so*",
    "PySide6/Qt/lib/libQt6WebSockets.so*",
    # Qt6 已移除 QtXmlPatterns
    "PySide6/Qt/lib/libQt6Quick3D*.so*",
    "PySide6/Qt/lib/libQt6QuickParticles.so*",
    "PySide6/Qt/lib/libQt6QuickTest.so*",
    "PySide6/Qt/lib/libQt6QuickTemplates2.so*",
    "PySide6/Qt/lib/libQt6QuickShapes.so*",
    "PySide6/Qt/lib/libQt6Location.so*",
    "PySide6/Qt/lib/libQt6Multimedia.so*",
    "PySide6/Qt/qml/QtQuick",
    "PySide6/Qt/qml/QtQuick3D",
    "PySide6/Qt/qml/QtBluetooth",
    "PySide6/Qt/qml/QtRemoteObjects",
    "PySide6/Qt/qml/QtSensors",
    "PySide6/Qt/qml/QtTest",
    "PySide6/Qt/qml/QtWebSockets",
    "PySide6/Qt/qml/QtQuick.2",
    "PySide6/Qt/qml/QtLocation",
    "PySide6/Qt/qml/QtNfc",
    # QtPositioning 保留 —— QtWebEngine 依赖
    # "PySide6/Qt/qml/QtPositioning",
    "PySide6/Qt/qml/QtMultimedia",
    # Qt6 已移除 QtAudioEngine
    "PySide6/Qt/resources/qtwebengine_devtools_resources.pak",
    "PySide6/Qt/resources/qtwebengine_resources_200p.pak",
    "PySide6/Qt/resources/qtwebengine_resources_100p.pak",
]
to_remove_windows = [
    "numpy",
    # Qt6 无 libGLESv2.dll，删去；软件光栅化与 d3dcompiler 仍随 PySide6 分发
    "PySide6/opengl32sw.dll",
    "PySide6/d3dcompiler_47.dll",
    "PySide6/Qt6Multimedia.dll",
    "PySide6/Qt6Test.dll",
    # 纯 widgets 应用不需要任何 QML（Qt6 WebEngine widgets 模式同样不依赖 qml/）
    "PySide6/qml",
    "PySide6/resources/qtwebengine_devtools_resources.pak",
    "PySide6/resources/qtwebengine_resources_200p.pak",
    "PySide6/resources/qtwebengine_resources_100p.pak",
]
# macOS 专用（.framework 目录）—— PySide6 wheel 内位于 PySide6/Qt/lib
to_remove_macos = [
    # Qt lib 下的 .framework 目录
    "PySide6/Qt/lib/QtBluetooth.framework",
    "PySide6/Qt/lib/QtRemoteObjects.framework",
    "PySide6/Qt/lib/QtSensors.framework",
    "PySide6/Qt/lib/QtTest.framework",
    "PySide6/Qt/lib/QtWebSockets.framework",
    "PySide6/Qt/lib/QtNfc.framework",
    "PySide6/Qt/lib/QtSerialPort.framework",
    "PySide6/Qt/lib/QtSql.framework",
    # Qt6 已移除 QtXmlPatterns
    "PySide6/Qt/lib/QtQuick3D.framework",
    "PySide6/Qt/lib/QtQuick3DAssetImport.framework",
    "PySide6/Qt/lib/QtQuick3DRender.framework",
    "PySide6/Qt/lib/QtQuick3DRuntimeRender.framework",
    "PySide6/Qt/lib/QtQuick3DUtils.framework",
    "PySide6/Qt/lib/QtQuickParticles.framework",
    "PySide6/Qt/lib/QtQuickTest.framework",
    "PySide6/Qt/lib/QtQuickTemplates2.framework",
    "PySide6/Qt/lib/QtQuickShapes.framework",
    "PySide6/Qt/lib/QtLocation.framework",
    "PySide6/Qt/lib/QtMultimedia.framework",
    # QtPositioning 保留 —— QtWebEngine 依赖 @rpath/QtPositioning
    # "PySide6/Qt/lib/QtPositioning.framework",
    # qml 目录（对应 Windows 的 qml 清理项）
    "PySide6/Qt/qml/QtQuick",
    "PySide6/Qt/qml/QtQuick3D",
    "PySide6/Qt/qml/QtBluetooth",
    "PySide6/Qt/qml/QtRemoteObjects",
    "PySide6/Qt/qml/QtSensors",
    "PySide6/Qt/qml/QtTest",
    "PySide6/Qt/qml/QtWebSockets",
    "PySide6/Qt/qml/QtQuick.2",
    "PySide6/Qt/qml/QtLocation",
    "PySide6/Qt/qml/QtNfc",
    # QtPositioning 保留 —— QtWebEngine 依赖
    # "PySide6/Qt/qml/QtPositioning",
    "PySide6/Qt/qml/QtMultimedia",
    # Qt6 已移除 QtAudioEngine
    # Resources 根目录下的 Qt 符号链接/可执行文件
    "QtBluetooth",
    "QtQuick3D",
    "QtRemoteObjects",
    "QtSensors",
    "QtTest",
    "QtWebSockets",
    "QtNfc",
    "QtLocation",
    "QtSerialPort",
    "QtSql",
]
# 根据平台组装最终列表
if platform.system() == "Darwin":
    to_remove = to_remove_common + to_remove_macos
elif platform.system() == "Linux":
    to_remove = to_remove_common + to_remove_linux
else:
    to_remove = to_remove_common + to_remove_windows
# 2. 图标选择 (跨平台)
icon_arg = None
if platform.system() == "Windows":
    icon_path = Path(base_dir) / "icons" / "drifox.ico"
    if icon_path.exists():
        icon_arg = f"--icon={icon_path}"
elif platform.system() == "Darwin":
    icon_path = Path(base_dir) / "icons" / "drifox.ico"
    if icon_path.exists():
        icon_arg = f"--icon={icon_path}"
# Linux: PyInstaller 默认无图标，也可使用 PNG 图标；当前跳过

# 需显式声明的隐藏导入（因 importlib.import_module() 动态加载而无法被 PyInstaller 自动检测）
_hidden_imports = [
    # app.core 全量子模块：PEP 562 懒加载（__getattr__ + importlib.import_module 动态字符串导入），
    # PyInstaller 静态分析无法发现。collect_submodules 一次性收集，后续新增懒加载模块也自动覆盖。
    *collect_submodules("app.core"),
    # gateway adapter 模块（也是 importlib.import_module 动态加载）
    "app.gateway.adapters.wecom",
    "app.gateway.adapters.dingtalk",
    "app.gateway.adapters.telegram",
    "app.gateway.adapters.discord",
    "app.gateway.adapters.feishu",
    "app.gateway.adapters.extra",
    # system 插件工具（plugins/system/tools/*.py 运行时加载）引用的 app 内模块
    "app.tools.task_state",
    "app.tools.process_job",
    "app.tools.bg_manager",
    # 插件 SDK（app/plugins/sdk.py）：仅被插件源文件 import，主代码零静态引用，
    # PyInstaller 分析不到，漏打会导致 system-transports/stream-sinks 加载失败
    "app.plugins.sdk",
    # system 插件引用的第三方包
    "html2text",
    "bs4",
    # 插件源文件（运行时动态加载，PyInstaller 不分析其依赖）引用的标准库：
    # assistant_hub/core/persona.py import getpass，漏打会导致人格卡片全空
    "getpass",
    # keyring：后端发现走 entry points，PyInstaller 收不齐会报
    # "No recommended backend was available"（jaraco/keyring #439/#468），
    # 按平台显式声明后端模块；运行时找不到后端则应用侧自动降级明文（fail-open）
    "keyring",
    *(
        {
            "Windows": ["keyring.backends.Windows", "win32cred", "pywintypes"],
            "Darwin": ["keyring.backends.macOS"],
        }.get(platform.system(), ["keyring.backends.chainer"])
    ),
    # pypinyin：history-manager 插件历史卡片拼音排序用。主代码零静态引用
    # （插件源文件运行时动态加载，PyInstaller 不分析插件依赖），漏打会导致
    # 插件 import pypinyin 失败、拼音排序静默降级
    "pypinyin",
]

# 打包排除：由插件自包含 deps/ 提供（codegraph-tools / desktop-automation），
# 不打进主程序，运行时靠插件 sys.path 注入加载
_exclude_modules = [
    "codegraph",
    "click",
    "pynput",
    "mss",
    "six",
    "colorama",
    # 开发/测试工具：仅 dev 依赖组安装，运行时零引用（诊断工具走系统 PATH 的
    # 外部可执行文件，不走这些包）。不排会被 PyInstaller 收进 _internal，
    # 实测曾把 mypy（0.74MB，black 经 mypy_extensions 连带）打进发行包。
    # ⚠️ 不要排 pyright：plugins/system-tools/tools/diagnostics_tools.py 有
    # `from pyright import cli`（try/except 保护，装了就走模块调用），排掉会让
    # get_diagnostics 在装了该包的用户机上永久降级到外部命令回退路径。
    "mypy",
    "mypy_extensions",
    "mypyc",
    "pytest",
    "_pytest",
    "pympler",
    "black",
    "coverage",
    "nox",
    "tox",
]

# 3. 构造参数列表
params = [
    "main.py",
    "--onedir",
    "--windowed",
    "--name=Drifox",  # 直接指定名称，省去后期改名麻烦
    # 数据文件包含
    f"--add-data=plugins{os.pathsep}plugins",
    # mermaid vendor：polyfill 无 CDN 降级（Chromium 83 必需），mermaid 降级 jsdelivr 国内不稳
    f"--add-data=app/resources/web/vendor/chromium83-polyfill.js{os.pathsep}app/resources/web/vendor",
    f"--add-data=app/resources/web/vendor/mermaid.min.js{os.pathsep}app/resources/web/vendor",
    # KaTeX 公式渲染 vendor：整目录收集（js + css + fonts/，css 相对路径引字体）
    f"--add-data=app/resources/web/vendor/katex{os.pathsep}app/resources/web/vendor",
    # 隐藏导入：gateway adapter 模块（importlib.import_module 动态加载，PyInstaller 无法自动发现）
    *[f"--hidden-import={m}" for m in _hidden_imports],
    # 排除插件自包含依赖（由插件 deps/ 目录运行时提供）
    *[f"--exclude-module={m}" for m in _exclude_modules],
]

if icon_arg:
    params.append(icon_arg)

# macOS: 指定单一架构，避免 universal2 体积膨胀
if platform.system() == "Darwin":
    arch = os.environ.get("PYINSTALLER_ARCH", "").strip() or platform.machine()
    # PyInstaller 期望的值一般是 "arm64" 或 "x86_64"
    if arch not in ("arm64", "x86_64"):
        arch = "arm64"
    params.append(f"--target-arch={arch}")


# 运行时配置
params.extend(
    [
        "--clean",
        "--noconfirm",
    ]
)


def _cleanup_targets(dist_path):
    """获取需要执行精简的目录列表
    
    macOS: .app/Contents/Resources + _internal（两处都有冗余文件）
    Windows/Linux: _internal（或 dist_path 自身）
    """
    targets = []
    app_path = dist_path + ".app"

    if platform.system() == "Darwin" and os.path.isdir(app_path):
        resources = os.path.join(app_path, "Contents", "Frameworks")
        if os.path.isdir(resources):
            targets.append(resources)
        # macOS 下 _internal 仍存在，包含 PIL 等 Python 包
        internal = os.path.join(dist_path, "_internal")
        if os.path.isdir(internal):
            targets.append(internal)
    else:
        internal = os.path.join(dist_path, "_internal")
        targets.append(internal if os.path.isdir(internal) else dist_path)

    return targets


def _remove_items(root, items):
    """在 root 下移除 items 列表中的条目

    支持 glob 通配符（如 '*.so*'），用于 Linux 平台处理带版本号的 .so 文件。
    """
    import glob as _glob

    for folder in items:
        # 检查是否包含通配符
        if "*" in folder or "?" in folder:
            pattern = os.path.join(root, folder)
            matches = _glob.glob(pattern)
            if not matches:
                continue
            for target in matches:
                try:
                    if os.path.isfile(target) or os.path.islink(target):
                        os.unlink(target)
                    elif os.path.isdir(target):
                        shutil.rmtree(target)
                    print(f"  [Cleanup] removed: {os.path.relpath(target, root)}")
                except Exception as e:
                    print(f"  [Cleanup] remove {os.path.relpath(target, root)} failed: {e}")
        else:
            target = os.path.join(root, folder)
            if os.path.exists(target) or os.path.islink(target):
                try:
                    if os.path.isfile(target) or os.path.islink(target):
                        os.unlink(target)
                    else:
                        shutil.rmtree(target)
                    print(f"  [Cleanup] removed: {folder}")
                except Exception as e:
                    print(f"  [Cleanup] remove {folder} failed: {e}")


_MAC_INFO_PLIST = {
    # macOS TCC 麦克风权限描述。缺此键时系统不弹授权弹窗、直接拒绝请求，
    # 录音流程能跑通但全程为数字静音（peak=0），表现为「点了没反应」。
    # 插件 voice-input 的 macOS 录音后端（AVFoundation）依赖本声明。
    "NSMicrophoneUsageDescription": (
        "DriFox 需要使用麦克风录制您的语音，用于语音听写功能"
        "（录音文件仅用于您配置的语音识别服务转文字）。"
    ),
}


def inject_macos_info_plist(dist_path):
    """把 macOS 权限描述写入 .app 的 Info.plist（仅 Darwin 生效）。

    PyInstaller 的 BUNDLE 在 assemble 阶段才写 Info.plist 并随即 adhoc 签名，
    CLI 参数表没有 info_plist 入口，故此处后置重写 plist 并重签。

    重签必须递归处理内层二进制：Contents/Frameworks 下游有 Qt framework、
    QtWebEngineProcess 等可执行体，只签外层会使签名与内层不一致。
    不做重签则签名密封（CodeResources）与 Info.plist 实际内容不符，
    启动时 Gatekeeper 可能直接拒绝。
    """
    if platform.system() != "Darwin":
        return
    import plistlib
    import subprocess

    app_path = dist_path + ".app"
    if not os.path.isdir(app_path):
        print(f"  [Plist] 跳过：未找到 {app_path}")
        return
    plist_path = os.path.join(app_path, "Contents", "Info.plist")
    if not os.path.isfile(plist_path):
        print(f"  [Plist] 跳过：未找到 {plist_path}")
        return

    with open(plist_path, "rb") as fh:
        info = plistlib.load(fh)
    changed = {k: v for k, v in _MAC_INFO_PLIST.items() if info.get(k) != v}
    if not changed:
        print("  [Plist] 权限描述已存在，无需改动")
        return
    info.update(changed)
    with open(plist_path, "wb") as fh:
        plistlib.dump(info, fh)
    for key in changed:
        print(f"  [Plist] 已写入 {key}")

    # 重签：与 PyInstaller 默认一致走 adhoc（identity=None → '-'）
    result = subprocess.run(
        ["/usr/bin/codesign", "-s", "-", "--force", "--deep", app_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
    )
    if result.returncode:
        print(f"  [Plist] 警告：重签失败（退出码 {result.returncode}）")
        print(f"  [Plist] {result.stdout.strip()[:500]}")
    else:
        print("  [Plist] 已重签 .app（adhoc，deep）")


def post_build_cleanup(dist_path):
    """Post-build cleanup to reduce package size"""
    targets = _cleanup_targets(dist_path)

    print("[Build] Compacting package size...")
    for t in targets:
        # 判断当前目标类型，选用合适的移除列表
        is_resources = t.endswith("Frameworks")
        if is_resources:
            items = to_remove_common + to_remove_macos
        elif platform.system() == "Linux":
            items = to_remove_common + to_remove_linux
        elif platform.system() == "Darwin":
            items = to_remove_common
        else:
            items = to_remove_common + to_remove_windows
        _remove_items(t, items)

        # 清理缓存
        for root, dirs, files in os.walk(t):
            for d in dirs:
                if d in (".mypy_cache", "__pycache__"):
                    try:
                        shutil.rmtree(os.path.join(root, d))
                        print(f"  [Cleanup] removed cache: {os.path.join(root, d)}")
                    except Exception as e:
                        print(f"  [Cleanup] remove cache {d} failed: {e}")


if __name__ == "__main__":
    print("[Build] Starting PyInstaller build for Drifox...")

    # 执行打包
    PyInstaller.__main__.run(params)

    # 4. 后置处理
    dist_final = os.path.join("dist", "Drifox")
    if os.path.exists(dist_final):
        # 顺序不可颠倒：先做体积精简（删文件），再注入 plist 并重签。
        # 若先重签再删文件，签名密封（CodeResources）会与实际内容不符而失效。
        post_build_cleanup(dist_final)
        inject_macos_info_plist(dist_final)

    print("[Build] Build completed successfully!")


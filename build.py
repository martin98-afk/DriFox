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
    "PyQt5/Qt5/translations",
]
# Linux 专用（.so 文件）
to_remove_linux = [
    "PyQt5/Qt5/lib/libQt5Bluetooth.so*",
    "PyQt5/Qt5/lib/libQt5Nfc.so*",
    "PyQt5/Qt5/lib/libQt5RemoteObjects.so*",
    "PyQt5/Qt5/lib/libQt5Sensors.so*",
    "PyQt5/Qt5/lib/libQt5SerialPort.so*",
    "PyQt5/Qt5/lib/libQt5Sql.so*",
    "PyQt5/Qt5/lib/libQt5Test.so*",
    "PyQt5/Qt5/lib/libQt5WebSockets.so*",
    "PyQt5/Qt5/lib/libQt5XmlPatterns.so*",
    "PyQt5/Qt5/lib/libQt5Quick3D*.so*",
    "PyQt5/Qt5/lib/libQt5QuickParticles.so*",
    "PyQt5/Qt5/lib/libQt5QuickTest.so*",
    "PyQt5/Qt5/lib/libQt5QuickTemplates2.so*",
    "PyQt5/Qt5/lib/libQt5QuickShapes.so*",
    "PyQt5/Qt5/lib/libQt5Location.so*",
    "PyQt5/Qt5/lib/libQt5Multimedia.so*",
    "PyQt5/Qt5/lib/libQt5Positioning.so*",
    "PyQt5/Qt5/qml/QtQuick",
    "PyQt5/Qt5/qml/QtQuick3D",
    "PyQt5/Qt5/qml/QtBluetooth",
    "PyQt5/Qt5/qml/QtRemoteObjects",
    "PyQt5/Qt5/qml/QtSensors",
    "PyQt5/Qt5/qml/QtTest",
    "PyQt5/Qt5/qml/QtWebSockets",
    "PyQt5/Qt5/qml/QtQuick.2",
    "PyQt5/Qt5/qml/QtLocation",
    "PyQt5/Qt5/qml/QtNfc",
    "PyQt5/Qt5/qml/QtPositioning",
    "PyQt5/Qt5/qml/QtMultimedia",
    "PyQt5/Qt5/qml/QtAudioEngine",
    "PyQt5/Qt5/resources/qtwebengine_devtools_resources.pak",
    "PyQt5/Qt5/resources/qtwebengine_resources_200p.pak",
    "PyQt5/Qt5/resources/qtwebengine_resources_100p.pak",
]
# Windows 专用（.dll 文件）
to_remove_windows = [
    "numpy",
    "PyQt5/Qt5/bin/libGLESv2.dll",
    "PyQt5/Qt5/bin/opengl32sw.dll",
    "PyQt5/Qt5/bin/d3dcompiler_47.dll",
    "PyQt5/Qt5/bin/Qt5Quick3D.dll",
    "PyQt5/Qt5/bin/Qt5Quick3DAssetImport.dll",
    "PyQt5/Qt5/bin/Qt5Quick3DRuntimeRender.dll",
    "PyQt5/Qt5/bin/Qt5Quick3DRender.dll",
    "PyQt5/Qt5/bin/Qt5Quick3DUtils.dll",
    "PyQt5/Qt5/bin/Qt5RemoteObjects.dll",
    "PyQt5/Qt5/bin/Qt5Sensors.dll",
    "PyQt5/Qt5/bin/Qt5QuickTest.dll",
    "PyQt5/Qt5/bin/Qt5QuickTemplates2.dll",
    "PyQt5/Qt5/bin/Qt5QuickShapes.dll",
    "PyQt5/Qt5/bin/Qt5QuickParticles.dll",
    "PyQt5/Qt5/bin/Qt5QuickControls2.dll",
    "PyQt5/Qt5/bin/Qt5Bluetooth.dll",
    "PyQt5/Qt5/bin/Qt5Location.dll",
    "PyQt5/Qt5/qml/QtQuick",
    "PyQt5/Qt5/qml/QtQuick3D",
    "PyQt5/Qt5/qml/QtBluetooth",
    "PyQt5/Qt5/qml/QtRemoteObjects",
    "PyQt5/Qt5/qml/QtSensors",
    "PyQt5/Qt5/qml/QtTest",
    "PyQt5/Qt5/qml/QtWebSockets",
    "PyQt5/Qt5/qml/QtQuick.2",
    "PyQt5/Qt5/resources/qtwebengine_devtools_resources.pak",
    "PyQt5/Qt5/resources/qtwebengine_resources_200p.pak",
    "PyQt5/Qt5/resources/qtwebengine_resources_100p.pak",
]
# macOS 专用（.framework 目录）
to_remove_macos = [
    # Qt lib 下的 .framework 目录
    "PyQt5/Qt5/lib/QtBluetooth.framework",
    "PyQt5/Qt5/lib/QtRemoteObjects.framework",
    "PyQt5/Qt5/lib/QtSensors.framework",
    "PyQt5/Qt5/lib/QtTest.framework",
    "PyQt5/Qt5/lib/QtWebSockets.framework",
    "PyQt5/Qt5/lib/QtNfc.framework",
    "PyQt5/Qt5/lib/QtSerialPort.framework",
    "PyQt5/Qt5/lib/QtSql.framework",
    "PyQt5/Qt5/lib/QtXmlPatterns.framework",
    "PyQt5/Qt5/lib/QtQuick3D.framework",
    "PyQt5/Qt5/lib/QtQuick3DAssetImport.framework",
    "PyQt5/Qt5/lib/QtQuick3DRender.framework",
    "PyQt5/Qt5/lib/QtQuick3DRuntimeRender.framework",
    "PyQt5/Qt5/lib/QtQuick3DUtils.framework",
    "PyQt5/Qt5/lib/QtQuickParticles.framework",
    "PyQt5/Qt5/lib/QtQuickTest.framework",
    "PyQt5/Qt5/lib/QtQuickTemplates2.framework",
    "PyQt5/Qt5/lib/QtQuickShapes.framework",
    "PyQt5/Qt5/lib/QtLocation.framework",
    "PyQt5/Qt5/lib/QtMultimedia.framework",
    # QtPositioning 保留 —— QtWebEngineWidgets.abi3.so 依赖 @rpath/QtPositioning
    # "PyQt5/Qt5/lib/QtPositioning.framework",
    # qml 目录（对应 Windows 的 qml 清理项）
    "PyQt5/Qt5/qml/QtQuick",
    "PyQt5/Qt5/qml/QtQuick3D",
    "PyQt5/Qt5/qml/QtBluetooth",
    "PyQt5/Qt5/qml/QtRemoteObjects",
    "PyQt5/Qt5/qml/QtSensors",
    "PyQt5/Qt5/qml/QtTest",
    "PyQt5/Qt5/qml/QtWebSockets",
    "PyQt5/Qt5/qml/QtQuick.2",
    "PyQt5/Qt5/qml/QtLocation",
    "PyQt5/Qt5/qml/QtNfc",
    "PyQt5/Qt5/qml/QtPositioning",
    "PyQt5/Qt5/qml/QtMultimedia",
    "PyQt5/Qt5/qml/QtAudioEngine",
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
    "QtXmlPatterns",
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
    # system 插件引用的第三方包
    "html2text",
    "bs4",
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
]

# 3. 构造参数列表
params = [
    "main.py",
    "--onedir",
    "--windowed",
    "--name=Drifox",  # 直接指定名称，省去后期改名麻烦
    # 数据文件包含
    f"--add-data=plugins{os.pathsep}plugins",
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
        post_build_cleanup(dist_final)

    print("[Build] Build completed successfully!")


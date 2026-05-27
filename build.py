# -*- coding: utf-8 -*-
import os
import shutil
import platform
from pathlib import Path

import PyInstaller.__main__

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
    "PyQt5/Qt5/bin/libcrypto-1_1-x64.dll",
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
    "PyQt5/Qt5/lib/QtPositioning.framework",
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

# 3. 构造参数列表
params = [
    "main.py",
    "--onedir",
    "--windowed",
    "--name=Drifox",  # 直接指定名称，省去后期改名麻烦
    # 数据文件包含
    f"--add-data=plugins{os.pathsep}plugins",
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
    """在 root 下移除 items 列表中的条目"""
    for folder in items:
        target = os.path.join(root, folder)
        if os.path.exists(target) or os.path.islink(target):
            try:
                if os.path.isfile(target) or os.path.islink(target):
                    os.unlink(target)
                else:
                    shutil.rmtree(target)
                print(f"  - 已移除: {folder}")
            except Exception as e:
                print(f"  - 移除 {folder} 失败: {e}")


def post_build_cleanup(dist_path):
    """打包后的精简逻辑"""
    targets = _cleanup_targets(dist_path)

    print("正在精简打包体积...")
    for t in targets:
        # 判断当前目标类型，选用合适的移除列表
        is_resources = t.endswith("Fframeworks")
        if is_resources:
            items = to_remove_common + to_remove_macos
        else:
            items = to_remove_common + (to_remove_windows if platform.system() != "Darwin" else [])
        _remove_items(t, items)

        # 清理缓存
        for root, dirs, files in os.walk(t):
            for d in dirs:
                if d in (".mypy_cache", "__pycache__"):
                    try:
                        shutil.rmtree(os.path.join(root, d))
                        print(f"  - 已移除: {os.path.join(root, d)}")
                    except Exception as e:
                        print(f"  - 移除 {d} 失败: {e}")


if __name__ == "__main__":
    print(f"Starting build for CanvasMind...")

    # 执行打包
    PyInstaller.__main__.run(params)

    # 4. 后置处理
    dist_final = os.path.join("dist", "Drifox")
    if os.path.exists(dist_final):
        post_build_cleanup(dist_final)

    print("\n✅ 打包任务顺利完成！")


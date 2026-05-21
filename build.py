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
# 需要删除的冗余库列表
to_remove = [
    "scipy",
    "numpy",
    "pandas",
    "cv2",
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
    "PyQt5/Qt5/translations",
    "ast_serialize",
    "websockets",
    "PyQt5/Qt5/qml/QtQuick3D",
    "PyQt5/Qt5/qml/QtBluetooth",
    "PyQt5/Qt5/qml/QtRemoteObjects",
    "PyQt5/Qt5/qml/QtSensors",
    "PyQt5/Qt5/qml/QtTest",
    "PyQt5/Qt5/qml/QtWebSockets",
    "PyQt5/Qt5/qml/QtQuick.2",
    "PyQt5/Qt5/qml/QtQuick3D",
    "watchfiles",
    # "PyQt5/Qt5/bin/opengl32sw.dll",
    "PyQt5/Qt5/bin/libGLESv2.dll",
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
    "PyQt5/Qt5/resources/qtwebengine_devtools_resources.pak",
    "PyQt5/Qt5/resources/qtwebengine_resources_200p.pak",
    "PyQt5/Qt5/resources/qtwebengine_resources_100p.pak",
]
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
    f"--add-data=app/agents{os.pathsep}app/agents",
    f"--add-data=app/skills{os.pathsep}app/skills",
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


def post_build_cleanup(dist_path):
    """打包后的精简逻辑"""
    internal_path = os.path.join(dist_path, "_internal")
    if not os.path.exists(internal_path):
        # 兼容不同版本的打包结构
        internal_path = dist_path

    print("正在精简打包体积...")
    for folder in to_remove:
        target = os.path.join(internal_path, folder)
        if os.path.exists(target):
            try:
                if os.path.isfile(target):
                    os.remove(target)
                else:
                    shutil.rmtree(target)
                print(f"  - 已移除: {folder}")
            except Exception as e:
                print(f"  - 移除 {folder} 失败: {e}")

    for root, dirs, files in os.walk(internal_path):
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


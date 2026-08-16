# -*- coding: utf-8 -*-
"""cx_Freeze 打包脚本（试用版，不替换 build.py 的 PyInstaller 流程）

用法：
    python build_cxfreeze.py

输出到 dist/cxfreeze/Drifox/（与 PyInstaller 的 dist/Drifox 互不干扰）
"""
import os
import platform

from cx_Freeze import Executable, Freezer

base_dir = os.path.dirname(os.path.abspath(__file__))

# ---- 入口与基础信息 ----
target_name = "Drifox"
icon_arg = None
if platform.system() == "Windows":
    icon_path = os.path.join(base_dir, "icons", "drifox.ico")
    if os.path.exists(icon_path):
        icon_arg = icon_path

base = "gui" if platform.system() == "Windows" else None

# ---- 隐藏导入：importlib.import_module 动态加载的模块（cx_Freeze 同样无法自动发现）----
includes = [
    "app.core.backend",
    "app.gateway.adapters.wecom",
    "app.gateway.adapters.dingtalk",
    "app.gateway.adapters.telegram",
    "app.gateway.adapters.discord",
    "app.gateway.adapters.feishu",
    "app.gateway.adapters.extra",
    # markdown 动态扩展（entry points 方式加载，静态分析无法发现）
    "markdown.extensions.fenced_code",
    "markdown.extensions.nl2br",
    "markdown.extensions.tables",
    "markdown.extensions.codehilite",
    # 插件代码（plugins/*.py 运行时加载）引用的第三方包
    "html2text",
    "bs4",
]

# 插件代码动态引用大量 app.* 子模块（task_state/process_job/bg_manager/result/registry 等），
# 逐个 include 不可维护 → 用 packages 全量收集 app 包
packages = ["app"]

# ---- 数据文件：plugins 插件目录 ----
# cx_Freeze 把代码放 lib/ 下，__file__ = lib/app/core/...
# 系统插件路径按 Path(__file__).parent.parent.parent/plugins 推导 → 必须复制到 lib/plugins
include_files = [
    (os.path.join(base_dir, "plugins"), "lib/plugins"),
]

executables = [
    Executable(
        "main.py",
        base=base,
        target_name=target_name,
        icon=icon_arg,
    )
]

# 打包排除：以下包由插件自包含 deps/ 提供（codegraph-tools / desktop-automation），
# 不打进主程序，运行时靠插件 sys.path 注入加载
excludes = [
    "codegraph",
    "click",
    "pynput",
    "mss",
    "six",
    "colorama",
]
target_dir = os.path.join(base_dir, "dist", "cxfreeze", target_name)

freezer = Freezer(
    executables,
    includes=includes,
    packages=packages,
    excludes=excludes,
    include_files=include_files,
    target_dir=target_dir,
    include_msvcr=True,
    silent=0,
)

if __name__ == "__main__":
    print(f"[Build] Starting cx_Freeze build for {target_name}...")
    freezer.freeze()
    print(f"[Build] Build completed -> {target_dir}")

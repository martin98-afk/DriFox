# -*- coding: utf-8 -*-
"""
创建 Drifox DMG 安装包
可通过环境变量指定：
  - DMG_VERSION: 主版本号（如 v0.5.6）
  - DMG_SUFFIX:  分支后缀（qt5 / pyside6 / dev）
产物命名：Drifox-macOS-{version}-{suffix}.dmg（dev 后缀省略）
"""
import os
import dmgbuild

def build_dmg():
    app_path = "dist/Drifox.app"

    version = os.environ.get("DMG_VERSION", "").strip()
    suffix = os.environ.get("DMG_SUFFIX", "dev").strip() or "dev"

    # 版本号段
    ver_part = f"-{version}" if version else ""
    # 后缀段（dev 省略，保持历史文件名兼容）
    suffix_part = "" if suffix == "dev" else f"-{suffix}"
    dmg_path = f"dist/Drifox-macOS{ver_part}{suffix_part}.dmg"

    # 删除旧的 dmg
    if os.path.exists(dmg_path):
        os.remove(dmg_path)

    # DMG 配置
    settings = {
        "format": "ULFO",
        "compression_level": 9,
        "files": [
            (app_path, "Drifox.app"),
        ],
        "symlinks": {
            "Applications": "/Applications",
        },
        "volume_name": "Drifox",
        "volume_icon_file": None,
    }

    print(f"正在创建 DMG: {dmg_path}")
    dmgbuild.build_dmg(
        dmg_path,
        "Drifox",
        settings=settings,
        # 不使用符号链接
        detach_retries=5,
    )
    print(f"✅ DMG 创建完成: {dmg_path}")

if __name__ == "__main__":
    build_dmg()

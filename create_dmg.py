# -*- coding: utf-8 -*-
"""
创建 Drifox DMG 安装包
可通过环境变量 DMG_VERSION 指定版本号（如 v0.2.10），输出为 Drifox-$tag.dmg
"""
import os
import dmgbuild

def build_dmg():
    app_path = "dist/Drifox.app"

    # 版本号（可选，默认无后缀）
    version = os.environ.get("DMG_VERSION", "").strip()
    suffix = f"-{version}" if version else ""
    dmg_path = f"dist/Drifox{suffix}.dmg"

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

# -*- coding: utf-8 -*-
"""
创建 Drifox DMG 安装包
可通过环境变量指定：
  - DMG_VERSION: 主版本号（如 v0.5.6）
  - DMG_SUFFIX:  分支后缀（如 pyside6）；空字符串表示不带后缀

单边后缀规则（与 release.yml / installer.iss 保持一致）：
  - DMG_SUFFIX 留空  → Drifox-macOS-v0.5.6.dmg          （dev 正式版，更新器期望）
  - DMG_SUFFIX=pyside6 → Drifox-macOS-v0.5.6-pyside6.dmg（PySide6 移植版）
"""
import os
import dmgbuild

def build_dmg():
    app_path = "dist/Drifox.app"

    version = os.environ.get("DMG_VERSION", "").strip()
    suffix = os.environ.get("DMG_SUFFIX", "").strip()

    # 版本号段
    ver_part = f"-{version}" if version else ""
    # 后缀段（空后缀表示不带，连字符也省掉；非空则拼 -suffix）
    suffix_part = f"-{suffix}" if suffix else ""
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

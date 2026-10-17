# -*- coding: utf-8 -*-
"""复现：输入框右下角工具计数 label（toolCountLabel）字体是否应用全局字体

模拟 bottom_toolbar_module 聚合 QSS + _refresh_tool_toggle_btn 运行期重设两条链，
polish 后用 QFontInfo 看实际渲染字族，与配置值对比。
"""

import sys

from PySide6.QtGui import QFontInfo
from PySide6.QtWidgets import QApplication, QLabel, QWidget


def main() -> int:
    app = QApplication(sys.argv)

    # 真实配置（用户环境）
    from app.utils.config import Settings

    settings = Settings.get_instance()
    try:
        cfg_family = settings.llm_font_family.value
    except Exception as exc:  # noqa: BLE001
        cfg_family = f"<读取失败: {exc}>"

    from app.utils.design_tokens import font_size_css
    from app.utils.utils import get_font_family_css

    css_family = get_font_family_css()

    # 链 1：bottom_toolbar_module 聚合 QSS（挂在容器上）
    container = QWidget()
    container.setObjectName("toolbarInner")
    container.setStyleSheet(f"""
        QWidget#toolbarInner {{ background: transparent; border: none; }}
        QLabel#toolCountLabel {{
            color: #888; background: transparent; border: none;
            {font_size_css(12)} {get_font_family_css()}
        }}
    """)

    # 链 2：_refresh_tool_toggle_btn 运行期重设（挂在 label 自身）
    label = QLabel("2/5", container)
    label.setObjectName("toolCountLabel")
    label.setStyleSheet(f"""
        color: #888;
        background: transparent; border: none;
        {font_size_css(12)} {get_font_family_css()}
    """)

    container.show()
    app.processEvents()  # 触发 QSS polish，让 font rule 写回 widget

    resolved = QFontInfo(label.font()).family()
    print(f"[配置] llm_font_family = {cfg_family!r}")
    print(f"[QSS串] {css_family}")
    print(f"[链1容器聚合QSS+链2自身重设] resolved family = {resolved!r}")
    print(f"[结论] {'一致' if resolved == cfg_family else '不一致 → 存在覆盖/未生效'}")

    # 只走链 1（容器聚合 QSS，label 自身无样式表）
    label2 = QLabel("2/5", container)
    label2.setObjectName("toolCountLabel")
    label2.setStyleSheet("")
    app.processEvents()
    print(f"[仅链1] resolved family = {QFontInfo(label2.font()).family()!r}")

    # 只走链 2（label 自身样式表，无容器聚合 QSS）
    label3 = QLabel("2/5")
    label3.setObjectName("toolCountLabel")
    label3.setStyleSheet(f"""
        color: #888;
        background: transparent; border: none;
        {font_size_css(12)} {get_font_family_css()}
    """)
    label3.show()
    app.processEvents()
    print(f"[仅链2] resolved family = {QFontInfo(label3.font()).family()!r}")

    container.hide()
    label3.hide()
    return 0


if __name__ == "__main__":
    sys.exit(main())

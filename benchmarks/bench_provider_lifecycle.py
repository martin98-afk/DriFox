# -*- coding: utf-8 -*-
"""量化一次性服务商表单关闭后的资源残留；--baseline 对照修复前关闭方法。"""

from __future__ import annotations

import argparse
import ast
import gc
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(model_count: int, baseline: bool):
    with tempfile.TemporaryDirectory(prefix="drifox_provider_lifecycle_") as isolation:
        os.environ["DRIFOX_DATA_DIR"] = isolation
        os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
        os.environ["QT_QPA_PLATFORM"] = "windows"
        import psutil
        from loguru import logger
        from PyQt5.QtCore import QCoreApplication, QEvent
        from PyQt5.QtWidgets import QApplication, QWidget
        from qfluentwidgets.common.style_sheet import styleSheetManager

        app = QApplication([])
        logger.remove()
        from app.widgets.cards.global_card_controller import GlobalCardController
        from app.widgets.cards.settings.base_settings_card import BaseSettingsCard
        from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

        close = GlobalCardController._close_edit_card
        if baseline:
            import app.widgets.cards.global_card_controller as module

            source = subprocess.check_output(
                ["git", "show", "6dad434f:app/widgets/cards/global_card_controller.py"],
                cwd=ROOT, text=True, encoding="utf-8",
            )
            cls = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and node.name == "GlobalCardController")
            method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_close_edit_card")
            namespace = dict(vars(module))
            exec(compile(ast.Module(body=[method], type_ignores=[]), "baseline", "exec"), namespace)
            close = namespace[method.name]
        process = psutil.Process()

        def settle(milliseconds):
            end = time.perf_counter() + milliseconds / 1000
            while time.perf_counter() < end:
                app.processEvents()
                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                time.sleep(0.005)

        def sample(stage):
            gc.collect()
            memory = process.memory_info()
            print(json.dumps({
                "stage": stage,
                "baseline": baseline,
                "models": model_count,
                "private_mb": round(getattr(memory, "private", memory.rss) / 2**20, 2),
                "rss_mb": round(memory.rss / 2**20, 2),
                "widgets": len(QApplication.allWidgets()),
                "styled_widgets": len(styleSheetManager.widgets),
            }, ensure_ascii=False), flush=True)

        shell = BaseSettingsCard("服务商配置")
        shell.resize(1000, 700)
        shell.show()
        # 预热：不将模块首次加载/样式系统初始化算作大表单常驻成本。
        warm = ProviderEditCard(parent=shell)
        warm.dispose()
        del warm
        settle(100)
        sample("before-open")
        form = ProviderEditCard(
            provider_name="benchmark-local",
            provider_info={"API_URL": "http://localhost:9/v1", "API_KEY": "", "模型列表": [f"model-{i}" for i in range(model_count)]},
            is_new=False,
            parent=shell,
        )
        shell.content_layout.addWidget(form)
        shell.set_save_button_handler(form._on_save)
        controller = GlobalCardController.__new__(GlobalCardController)
        controller._tab_manager = SimpleNamespace(close_replace_card=lambda card_id: None)
        controller._card_manager = SimpleNamespace(hide_card=lambda *args: shell.hide(), show_card=lambda *args: None)
        controller._provider_edit_popup = form
        controller._provider_picker_card = shell
        controller._provider_edit_card = None
        controller._provider_view = "edit"
        end = time.perf_counter() + 20
        while form.modelListEditor._pending_rows and time.perf_counter() < end:
            settle(20)
        assert not form.modelListEditor._pending_rows
        settle(100)
        sample("opened")
        del form
        close(controller, "provider_picker")
        settle(300)
        sample("closed")
        shell.deleteLater()
        settle(100)
        app.quit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="比较一次性模型表单关闭前后的内存")
    parser.add_argument("--models", type=int, default=50)
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    run(args.models, args.baseline)

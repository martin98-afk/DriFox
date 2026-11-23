# -*- coding: utf-8 -*-
"""ui_helpers 样式常量惰性化回归（实施单 #R2）。

覆盖硬约束：
① 模块导入不触发 config.load / secret_store.unwrap_secrets（subprocess spy 计数）
② 首次取样式值触发完整链，且返回值与原 _get_* 函数直调逐字节一致（三级链语义等价）
③ 首访后缓存：二次访问不再重算；未知名抛 AttributeError
"""

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

_PROBE = """
import sys
calls = {"load": 0, "unwrap": 0}

import app.utils.config as config_mod
from app.utils.config import Settings

_orig_load = Settings.load
def _spy_load(self, *a, **k):
    calls["load"] += 1
    return _orig_load(self, *a, **k)
Settings.load = _spy_load

# unwrap_secrets 是 secret_store 模块级函数，_apply_secret_mode 内延迟
# from-import（每次调用现查 secret_store 模块）→ spy 替换模块属性即可拦到
import app.utils.secret_store as secret_mod

_orig_unwrap = secret_mod.unwrap_secrets
def _spy_unwrap(*a, **k):
    calls["unwrap"] += 1
    return _orig_unwrap(*a, **k)
secret_mod.unwrap_secrets = _spy_unwrap

import app.widgets.ui_helpers as uh

print("AFTER_IMPORT", calls["load"], calls["unwrap"], sep="|")
vals = (uh.TITLE_STYLE, uh.MODEL_BTN_STYLE, uh.MODEL_BTN_TEXT_STYLE)
print("AFTER_ACCESS", calls["load"], calls["unwrap"], sep="|")
print("EAGER_MATCH", vals == (uh._get_title_style(), uh._get_model_btn_style(), uh._get_model_btn_text_style()), sep="|")
print("CACHE_HIT", uh.TITLE_STYLE is vals[0], sep="|")
"""

def _run_probe() -> str:
    r = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO),
        timeout=120,
    )
    assert r.returncode == 0, f"探针进程失败: {r.stderr[-600:]}"
    return r.stdout

def _grab(out: str, key: str):
    m = re.search(rf"^{key}\|(.*)$", out, re.MULTILINE)
    assert m, f"探针输出缺 {key}: {out[-400:]}"
    return m.group(1)

def test_import_does_not_trigger_config_or_secret_chain():
    """约束①：模块导入零触发 config.load / unwrap_secrets（暖启动 -213ms 的根）"""
    out = _run_probe()
    loads, unwraps = _grab(out, "AFTER_IMPORT").split("|")
    assert int(loads) == 0, f"导入期触发 config.load {loads} 次"
    assert int(unwraps) == 0, f"导入期触发 unwrap_secrets {unwraps} 次"

def test_first_access_triggers_chain_and_matches_eager_values():
    """约束②：首次访问触发完整链；三个常量值与原 _get_* 直调逐字节一致"""
    out = _run_probe()
    loads, unwraps = _grab(out, "AFTER_ACCESS").split("|")
    assert int(loads) >= 1, "首次访问未触发 config.load（样式空窗/默认值回退？）"
    assert int(unwraps) >= 1, "首次访问未触发 unwrap_secrets（三级链被短路？）"
    assert _grab(out, "EAGER_MATCH") == "True"

def test_cached_after_first_access():
    """首访后缓存进模块 globals：二次访问同对象，不重算"""
    out = _run_probe()
    assert _grab(out, "CACHE_HIT") == "True"

def test_unknown_attr_raises_attribute_error():
    """未知名仍抛 AttributeError（PEP 562 语义完整）"""
    r = subprocess.run(
        [sys.executable, "-c", "import app.widgets.ui_helpers as uh; uh.NO_SUCH_THING"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO), timeout=120,
    )
    assert r.returncode != 0
    assert "NO_SUCH_THING" in (r.stderr or "")

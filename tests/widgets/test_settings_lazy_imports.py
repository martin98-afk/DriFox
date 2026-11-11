# -*- coding: utf-8 -*-
"""设置首开 import 下沉回归（#31：mcp SDK / GiteeCard 两链懒化）

plan #29 实测：点设置同步 import 主卡 909ms，大头 = mcp_setting_card 顶部
import app.tools.mcp_tools（mcp SDK 全家桶 cum 598.9ms）+ GiteeCard 链
（gateway.auth + requests ~155ms）。下沉后：
- GiteeCard：import 下沉到 _setup_content（主卡构造期才拉链）
- mcp_setting_card 整模块下沉：MCP 页懒构造（_ensure_page_ready），进页才拉
  mcp SDK 与 mcp-eventloop 常驻线程

断言用子进程口径（sys.modules 全局态不受 pytest 同进程其他测试污染）。
已知例外：hooks 卡构造链（hook_setting_card.py:44 顶层 import mcp_setting_card）
在主卡构造期拉起 mcp_setting_card/mcp_tools——hook_setting_card 在本任务授权
范围外，另案处理；本套断言只锁 import 期（构造前）与 MCP 页两段。
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _REPO_ROOT / "app" / "widgets" / "cards" / "settings" / "llm_settings_card.py"

_PROBE = r"""
import sys
import time

t0 = time.perf_counter()
from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard  # noqa: E401,F401

# 快照 1：import 后立即（此刻 sys.modules 才反映 import 期真实状态；
# 主卡构造期 hooks 卡会拉 mcp_setting_card，不能在构造后再判定）
at_import = {
    "mcp_tools": "app.tools.mcp_tools" in sys.modules,
    "gitee_card": "app.widgets.cards.settings.gitee_card" in sys.modules,
    "requests": "requests" in sys.modules,
    "gateway": any(m.startswith("app.gateway") for m in sys.modules),
}
t_import = time.perf_counter() - t0

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
card = LLMSettingsCard()

# 快照 2：主卡构造后（MCP 页保持懒 / giteeCard 已建）
after_construct = {
    "mcp_list_card_none": card.mcpListCard is None,
    "gitee_card_built": getattr(card, "giteeCard", None) is not None,
}

# 进 MCP 页：懒构造拉 SDK
card._ensure_page_ready("mcp")
after_page = {
    "mcp_tools": "app.tools.mcp_tools" in sys.modules,
    "mcp_list_card_built": card.mcpListCard is not None,
    "in_list_cards": card.mcpListCard in card._list_cards,
}

import json

state = {
    "import_s": round(t_import, 4),
    "at_import_mcp_tools": at_import["mcp_tools"],
    "at_import_gitee_card": at_import["gitee_card"],
    "at_import_requests": at_import["requests"],
    "at_import_gateway": at_import["gateway"],
    "after_construct_mcp_none": after_construct["mcp_list_card_none"],
    "after_construct_gitee_built": after_construct["gitee_card_built"],
    "after_page_mcp_tools": after_page["mcp_tools"],
    "after_page_card_built": after_page["mcp_list_card_built"],
    "after_page_in_list_cards": after_page["in_list_cards"],
}

print("@@PROBE@@" + json.dumps(state))
"""


def _run_probe() -> dict:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        timeout=180,
        env=env,
    )
    for ln in proc.stdout.splitlines():
        if ln.startswith("@@PROBE@@"):
            return json.loads(ln[len("@@PROBE@@") :])
    raise AssertionError(f"探针无输出（stderr 尾部）: {proc.stderr[-500:]}")


def test_top_level_imports_sinked():
    """AST 守卫：主卡模块顶层不得再 import gitee_card / mcp_setting_card"""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"), filename=str(_SRC))
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", "") or ""
            names = " ".join(a.name for a in getattr(node, "names", []))
            blob = mod + " " + names
            assert "gitee_card" not in blob, "GiteeCard import 回退顶层（首开 155ms 回归）"
            assert "mcp_setting_card" not in blob, "mcp_setting_card import 回退顶层（首开 599ms 回归）"


def test_import_phase_clean_and_construct_semantics():
    """子进程实测：import <0.4s 且四链未拉；构造后 gitee 建而 MCP 页保持懒"""
    state = _run_probe()
    assert state["import_s"] < 0.4, f"import 主卡仍超时: {state['import_s']}s（对照 909ms 基线）"
    assert state["at_import_mcp_tools"] is False, "import 期不得拉起 mcp SDK"
    assert state["at_import_gitee_card"] is False, "import 期不得拉起 gitee_card 链"
    assert state["at_import_requests"] is False, "import 期不得拉起 requests"
    assert state["at_import_gateway"] is False, "import 期不得拉起 gateway.auth"
    assert state["after_construct_gitee_built"] is True, "provider 首屏页 giteeCard 构造期正常"
    assert state["after_construct_mcp_none"] is True, "主卡构造后 MCP 页必须保持懒（None）"


def test_entering_mcp_page_pulls_sdk_and_registers():
    """进 MCP 页：懒构造拉起 mcp SDK，卡片补入手风琴列表（功能可用口径）"""
    state = _run_probe()
    assert state["after_page_mcp_tools"] is True, "进 MCP 页必须拉起 mcp SDK（功能可用）"
    assert state["after_page_card_built"] is True, "进页后 mcpListCard 必须构建"
    assert state["after_page_in_list_cards"] is True, "懒构造卡必须补入手风琴列表"


def test_mcp_registered_in_lazy_page_builders():
    """守卫：mcp 页必须登记在 _LAZY_PAGE_BUILDERS（懒构造机制接线）"""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"), filename=str(_SRC))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_LAZY_PAGE_BUILDERS" for t in node.targets
        ):
            src_text = ast.unparse(node)
            assert "'mcp'" in src_text and "_build_mcp_page" in src_text, "mcp 页懒构造登记缺失"
            return
    raise AssertionError("未找到 _LAZY_PAGE_BUILDERS")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

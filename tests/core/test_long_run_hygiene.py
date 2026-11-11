# -*- coding: utf-8 -*-
"""长跑小项 #40 回归：日志 retention / 删除配置残留轮询 / 懒态盲区。

三件各自可验：
1. logging_setup.py 的 all.log / mem_diag.log 两路 sink 带 retention="7 days"
   （对齐分系统日志；目录总量判定线 ≤50MB，dev 基线 13.7MB）
2. provider_setting_card._remove_provider 删除配置后调 UsageService.invalidate
   （_active_plan_keys/_active_balance_keys 残留轮询退出）
3. main_widget 两处热重载刷卡的 hasattr → getattr(None) 懒态守卫
"""
import ast
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8-sig")


def test_log_retention_on_all_and_mem_diag():
    """条 1：两路 sink 配置 retention=7 days（AST 精确取 _add_sink 调用 kwargs）"""
    tree = ast.parse(_read("app/core/infra/logging_setup.py"))
    found = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_add_sink"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            name = node.args[0].value
            if name in ("all.log", "mem_diag.log"):
                kws = {kw.arg: (kw.value.value if isinstance(kw.value, ast.Constant) else None) for kw in node.keywords}
                found[name] = kws
    assert set(found) == {"all.log", "mem_diag.log"}, "两路 sink 必须都在 AST 中"
    for name, kws in found.items():
        assert kws.get("retention") == "7 days", f"{name} 缺 retention=7 days（长跑无界累积）"
        assert kws.get("rotation") == "10 MB", f"{name} 轮转口径不变"


def test_log_retention_judgement_line_comment():
    """条 1：判定线注释在案（≤50MB / dev 13.7MB）"""
    src = _read("app/core/infra/logging_setup.py")
    assert "≤50MB" in src and "13.7MB" in src, "目录总量判定线与 dev 基线注释缺失"


def test_remove_provider_invalidates_usage_service():
    """条 2：删除路径 _remove_provider 必须调 invalidate（AST + 行为两路）"""
    src = _read("app/widgets/cards/settings/provider_setting_card.py")
    tree = ast.parse(src)
    method = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_remove_provider":
            method = node
            break
    assert method is not None, "_remove_provider 未找到"
    text = ast.unparse(method)
    assert "UsageService.get_instance().invalidate" in text, (
        "删除配置后必须失效 UsageService 活跃轮询 key（残留轮询）"
    )


def test_usage_invalidate_clears_active_keys():
    """条 2 行为：invalidate(config_id) 清 _active_plan_keys/_active_balance_keys"""
    pytest.importorskip("PyQt5.QtCore")
    from app.core.infra.usage_service import UsageService

    svc = UsageService.__new__(UsageService)
    svc._plan_cache = {("p", "cfg-1"): (0.0, None)}
    svc._balance_cache = {("p", "cfg-1"): (0.0, None)}
    svc._configs = {"cfg-1": {}}
    svc._active_plan_keys = {("p", "cfg-1"), ("p", "cfg-2")}
    svc._active_balance_keys = {("p", "cfg-1")}
    svc.invalidate("cfg-1")
    assert svc._active_plan_keys == {("p", "cfg-2")}, "被删配置必须从活跃轮询退出"
    assert svc._active_balance_keys == set()
    assert "cfg-1" not in svc._configs


def test_hot_reload_lazy_guards_use_getattr_none():
    """条 3：main_widget 两处热重载刷卡改 getattr(None) + None 守卫，不再靠
    except AttributeError 吞掉懒态缺失。"""
    src = _read("app/main_widget.py")
    assert src.count('card = getattr(win._settings_popup, "hookListCard", None)') == 1
    assert src.count('mcp_card = getattr(win._settings_popup, "mcpListCard", None)') == 1
    assert src.count("if card is None:") >= 1 and src.count("if mcp_card is None:") >= 1, (
        "两处懒态 None 守卫必须存在"
    )
    # hooks 分支不再用 except AttributeError 兜底吞错
    hooks_block = src[src.find("# Hooks 变更：刷新 hook 设置卡片") : src.find("# 主题变更：刷新主题下拉列表")]
    assert "except RuntimeError:" in hooks_block and "except (RuntimeError, AttributeError):" not in hooks_block, (
        "hooks 热重载分支不得再靠 AttributeError 兜底吞懒态错误"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

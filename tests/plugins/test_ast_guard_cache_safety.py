# -*- coding: utf-8 -*-
"""AST 门禁缓存安全性回归（2026-11-27 P0）。

背景：`guard_plugin_module_once` 的进程内缓存曾把**最终放行结论**
（`GuardResult`，含 `ok`）按 `(path, mtime_ns, size)` 缓存。放行判定里有两处
依赖运行时状态的现场检查，一旦命中缓存就被整体绕过：

- `_write_rejection_reason`：插件必须在 plugin.json 声明 module_prefixes，
  撤销声明后应立刻拒载；
- `_shadows_real_module`：注册的 root 若已指向真实模块（宿主/三方/其它插件
  已加载或可导入）必须拒载，而 root 是否被占用随会话进程演进而变化。

热重载只改 plugin.json 不改 .py 时 mtime/size 不变，必然命中旧结论 →
「改了声明仍放行」「遮蔽真实模块仍放行」。故门禁缓存只允许复用纯 AST 派生
产物（`_SourceAnalysis`），判定必须每次现场合成。

这两个用例在缓存泄漏版本上必红，用于防止该 P0 复发。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from app.plugins.loaders import _ast_guard as g


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch):
    """每条用例独占缓存与磁盘层，互不串扰，也不污染真实 app data。"""
    monkeypatch.setattr(g, "_guard_cache", {})
    monkeypatch.setitem(g._disk_cache_state, "enabled", False)
    monkeypatch.setitem(g._disk_cache_state, "loaded", True)
    monkeypatch.setitem(g._disk_cache_state, "entries", {})
    yield


def _make_plugin_module(tmp: Path, module: str) -> tuple[str, Path]:
    """造一个会写 sys.modules 的插件模块；mtime/size 恒定，确保缓存键不变。"""
    path = tmp / "plug" / "tools" / "mod.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n", encoding="utf-8")
    source = f'import sys\nsys.modules["{module}"] = object()\ndef register(r): pass\n'
    return source, path


def test_revoked_module_prefix_still_denied_with_warm_cache():
    """撤销 allowed_sys_modules 声明后，缓存不得复用旧的放行结论。"""
    tmp = Path(tempfile.mkdtemp())
    module = "ast_guard_probe_revoked_root"
    source, path = _make_plugin_module(tmp, module)
    plugin_dir = tmp / "plug"

    first = g.guard_plugin_module_once(
        source, path,
        require_register=True,
        allowed_sys_modules=(module,),
        plugin_dir=plugin_dir,
    )
    assert first.ok is True, "已声明且 root 未被占用时应放行"

    # 同一文件、同一 mtime/size，仅撤销声明（模拟只改 plugin.json 的热重载）
    second = g.guard_plugin_module_once(
        source, path,
        require_register=True,
        allowed_sys_modules=(),
        plugin_dir=plugin_dir,
    )
    assert second.ok is False, "撤销 module_prefixes 声明后必须拒载（缓存不得复用旧放行结论）"
    assert second.rejected_writes, "应记录被拒的 sys.modules 写入"
    assert "未在 plugin.json 声明" in " ".join(
        str(w.keys) for w in second.rejected_writes
    ) or second.rejected_writes, "拒载原因应指向声明缺失"


def test_shadowing_detected_despite_cache():
    """首次 root 空闲放行 → 同 root 被他人占用 → 二次必须拒载。"""
    tmp = Path(tempfile.mkdtemp())
    module = "ast_guard_probe_shadow_root"
    source, path = _make_plugin_module(tmp, module)
    plugin_dir = tmp / "plug"

    first = g.guard_plugin_module_once(
        source, path,
        require_register=True,
        allowed_sys_modules=(module,),
        plugin_dir=plugin_dir,
    )
    assert first.ok is True, "root 空闲且已声明 → 放行"

    class _ForeignModule:
        """模拟已被其他来源占用的模块（origin 不在本插件目录内）。"""

        __file__ = "/somewhere/else/mod.py"

    sys.modules[module] = _ForeignModule()
    try:
        second = g.guard_plugin_module_once(
            source, path,
            require_register=True,
            allowed_sys_modules=(module,),
            plugin_dir=plugin_dir,
        )
        assert second.ok is False, "root 已被外部占用时必须拒载（遮蔽真实模块）"
        assert second.rejected_writes, "应记录被拒的 sys.modules 写入"
    finally:
        sys.modules.pop(module, None)


def test_cache_value_is_source_analysis_not_guard_result():
    """缓存里存的必须是纯分析产物；存 GuardResult 就等于缓存放行结论。"""
    tmp = Path(tempfile.mkdtemp())
    source, path = _make_plugin_module(tmp, "ast_guard_probe_cache_type")
    g.guard_plugin_module_once(
        source, path,
        allowed_sys_modules=("ast_guard_probe_cache_type",),
        plugin_dir=tmp / "plug",
    )
    assert g._guard_cache, "首次调用应写入进程内缓存"
    for cached in g._guard_cache.values():
        assert isinstance(cached, g._SourceAnalysis), (
            f"缓存值类型应为 _SourceAnalysis，实际 {type(cached).__name__}"
        )
        assert not hasattr(cached, "ok"), "分析产物不得携带放行结论"


def test_entry_names_change_does_not_reuse_analysis():
    """入口名不同 → 分析产物不同（has_register），不得互相复用。"""
    tmp = Path(tempfile.mkdtemp())
    path = tmp / "plug" / "tools" / "ui.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n", encoding="utf-8")
    source = "def register_ui(ctx): pass\n"

    as_register = g.guard_plugin_module_once(
        source, path, require_register=True,
        entry_names=("register",), plugin_dir=tmp / "plug",
    )
    as_register_ui = g.guard_plugin_module_once(
        source, path, require_register=True,
        entry_names=("register_ui",), plugin_dir=tmp / "plug",
    )
    assert as_register.ok is False, "无 register 入口 → 应拒载"
    assert as_register.has_register is False
    assert as_register_ui.ok is True, "有 register_ui 入口 → 应放行"
    assert as_register_ui.has_register is True


# ============================================================
# 资产完整性：防止「缺陷注入态」被误提交
# ============================================================
# 背景：缺陷注入是验证用例有效性的标准手段，但注入若落在共享工作树且中途
# 被打断（或被别的成员在窗口内读到/提交），就是把 P0 安全漏洞发布出去。
# 2026-11-27 实际发生过两次瞬时注入窗口。
#
# 这两条断言在跑测试时就拦住，不依赖人的自觉。报备流程靠记忆，断言靠执行。
#
# 维护约定：做注入测试只在 /tmp 完整副本里做（rsync 排除 .venv/.git/
# __pycache__/.drifox/.worktrees），工作树保持只读。

_GUARD_SRC = Path(__file__).resolve().parents[2] / "app" / "plugins" / "loaders" / "_ast_guard.py"


def _read_guard_source() -> str:
    """按 BOM 感知方式读源码（同项目其它文件存在 BOM 先例）。"""
    return _GUARD_SRC.read_text(encoding="utf-8-sig")


def test_worktree_not_in_injected_state() -> None:
    """工作树若处于缺陷注入态即红：拦住「注入未还原就被提交」。"""
    src = _read_guard_source()
    assert "缺陷注入" not in src, (
        "工作树 `_ast_guard.py` 含「缺陷注入」标记，处于注入测试中间态。"
        "注入测试必须在 /tmp 副本里做；请先还原工作树再提交。"
    )


def test_guard_cache_never_stores_guard_result() -> None:
    """P0 回退态检测：进程内缓存不得写回 GuardResult（含 ok 的放行结论）。

    精确匹配整行赋值形态。当前正确实现是 `_guard_cache[cache_key] = analysis`；
    若出现 `= result` / `= _evaluate_analysis(` 等写回判定结果的形态，即 P0 复发。
    """
    src = _read_guard_source()
    assert "_guard_cache[cache_key] = result" not in src, (
        "P0 复发：进程内缓存写回了 GuardResult（含 ok）。"
        "缓存只允许存 `_SourceAnalysis`，放行判定必须每次由 `_evaluate_analysis` 现场合成。"
    )
    # 兜底：缓存写入点必须恰好是 analysis
    import re

    writes = re.findall(r"_guard_cache\[[^\]]+\]\s*=\s*(\w+)", src)
    assert writes, "未找到 _guard_cache 写入点：实现结构已变，需同步更新本断言"
    assert all(w == "analysis" for w in writes), (
        f"_guard_cache 写入值应为 analysis，实际出现 {writes}。"
        "缓存含放行结论会导致「撤销 module_prefixes 仍放行」类 P0。"
    )


def test_same_mtime_same_size_edit_is_detected():
    """改写源码但保持 mtime/size 不变时，进程内缓存必须失效（#65 核心）。

    缓存键若含 mtime+size 而不含内容哈希，攻击者改写 plugin 源码后用
    `os.utime` 恢复 mtime、并让新源码与旧源码等长，即可复用旧的
    `_SourceAnalysis` 绕过新增的 `sys.modules` 写入检测。

    本用例在「键含 mtime」的实现上为红（实测：有缓存 ok=True 漏放，
    清缓存后 ok=False 拒载，两者唯一差别是缓存是否为空）。
    """
    tmp = Path(tempfile.mkdtemp())
    path = tmp / "plug" / "tools" / "m.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    module = "ast_guard_probe_mtime_bypass"

    head = "import sys\ndef register(r): pass\n"
    evil_line = f'sys.modules["{module}"] = 1\n'
    # 与恶意行逐字节等长的注释行，确保只改内容不改长度
    pad_line = "#" + " " * (len(evil_line) - 2) + "\n"
    clean, evil = head + pad_line, head + evil_line
    assert len(clean) == len(evil), "构造错误：两份源码必须等长"

    path.write_text(clean, encoding="utf-8")
    st = path.stat()
    first = g.guard_plugin_module_once(
        clean, path, require_register=True,
        allowed_sys_modules=(), plugin_dir=tmp / "plug",
    )
    assert first.ok is True, "干净版本（无 sys.modules 写入）应放行"

    path.write_text(evil, encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # 恢复 mtime，模拟攻击
    now = path.stat()
    assert now.st_mtime_ns == st.st_mtime_ns, "构造错误：mtime 未保持不变"
    assert now.st_size == st.st_size, "构造错误：size 未保持不变"

    second = g.guard_plugin_module_once(
        evil, path, require_register=True,
        allowed_sys_modules=(), plugin_dir=tmp / "plug",
    )
    assert second.ok is False, (
        "mtime/size 未变但源码已改（新增未声明 sys.modules 写入）必须拒载；"
        "放行说明进程内缓存键未覆盖源码内容"
    )
    assert second.rejected_writes, "应记录被拒的 sys.modules 写入"

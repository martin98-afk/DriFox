# -*- coding: utf-8 -*-
"""B1：架构边界守卫（T2-5 防回归）。

规则：
- app/**（除 app/plugins/ 子树）：零容忍 —— 宿主核心禁止 import plugins.*
  （插件单点失败隔离 + 打包可导入性；插件→宿主走稳定门面）
- tests/**：白名单冻结 —— 存量白盒测试 22 文件放行（历史形态，逐步清退），
  新增违规立即变红

含「故意注入可红」自证用例：对含违规的临时目录跑同一扫描器，断言能命中。
"""
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = PROJECT_ROOT / "app"
TESTS_ROOT = PROJECT_ROOT / "tests"
_PATTERN = re.compile(r"^(from|import)\s+plugins\.", re.MULTILINE)

# tests/ 侧存量白名单（相对 tests/ 的 posix 路径）：冻结现状，只防新增。
# 清退一个文件时同步从此处移除。
LEGACY_TESTS_ALLOWLIST = {
    "debug/agent_trace_stream_dup_repro.py",
    "debug/agent_trace_token_repro.py",
    "debug/verify_incremental_projection.py",
    "file_tools_resolve_test.py",
    "plugins/test_adapter_families.py",
    "plugins/test_builtin_openai_adapter.py",
    "plugins/test_e2e_phase_c.py",
    "plugins/test_hook_policy_plugins.py",
    "plugins/test_loop_policy.py",
    "plugins/test_openai_adapter_detectors.py",
    "plugins/test_openai_serializer.py",
    "plugins/test_serializer_single_entry.py",
    "plugins/test_storage_consumer_migration.py",
    "plugins/test_storage_engine.py",
    "plugins/test_subagent_loop_policy.py",
    "plugins/test_ui_storage_migration.py",
    "plugins/test_websearch_config_contract.py",
    "test_workflow_journal.py",
    "test_workflow_store.py",
    "test_workflow_tool.py",
    "widgets/test_changelog_fetcher_thread_md.py",
    "widgets/test_plugin_tag_renderer.py",
}


def _scan_import_violations(root: Path, skip_plugins_subtree: bool = False) -> list:
    """扫描 root 下 *.py 的 `from plugins.` / `import plugins.` 命中。

    Args:
        skip_plugins_subtree: 豁免 root 下第一层 plugins/ 子树（app 扫描用，
            插件互引由插件机制管理；tests 扫描不豁免——白名单管）。

    Returns:
        [(文件相对路径, 行号, 行文本), ...]
    """
    hits = []
    if not root.exists():
        return hits
    for py in sorted(root.rglob("*.py")):
        rel_parts = py.relative_to(root).parts
        if skip_plugins_subtree and rel_parts and rel_parts[0] == "plugins":
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _PATTERN.match(line) or _PATTERN.match(line.strip()):
                hits.append((py.relative_to(root).as_posix(), lineno, line.strip()))
    return hits


def test_app_core_never_imports_plugins_namespace():
    """宿主 app 核心（除 app/plugins/ 子树）零 plugins.* 直接导入，零容忍。"""
    violations = _scan_import_violations(APP_ROOT, skip_plugins_subtree=True)
    assert violations == [], (
        "app 核心出现反向依赖插件内部模块（禁止）：\n"
        + "\n".join(f"  app/{f}:{ln}: {line}" for f, ln, line in violations)
    )


def test_tests_side_no_new_plugins_imports():
    """tests/ 侧白名单冻结：存量放行，新增 plugins.* 导入即红。"""
    violations = _scan_import_violations(TESTS_ROOT)
    new_hits = [v for v in violations if v[0] not in LEGACY_TESTS_ALLOWLIST]
    assert new_hits == [], (
        "tests/ 出现白名单外的 plugins.* 导入（新测试请经由公开门面或 "
        "sys.path+包名加载，参考 tests/test_marketplace_proxy.py 的 pm_ui 惯例）：\n"
        + "\n".join(f"  tests/{f}:{ln}: {line}" for f, ln, line in new_hits)
    )


def test_scanner_catches_injected_violation(tmp_path):
    """故意注入验证：扫描器对违规样本必须可红（守卫有效性自证）。"""
    bad = tmp_path / "fake_app" / "sub"
    bad.mkdir(parents=True)
    (bad / "violator.py").write_text(
        "import json\nfrom plugins.system.tools import file_tools\n", encoding="utf-8"
    )
    clean = tmp_path / "fake_app" / "clean.py"
    clean.write_text("from app.utils import utils\n", encoding="utf-8")
    # 子树豁免同样生效（app 扫描语义）
    exempt = tmp_path / "fake_app" / "plugins" / "x"
    exempt.mkdir(parents=True)
    (exempt / "ok.py").write_text("import plugins.anything\n", encoding="utf-8")

    hits = _scan_import_violations(tmp_path / "fake_app", skip_plugins_subtree=True)
    assert len(hits) == 1
    rel, _ln, line = hits[0]
    assert rel.endswith("violator.py") and "file_tools" in line


def _ci_workflow_texts() -> list:
    wf_dir = PROJECT_ROOT / ".github" / "workflows"
    if not wf_dir.exists():
        return []
    return [p.read_text(encoding="utf-8", errors="replace") for p in sorted(wf_dir.glob("*.y*ml"))]


def test_arch_guard_registered_in_ci():
    """CI 若运行 pytest 测试套件则须包含本守卫；无 CI/CI 不跑 pytest 则跳过（不强造基建）。"""
    texts = _ci_workflow_texts()
    if not texts:
        pytest.skip("项目无 .github/workflows CI 配置，守卫仅在本地/手动 pytest 生效")
    joined = "\n".join(texts)
    if "pytest" not in joined:
        pytest.skip(
            "现有 CI workflows 不运行 pytest 测试套件（仅 marketplace/release 任务），"
            "守卫暂无 attach 点；接入测试流水线时须包含 tests/test_arch_boundaries.py"
        )
    assert "arch_boundaries" in joined, "CI 已运行 pytest 但未包含架构守卫（tests/test_arch_boundaries.py）"

# -*- coding: utf-8 -*-
"""EU-G18：hook 相对路径导入的范围限制（含链接逃逸防护）

## 背景
相对路径是插件自带 hook 的**唯一可用方式**（插件模块不可能位于 `app.hooks`/`app.utils`），
故**不能**套用 `SAFE_PYTHON_MODULES` 白名单（那会废掉所有插件的 python 型 hook）。
但不加限制则可越界导入任意模块。

## 实测结论（plan 验证，本批用测试锁定）
| 越界方式 | 结果 |
|---|---|
| `..` 各种变体（9 种构造） | ❌ 全被封死 —— `replace(".", "/")` 把点转斜杠，上跳语义意外消灭 |
| 绝对路径 | ❌ 封死 —— `startswith(".")` 要求 |
| **junction（`mklink /J`）** | ✅ **可越界**（无需管理员）→ 本批拦 |
| **硬链接（`os.link`）** | ✅ **可越界**（需同卷）→ 本批拦 |

## 修法要点
1. `resolve()` 后必须在 `config_dir` 子树内 → 拦 junction / 符号链接
2. 拒任何链接文件本身（`is_symlink()` 或 `st_nlink > 1`）→ 拦硬链接
   （`resolve()` **无法**识别硬链接：它返回链接自身路径，仍"在子树内"）
3. 不改执行语义（hook 保持静默执行）——只限制"能不能导入"
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from loguru import logger

from app.core.hooks.hook_manager import Hook, HookWorker

_IS_WIN = sys.platform == "win32"


def _make_worker(tmp_path, module_ref: str, func_name: str = "hook_func") -> HookWorker:
    """构造可直调 _execute_python 的 worker（跳过 Qt signals）"""
    hooks_json = tmp_path / "hooks.json"
    hooks_json.write_text("{}", encoding="utf-8")
    worker = HookWorker.__new__(HookWorker)
    worker.hook = Hook(type="python", function=f"{module_ref}:{func_name}", config_file=str(hooks_json))
    worker.event_name = "test-event"
    worker.context = {}
    return worker


@pytest.fixture
def capture_warnings():
    """捕获 loguru WARNING+ 记录（断言拒执行日志）"""
    records = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    yield records
    logger.remove(sink_id)


# ── 1. 正经场景：同目录模块仍可导入（回归，确保不破坏插件 hook）──


def test_relative_module_inside_dir_allowed(tmp_path):
    """hooks.json 同目录的模块 → 正常导入（不破坏现有插件 hook）"""
    (tmp_path / "relmod.py").write_text("def hook_func(**kw):\n    return 'inside-ok'\n", encoding="utf-8")
    worker = _make_worker(tmp_path, ".relmod")
    out, ok = worker._execute_python()
    assert ok is True and out == "inside-ok", f"同目录模块应放行，实际 {out!r} ok={ok}"


def test_relative_module_in_subdir_allowed(tmp_path):
    """子树内的子目录模块也放行（子树而非仅同层）"""
    sub = tmp_path / "helpers"
    sub.mkdir()
    (sub / "submod.py").write_text("def hook_func(**kw):\n    return 'sub-ok'\n", encoding="utf-8")
    worker = _make_worker(tmp_path, ".helpers.submod")
    out, ok = worker._execute_python()
    assert ok is True and out == "sub-ok"


# ── 2. 越界场景：父目录模块被拒 ──


def test_relative_module_escaping_dir_rejected(tmp_path, capture_warnings):
    """越界到父目录的模块 → 拒执行 + warning

    注：`..` 会先被 `replace(".", "/")` 转成 `/`，故实际路径形态会变；
    本用例验证**无论何种形态，越界结果都是拒绝**。
    """
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "evil.py").write_text("def hook_func(**kw):\n    return 'ESCAPED'\n", encoding="utf-8")
    inner = parent / "inner"
    inner.mkdir()

    worker = _make_worker(inner, "..evil")
    out, ok = worker._execute_python()
    assert ok is False, f"越界模块不应执行，实际 out={out!r} ok={ok}"
    assert "ESCAPED" not in out


# ── 3. `..` 变体参数化（锁定现状的意外防护，防未来改写回归）──


@pytest.mark.parametrize(
    "module_ref",
    [
        "..evil",
        "...evil",
        "..parent.evil",
        "....evil",
        "..a..b",
        ".a..b",
        "..",
        "...",
        ".....",
    ],
)
def test_relative_module_dotdot_forms_rejected(tmp_path, module_ref, capture_warnings):
    """9 种 `..` 构造 → 全部拒绝（锁定 `replace(".", "/")` 的意外防护）

    ⚠ 这层防护是**副作用**而非刻意设计（点号被转成路径分隔符，上跳语义消失）。
    本测试的价值是：**未来若有人改写这段路径处理逻辑，此处会立刻变红**。
    """
    # 在父目录放一个"可被越界拿到"的模块，确认真的拿不到
    (tmp_path / "evil.py").write_text("def hook_func(**kw):\n    return 'ESCAPED'\n", encoding="utf-8")
    inner = tmp_path / "inner"
    inner.mkdir()

    worker = _make_worker(inner, module_ref)
    out, ok = worker._execute_python()
    assert ok is False, f"{module_ref!r} 应被拒绝，实际 out={out!r} ok={ok}"
    assert "ESCAPED" not in out


def test_absolute_path_rejected(tmp_path, capture_warnings):
    """绝对路径形态 → 拒绝（`startswith(".")` 要求）"""
    target = tmp_path / "absmod.py"
    target.write_text("def hook_func(**kw):\n    return 'ABS'\n", encoding="utf-8")
    worker = _make_worker(tmp_path, str(target))
    out, ok = worker._execute_python()
    assert ok is False
    assert "ABS" not in out


# ── 4. junction 越界（核心回归）──


@pytest.mark.skipif(not _IS_WIN, reason="junction 是 NTFS 特性")
def test_relative_module_via_junction_rejected(tmp_path, capture_warnings):
    """junction 指向外部目录 → 拒执行（`resolve()` 后不在子树内）

    junction 无需管理员权限即可创建（这与符号链接不同：后者需 SeCreateSymbolicLinkPrivilege）。
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "linked.py").write_text("def hook_func(**kw):\n    return 'JUNCTION-ESCAPED'\n", encoding="utf-8")

    inner = tmp_path / "inner"
    inner.mkdir()
    junction = inner / "jlink"
    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0 or not junction.exists():
        pytest.skip(f"本机无法创建 junction（非缺陷）: {r.stdout} {r.stderr}")

    worker = _make_worker(inner, ".jlink.linked")
    out, ok = worker._execute_python()
    assert ok is False, f"junction 越界不应执行，实际 out={out!r} ok={ok}"
    assert "JUNCTION-ESCAPED" not in out
    assert any("越界" in w or "链接" in w for w in capture_warnings), "应有越界告警日志"


# ── 5. 硬链接越界（resolve 无法识别，需 nlink 判定）──


@pytest.mark.skipif(not _IS_WIN, reason="硬链接在 Windows 需 NTFS + 同卷")
def test_relative_module_hardlink_rejected(tmp_path, capture_warnings):
    """硬链接指向外部文件 → 拒执行（`st_nlink > 1` 判定）

    ⚠ 关键：硬链接的 `resolve()` **返回链接自身路径**（仍在子树内），
    故只用 resolve 检查会漏判 —— 必须额外用 nlink 判定。
    """
    outside = tmp_path / "outside2"
    outside.mkdir()
    src = outside / "target.py"
    src.write_text("def hook_func(**kw):\n    return 'HARDLINK-ESCAPED'\n", encoding="utf-8")

    inner = tmp_path / "inner2"
    inner.mkdir()
    link = inner / "hard.py"
    try:
        os.link(src, link)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"本机无法创建硬链接（非缺陷）: {e}")

    # 前置确认：链接确实在子树内且 nlink > 1（否则本用例无意义）
    assert link.resolve().is_relative_to(inner.resolve()), "硬链接 resolve 后仍在子树内（这正是漏判点）"
    assert link.stat().st_nlink > 1, "硬链接 nlink 应 > 1"

    worker = _make_worker(inner, ".hard")
    out, ok = worker._execute_python()
    assert ok is False, f"硬链接越界不应执行，实际 out={out!r} ok={ok}"
    assert "HARDLINK-ESCAPED" not in out
    assert any("链接" in w for w in capture_warnings), "应有链接告警日志"


# ── 6. 辅助函数单测 ──


def test_is_linked_file_detects_hardlink(tmp_path):
    """`_is_linked_file` 能识别硬链接"""
    src = tmp_path / "a.txt"
    src.write_text("x", encoding="utf-8")
    assert HookWorker._is_linked_file(src) is False, "单目录项文件不应判为链接"
    link = tmp_path / "b.txt"
    try:
        os.link(src, link)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"本机无法创建硬链接: {e}")
    assert HookWorker._is_linked_file(src) is True


def test_is_linked_file_missing_path_safe(tmp_path):
    """路径不存在 → 返回 False（不抛异常）"""
    assert HookWorker._is_linked_file(tmp_path / "nope.txt") is False


def test_symlink_rejected_if_creatable(tmp_path, capture_warnings):
    """符号链接（若有权限创建）→ 拒执行"""
    outside = tmp_path / "outside3"
    outside.mkdir()
    (outside / "sym.py").write_text("def hook_func(**kw):\n    return 'SYMLINK-ESCAPED'\n", encoding="utf-8")
    inner = tmp_path / "inner3"
    inner.mkdir()
    link = inner / "sym.py"
    try:
        link.symlink_to(outside / "sym.py")
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"本机无符号链接权限（WinError 1314 常见，非缺陷）: {e}")

    worker = _make_worker(inner, ".sym")
    out, ok = worker._execute_python()
    assert ok is False
    assert "SYMLINK-ESCAPED" not in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

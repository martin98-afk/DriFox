# -*- coding: utf-8 -*-
"""value_style AST 静态检查（#23 ②）：调用侧形态错配在 CI 炸，不到运行时。

扫 plugins/*/tools/*.py（一层插件名，排除 deps）：
1. 收集 `x = caps_for("cap_id")`（能力名为字符串字面量；动态形式跳过）
2. catalog 声明 value_style="value" 的能力：`x(...)` 调用 → FAIL
   （pool 值即终值，再调一次就是本次 'dict' object is not callable 的根因）
3. value_style="factory" 的能力：x 被当对象用（属性访问/下标）→ FAIL
   （闭包必须先调用拿 facade；`x is None` 判空与 `x() if x else ...` 合法）
"""

import ast
from pathlib import Path

PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent / "plugins"


def _catalog():
    from app.plugins.capabilities.catalog import CATALOG

    return CATALOG


def _iter_scope(unit: ast.AST):
    """遍历一个作用域内的节点（不进入嵌套函数/Lambda，防同名变量跨作用域污染）。"""
    stack = list(ast.iter_child_nodes(unit))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _collect_caps_for_assignments(tree: ast.AST) -> dict:
    """收集 `x = caps_for("cap_id")` 的 (变量名 → 能力名)。动态能力名跳过。

    按作用域收集：模块级与每个函数各自独立（同名变量不合并）。
    """
    found: dict = {}

    def collect_in(unit: ast.AST) -> None:
        for node in _iter_scope(unit):
            if isinstance(node, ast.Assign):
                call = node.value
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "caps_for"
                ):
                    continue
                if (
                    not call.args
                    or not isinstance(call.args[0], ast.Constant)
                    or not isinstance(call.args[0].value, str)
                ):
                    continue
                cap_id = call.args[0].value
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        found[(id(unit), target.id)] = cap_id

    collect_in(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            collect_in(node)
    return found


def _violations(tree: ast.AST, var_caps: dict, catalog) -> list:
    """按 value_style 检查调用侧形态（作用域隔离），返回违规描述列表。"""
    bad: list = []

    def check(unit: ast.AST, inherited: dict) -> None:
        local = dict(inherited)
        local.update({name: cap for (scope, name), cap in var_caps.items() if scope == id(unit)})
        if not local:
            return
        for node in _iter_scope(unit):
            # 规则 1：value 能力被调用
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in local:
                cap_id = local[node.func.id]
                spec = catalog.get(cap_id)
                if spec is not None and spec.value_style == "value":
                    bad.append(
                        f"value 能力 {cap_id} 被再调用（pool 值即终值）：{node.lineno} 行 `{node.func.id}()`"
                    )
            # 规则 2：factory 能力的闭包被当对象用（属性访问 / 下标）
            if (
                isinstance(node, (ast.Attribute, ast.Subscript))
                and isinstance(node.value, ast.Name)
                and node.value.id in local
            ):
                cap_id = local[node.value.id]
                spec = catalog.get(cap_id)
                if spec is not None and spec.value_style == "factory":
                    label = getattr(node, "attr", "[...]")
                    bad.append(
                        f"factory 能力 {cap_id} 的闭包被当对象用（{label}）：{node.lineno} 行，应先 `x()` 取 facade"
                    )

    module_level = {name: cap for (scope, name), cap in var_caps.items() if scope == id(tree)}
    check(tree, {})
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            check(node, module_level)  # 模块级 caps_for 赋值对全文件函数可见（闭包读外名）
    return bad


def test_value_style_declared_for_all_caps():
    """每个能力都有 value_style 标注，且取值合法。"""
    catalog = _catalog()
    for cap_id, spec in catalog.items():
        assert spec.value_style in ("value", "factory"), f"{cap_id}: 非法 value_style={spec.value_style!r}"


def test_known_value_caps_are_marked():
    """确证的两段式误用受害者在 catalog 中必须标 value（单一真相源抽检）。"""
    catalog = _catalog()
    assert catalog["team.spawn"].value_style == "value"
    assert catalog["tool.execute"].value_style == "value"
    for cap_id in ("team.read", "team.send", "team.manage", "action.queue"):
        assert catalog[cap_id].value_style == "factory", cap_id


def test_tools_dir_no_style_mismatch():
    """扫描全部插件 tools/*.py：caps_for 赋值变量的使用形态必须与声明一致。"""
    catalog = _catalog()
    problems: list = []
    for path in sorted(PLUGINS_DIR.glob("*/tools/*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        var_caps = _collect_caps_for_assignments(tree)
        for v in _violations(tree, var_caps, catalog):
            problems.append(f"{path.relative_to(PLUGINS_DIR.parent)}: {v}")
    assert not problems, "caps_for 形态错配:\n" + "\n".join(problems)


def test_ast_checker_catches_planted_value_call(tmp_path, monkeypatch):
    """人为制造一次 `x()` 错误：检查器必须点名（验证检查器自身有效）。"""
    import tests.plugins.test_capability_value_style_ast as self_mod

    planted = tmp_path / "tools_planted"
    (planted).mkdir()
    (planted / "planted_tools.py").write_text(
        "from app.plugins.sdk import caps_for\n"
        "spawn = caps_for('team.spawn')\n"
        "def go():\n"
        "    return spawn('x')  # value 能力被调用 → 必须被抓\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(self_mod, "PLUGINS_DIR", tmp_path)
    catalog = self_mod._catalog()
    tree = ast.parse((planted / "planted_tools.py").read_text(encoding="utf-8"))
    var_caps = self_mod._collect_caps_for_assignments(tree)
    assert list(var_caps.values()) == ["team.spawn"]
    bad = self_mod._violations(tree, var_caps, catalog)
    assert len(bad) == 1 and "value 能力 team.spawn 被再调用" in bad[0]


def test_ast_checker_scopes_same_name_vars(tmp_path):
    """同名变量跨作用域不污染（#23：cap.get 误报回归锁）。"""
    src = (
        "from app.plugins.sdk import caps_for\n"
        "def a():\n"
        "    cap = caps_for('action.queue')\n"
        "    return cap()\n"
        "def b(cap: dict) -> str:\n"
        "    return cap.get('k')  # 参数同名但非 caps_for 赋值 → 不得报\n"
    )
    tree = ast.parse(src)
    catalog = __import__("app.plugins.capabilities.catalog", fromlist=["CATALOG"]).CATALOG
    var_caps = _collect_caps_for_assignments(tree)
    assert not _violations(tree, var_caps, catalog)

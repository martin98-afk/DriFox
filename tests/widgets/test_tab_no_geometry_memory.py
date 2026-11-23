# -*- coding: utf-8 -*-
"""产品契约：DriFox **不记忆**窗口几何（大小/位置/面板宽度）。

## 为什么要有这条测试

历史上 DriFox 会持久化 `tab_manager_geometry`（含 x/y/w/h），并因
「保存 frame 坐标却用 client 语义还原」导致每次 minimize→restore
窗口向上漂移 title bar 高度（约 65px）。

后来按产品要求**彻底移除几何记忆**：每次打开固定 1100x800 居中、侧边栏固定宽。
旧测试 `test_tab_geometry_roundtrip.py` 断言的正是被移除的那套记忆机制
（fixture 里直接读 `settings.tab_manager_geometry`，已必抛 AttributeError）。

本文件锁住**新的**契约，防止几何记忆被无声地加回来：

1. `Settings` 上不存在任何几何持久化项
2. 配置文件里不会出现几何键
3. `_do_save_geometry()` 是空实现（不写配置）
4. `_restore_geometry()` 使用固定 1100x800 而非读配置

## 与「不记忆」语义的对应

任何一条失败都意味着「某处又把几何写盘了」或「恢复逻辑开始读配置了」，
即产品语义被改回去 —— 那时必须先改这份契约（并说明为什么重新引入记忆），
而不是让测试自己变红被忽略。
"""

import ast
import json
from pathlib import Path

import pytest

# 窗口几何相关的历史配置项名（曾用于持久化 x/y/w/h 与面板宽度）
_LEGACY_GEOMETRY_KEYS = (
    "tab_manager_geometry",
    "tab_panel_width",
    "tab_panel_collapsed",
)

# 固定默认几何（与 tab_manager_window.py::_restore_geometry 保持一致）
_EXPECTED_DEFAULT_SIZE = (1100, 800)


def _settings_source() -> str:
    """读 Settings 的定义源码（app/utils/config.py）。

    优先走包内导入定位真实文件；失败则按仓库相对路径取。
    """
    try:
        import app.utils.config as cfg_mod

        return Path(cfg_mod.__file__).read_text(encoding="utf-8-sig")
    except Exception:
        root = Path(__file__).resolve().parent.parent.parent
        return (root / "app" / "utils" / "config.py").read_text(encoding="utf-8-sig")


class TestNoGeometryPersistenceInSettings:
    """Settings 上不得存在任何几何持久化项。"""

    @pytest.mark.parametrize("key", _LEGACY_GEOMETRY_KEYS)
    def test_legacy_geometry_key_absent(self, key):
        """历史几何配置项必须已从不 Settings 中移除。

        这是「删除旧测试」的核心依据之一：旧测试的 autouse fixture 直接读
        `settings.tab_manager_geometry.value`，该项不存在 → 3 个用例全 ERROR。
        """
        from app.utils.config import Settings

        settings = Settings.get_instance()
        assert not hasattr(settings, key), (
            f"Settings 仍暴露 {key}：窗口几何记忆被重新引入，"
            f"请先确认产品语义并更新本契约"
        )

    def test_settings_source_marks_geometry_removed(self):
        """config.py 的注释须显式说明几何配置项已移除（防注释被顺手删掉后语义丢失）。"""
        src = _settings_source()
        assert "tab_manager_geometry" in src, (
            "config.py 里连「已移除」的说明都没了：后续读者无法知道为什么没有几何配置"
        )
        assert "已移除" in src, "config.py 应保留「配置项已移除」的说明"

    def test_no_write_geometry_calls_in_app(self):
        """全 app/ 不得调用 QWidget.saveGeometry（Qt 自带几何持久化入口）。"""
        root = Path(__file__).resolve().parent.parent.parent / "app"
        offenders = []
        for py in root.rglob("*.py"):
            try:
                text = py.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError):
                continue
            if "saveGeometry" in text or "restoreGeometry" in text:
                offenders.append(str(py.relative_to(root.parent)))
        assert not offenders, (
            f"以下文件使用了 Qt 几何持久化 API，与「不记忆窗口几何」语义冲突：{offenders}"
        )


class TestDoSaveGeometryIsNoop:
    """`_do_save_geometry()` 必须是空实现（AST 级验证，防被悄悄填回实现体）。"""

    @staticmethod
    def _method_node(name: str):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "app"
            / "widgets"
            / "tab_manager_window.py"
        )
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        raise AssertionError(f"tab_manager_window.py 里找不到 {name}()")

    def test_do_save_geometry_has_no_body(self):
        """除 docstring 外不得有任何语句（有任何语句就可能是在写配置）。"""
        node = self._method_node("_do_save_geometry")
        real = [
            stmt
            for stmt in node.body
            if not (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            )
        ]
        assert not real, (
            f"_do_save_geometry 出现了 {len(real)} 条实际语句："
            f"几何记忆可能被重新引入。语句类型：{[type(s).__name__ for s in real]}"
        )

    def test_restore_geometry_uses_fixed_default(self):
        """`_restore_geometry()` 不得读取任何持久化配置（只用固定值 + 屏幕尺寸）。"""
        node = self._method_node("_restore_geometry")
        used_names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        used_attrs = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}

        # 1) 不得触碰 Settings / 配置对象
        assert "Settings" not in used_names, "_restore_geometry 开始读 Settings = 记忆被加回来了"
        assert "cfg" not in used_names, "_restore_geometry 出现 cfg = 记忆被加回来了"
        for name in used_names:
            assert "tab_manager_geometry" not in name, f"引用了已移除的配置项 {name}"

        # 2) 不得调用 Qt 的几何持久化 API（availableGeometry 是屏幕查询，合法）
        _QT_SCREEN_GEOMETRY_APIS = {"availableGeometry", "screenGeometry", "geometry"}
        for attr in used_attrs:
            if attr in _QT_SCREEN_GEOMETRY_APIS:
                continue
            assert "saveGeometry" not in attr and "restoreGeometry" not in attr, (
                f"_restore_geometry 调用了 Qt 几何持久化 API {attr}"
            )


class TestConfigFileHasNoGeometryKeys:
    """实际写出的配置文件里不得出现几何键。"""

    def test_no_geometry_keys_after_settings_roundtrip(self, tmp_path):
        """触发一次配置读写，断言落盘 JSON 里没有几何相关键。"""
        from app.utils.config import Settings

        settings = Settings.get_instance()
        # 触发一次 save（写当前配置到磁盘）
        try:
            settings.save()
        except Exception:
            pytest.skip("当前环境无法写配置（只读/无 qconfig）")

        cfg_path = None
        try:
            from qfluentwidgets.common.config import CONFIG_PATH

            cfg_path = Path(CONFIG_PATH)
        except Exception:
            pass

        if cfg_path is None or not cfg_path.exists():
            pytest.skip("找不到 qfluentwidgets 配置文件路径")

        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pytest.skip("配置文件无法解析")

        flat = json.dumps(data, ensure_ascii=False).lower()
        for key in _LEGACY_GEOMETRY_KEYS:
            assert key.lower() not in flat, (
                f"配置文件里出现了几何键 {key}：几何记忆被重新引入"
            )


class TestDefaultGeometryValues:
    """固定默认几何的具体值（与 _restore_geometry 的实现对齐）。"""

    def test_restore_geometry_literal_matches(self):
        """`_restore_geometry` 里的 w/h 字面量必须等于 1100x800。

        注释校准的锚点：config.py 的注释写的就是这个尺寸，两处必须一致。
        """
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "app"
            / "widgets"
            / "tab_manager_window.py"
        )
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_restore_geometry":
                # 找 `w, h = 1100, 800`
                for stmt in ast.walk(node):
                    if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Tuple):
                        vals = stmt.value.elts if isinstance(stmt.value, ast.Tuple) else []
                        nums = [v.value for v in vals if isinstance(v, ast.Constant)]
                        if len(nums) == 2:
                            assert tuple(nums) == _EXPECTED_DEFAULT_SIZE, (
                                f"_restore_geometry 默认尺寸是 {nums}，"
                                f"与契约约定的 {_EXPECTED_DEFAULT_SIZE} 不一致"
                            )
                            return
        pytest.fail("_restore_geometry 里找不到 w, h = <num>, <num> 形式的默认尺寸")

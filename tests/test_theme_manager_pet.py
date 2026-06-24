"""ThemeManager.get_theme_pet() 的单元测试"""
import pytest
from pathlib import Path

from app.utils.theme_manager import ThemeManager


@pytest.fixture
def fresh_manager(monkeypatch, tmp_path):
    """每次返回新初始化的 ThemeManager，避免单例污染。"""
    ThemeManager._instance = None
    # 临时把内置主题目录重定向到 tmp_path
    builtin_dir = tmp_path / "builtin"
    monkeypatch.setattr("app.utils.theme_manager._BUILTIN_THEMES_DIR", builtin_dir)

    def _load_themes_fake(self):
        """只从测试目录加载，跳过插件/用户主题"""
        self._load_from_dir(builtin_dir, is_builtin=True)

    monkeypatch.setattr("app.utils.theme_manager.ThemeManager._load_themes", _load_themes_fake)
    return ThemeManager()


def _make_theme_dir(base: Path, theme_id: str, pet_image: str | None) -> Path:
    """构造一个主题文件夹。"""
    theme_dir = base / theme_id
    theme_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = theme_dir / f"{theme_id}.yaml"
    pet_block = f"pet:\n  image: {pet_image}\n" if pet_image else ""
    yaml_path.write_text(
        f"name: {theme_id}\nid: {theme_id}\n{pet_block}",
        encoding="utf-8",
    )
    return theme_dir


def test_undeclared_pet_returns_empty_dict(fresh_manager, tmp_path):
    """未声明 pet 段的主题 → 返回 {}（调用方 fallback 到内嵌默认）"""
    _make_theme_dir(tmp_path / "builtin", "no_pet_theme", pet_image=None)

    result = fresh_manager.get_theme_pet("no_pet_theme")
    assert result == {}, f"期望空 dict，实际: {result}"


def test_declared_pet_with_existing_image_returns_path(fresh_manager, tmp_path):
    """声明 pet.image 且文件存在 → 返回 {'image': Path, 'source': 'theme'}"""
    theme_dir = _make_theme_dir(tmp_path / "builtin", "with_pet", pet_image="./pet.png")
    (theme_dir / "pet.png").write_bytes(b"\x89PNG\r\n\x1a\n")  # 假 png

    # 手动触发一次加载（themes 在 manager init 后才创建）
    fresh_manager._themes.clear()
    fresh_manager._load_themes()

    result = fresh_manager.get_theme_pet("with_pet")
    assert result.get("source") == "theme"
    assert result.get("image") == theme_dir / "pet.png"
    assert result["image"].exists()


def test_declared_pet_with_missing_image_returns_empty(fresh_manager, tmp_path):
    """声明 pet.image 但文件不存在 → 返回 {}（fallback 到内嵌默认）"""
    _make_theme_dir(tmp_path / "builtin", "missing_pet", pet_image="./pet.png")
    # 注意：没有创建 pet.png 文件

    result = fresh_manager.get_theme_pet("missing_pet")
    assert result == {}, f"期望空 dict（文件不存在 fallback），实际: {result}"


def test_unknown_theme_returns_empty(fresh_manager):
    """不存在的 theme_id → 返回 {}"""
    result = fresh_manager.get_theme_pet("nonexistent")
    assert result == {}


def test_empty_pet_block_returns_empty(fresh_manager, tmp_path):
    """pet: 段存在但为空（无 image 字段）→ 返回 {}"""
    theme_dir = tmp_path / "builtin" / "empty_pet"
    theme_dir.mkdir(parents=True, exist_ok=True)
    (theme_dir / "empty_pet.yaml").write_text(
        "name: empty\nid: empty_pet\npet:\n",
        encoding="utf-8",
    )

    result = fresh_manager.get_theme_pet("empty_pet")
    assert result == {}

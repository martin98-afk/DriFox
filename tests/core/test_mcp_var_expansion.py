"""测试 MCP 配置中的 ${CLAUDE_PLUGIN_ROOT} / ${CLAUDE_PLUGIN_DATA} 变量展开/逆展开"""

import json
import tempfile
from pathlib import Path

from app.plugins.managers.plugin_manager import PluginManager

PLUGIN_ROOT = Path("/Users/test/.drifox/plugins/test-plugin")
PLUGIN_DATA = PLUGIN_ROOT / "data"


class TestExpandMCPVars:
    """测试 _expand_mcp_vars"""

    def test_expand_simple_string(self):
        result = PluginManager._expand_mcp_vars("${CLAUDE_PLUGIN_ROOT}/foo.py", PLUGIN_ROOT)
        assert result == f"{PLUGIN_ROOT.as_posix()}/foo.py"

    def test_expand_data_var(self):
        result = PluginManager._expand_mcp_vars("${CLAUDE_PLUGIN_DATA}/bar", PLUGIN_ROOT)
        assert result == f"{PLUGIN_DATA.as_posix()}/bar"

    def test_expand_dict(self):
        cfg = {"PYTHONPATH": "${CLAUDE_PLUGIN_ROOT}", "DATA": "${CLAUDE_PLUGIN_DATA}"}
        result = PluginManager._expand_mcp_vars(cfg, PLUGIN_ROOT)
        assert result["PYTHONPATH"] == PLUGIN_ROOT.as_posix()
        assert result["DATA"] == PLUGIN_DATA.as_posix()

    def test_expand_list(self):
        result = PluginManager._expand_mcp_vars(
            ["${CLAUDE_PLUGIN_ROOT}/a.py", "${CLAUDE_PLUGIN_DATA}/b.json"],
            PLUGIN_ROOT,
        )
        assert result == [
            f"{PLUGIN_ROOT.as_posix()}/a.py",
            f"{PLUGIN_DATA.as_posix()}/b.json",
        ]

    def test_expand_non_string(self):
        assert PluginManager._expand_mcp_vars(42, PLUGIN_ROOT) == 42
        assert PluginManager._expand_mcp_vars(None, PLUGIN_ROOT) is None
        assert PluginManager._expand_mcp_vars(True, PLUGIN_ROOT) is True

    def test_expand_no_vars(self):
        assert PluginManager._expand_mcp_vars("plain text", PLUGIN_ROOT) == "plain text"

    def test_expand_backslash_normalization(self):
        """反斜杠占位符也能正确展开（Windows 用户手动编辑场景）"""
        result = PluginManager._expand_mcp_vars(
            "${CLAUDE_PLUGIN_ROOT}\\bin\\server.exe", PLUGIN_ROOT
        )
        assert result == f"{PLUGIN_ROOT.as_posix()}/bin/server.exe"


class TestUnexpandMCPVars:
    """测试 _unexpand_mcp_vars"""

    def test_unexpand_simple_string(self):
        result = PluginManager._unexpand_mcp_vars(
            f"{PLUGIN_ROOT.as_posix()}/foo.py", PLUGIN_ROOT
        )
        assert result == "${CLAUDE_PLUGIN_ROOT}/foo.py"

    def test_unexpand_data_var(self):
        result = PluginManager._unexpand_mcp_vars(
            f"{PLUGIN_DATA.as_posix()}/bar", PLUGIN_ROOT
        )
        assert result == "${CLAUDE_PLUGIN_DATA}/bar"

    def test_unexpand_dict(self):
        cfg = {"PYTHONPATH": PLUGIN_ROOT.as_posix(), "DATA": PLUGIN_DATA.as_posix()}
        result = PluginManager._unexpand_mcp_vars(cfg, PLUGIN_ROOT)
        assert result["PYTHONPATH"] == "${CLAUDE_PLUGIN_ROOT}"
        assert result["DATA"] == "${CLAUDE_PLUGIN_DATA}"

    def test_unexpand_list(self):
        result = PluginManager._unexpand_mcp_vars(
            [f"{PLUGIN_ROOT.as_posix()}/a.py", "unchanged"],
            PLUGIN_ROOT,
        )
        assert result == ["${CLAUDE_PLUGIN_ROOT}/a.py", "unchanged"]

    def test_unexpand_no_match(self):
        assert PluginManager._unexpand_mcp_vars("plain text", PLUGIN_ROOT) == "plain text"
        assert PluginManager._unexpand_mcp_vars(42, PLUGIN_ROOT) == 42

    def test_unexpand_data_before_root(self):
        """确保逆展开时先替换 DATA（更长的路径），避免被 ROOT 部分匹配"""
        result = PluginManager._unexpand_mcp_vars(PLUGIN_DATA.as_posix(), PLUGIN_ROOT)
        assert result == "${CLAUDE_PLUGIN_DATA}"

    def test_unexpand_backslash_normalization(self):
        """Windows 反斜杠路径也能正确逆展开"""
        root_backslash = PLUGIN_ROOT.as_posix().replace("/", "\\")
        result = PluginManager._unexpand_mcp_vars(
            f"{root_backslash}\\bin\\server.exe", PLUGIN_ROOT
        )
        assert result == "${CLAUDE_PLUGIN_ROOT}/bin/server.exe"


class TestRoundtrip:
    """测试展开→逆展开的往返一致性"""

    def test_roundtrip_real_config(self):
        """用真实 agent-memory 插件配置格式测试往返"""
        original = {
            "command": "python",
            "args": ["${CLAUDE_PLUGIN_ROOT}/plugin/mcp_server.py"],
            "env": {
                "PYTHONPATH": "${CLAUDE_PLUGIN_ROOT}",
                "AGENT_MEMORY_DATA": "${CLAUDE_PLUGIN_DATA}",
            },
            "type": "stdio",
            "enabled": True,
            "url": "",
            "headers": {},
        }
        expanded = PluginManager._expand_mcp_vars(original, PLUGIN_ROOT)

        # 验证展开正确
        assert expanded["args"] == [f"{PLUGIN_ROOT.as_posix()}/plugin/mcp_server.py"]
        assert expanded["env"]["PYTHONPATH"] == PLUGIN_ROOT.as_posix()
        assert expanded["env"]["AGENT_MEMORY_DATA"] == PLUGIN_DATA.as_posix()

        # 逆展开回原始
        unexpanded = PluginManager._unexpand_mcp_vars(expanded, PLUGIN_ROOT)
        assert unexpanded == original

    def test_roundtrip_nested(self):
        """深嵌套结构的往返测试"""
        original = {
            "env": {
                "nested": {
                    "deep": "${CLAUDE_PLUGIN_ROOT}/deep/path",
                    "list": ["${CLAUDE_PLUGIN_DATA}/a", "${CLAUDE_PLUGIN_ROOT}/b"],
                }
            }
        }
        expanded = PluginManager._expand_mcp_vars(original, PLUGIN_ROOT)
        unexpanded = PluginManager._unexpand_mcp_vars(expanded, PLUGIN_ROOT)
        assert unexpanded == original


class TestBuildMCPEntry:
    """测试 _build_mcp_entry 集成"""

    def test_expand_vars_in_entry(self):
        """_build_mcp_entry 返回的条目应包含展开后的值"""
        with tempfile.TemporaryDirectory() as tmpdir:
            mcp_json = Path(tmpdir) / ".mcp.json"
            mcp_json.write_text(json.dumps({"mcpServers": {"test": {}}}))

            pm = PluginManager()
            entry = pm._build_mcp_entry(
                "test-server",
                {
                    "command": "python",
                    "args": ["${CLAUDE_PLUGIN_ROOT}/server.py"],
                    "env": {"ROOT": "${CLAUDE_PLUGIN_ROOT}", "DATA": "${CLAUDE_PLUGIN_DATA}"},
                    "type": "stdio",
                    "enabled": True,
                    "url": "",
                    "headers": {},
                },
                mcp_json,
            )

            expected_root = mcp_json.parent.as_posix()
            assert entry["args"] == [f"{expected_root}/server.py"]
            assert entry["env"]["ROOT"] == expected_root
            assert entry["env"]["DATA"] == f"{expected_root}/data"
            assert entry["_source"] == str(mcp_json)

    def test_no_vars_passthrough(self):
        """无变量占位符的配置直接透传"""
        with tempfile.TemporaryDirectory() as tmpdir:
            mcp_json = Path(tmpdir) / ".mcp.json"
            mcp_json.write_text(json.dumps({"mcpServers": {"test": {}}}))

            pm = PluginManager()
            entry = pm._build_mcp_entry(
                "test-server",
                {
                    "command": "npx",
                    "args": ["@playwright/mcp@latest"],
                    "env": {},
                    "type": "stdio",
                    "enabled": True,
                },
                mcp_json,
            )
            assert entry["command"] == "npx"
            assert entry["args"] == ["@playwright/mcp@latest"]

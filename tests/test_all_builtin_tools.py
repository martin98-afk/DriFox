# -*- coding: utf-8 -*-
"""
测试所有内置工具是否能正常工作

运行方式: python -m pytest tests/test_all_builtin_tools.py -v
或者: python tests/test_all_builtin_tools.py
"""

import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

# 设置工作目录为项目根目录
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 使用标准输出而非 loguru，避免 QApplication 相关问题
print(f"[Test] Project root: {PROJECT_ROOT}")


class TestBuiltinTools:
    """测试所有内置工具"""

    @classmethod
    def setup_class(cls):
        """初始化测试环境"""
        # 创建一个虚拟的 QApplication 环境（如果尚未创建）
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

        from PyQt5.QtWidgets import QApplication

        cls.app = QApplication.instance()
        if cls.app is None:
            cls.app = QApplication(sys.argv)

        # 创建临时工作目录用于测试
        cls.test_workdir = tempfile.mkdtemp(prefix="drifox_test_")
        print(f"[Test] Created temp workdir: {cls.test_workdir}")

        # 初始化 BuiltinTools
        from app.tools import BuiltinTools

        cls.tools = BuiltinTools(homepage=None, workdir=cls.test_workdir)

    @classmethod
    def teardown_class(cls):
        """清理测试环境"""
        if hasattr(cls, "tools"):
            try:
                cls.tools.cleanup()
            except Exception as e:
                print(f"[Test] Cleanup error: {e}")

    def _test_tool(self, tool_name: str, test_func, *args, **kwargs) -> Dict[str, Any]:
        """测试单个工具"""
        result = {"tool": tool_name, "status": "unknown", "message": ""}
        try:
            print(f"\n[Testing] {tool_name}...")
            response = test_func(*args, **kwargs)

            # 检查返回结果
            if hasattr(response, "success"):
                if response.success:
                    result["status"] = "pass"
                    result["message"] = str(response.content)[:200] if response.content else "OK"
                else:
                    result["status"] = "fail"
                    result["message"] = response.error or "Unknown error"
            else:
                # 没有标准 ToolResult 的工具
                result["status"] = "pass"
                result["message"] = str(response)[:200]

            print(f"[Result] {tool_name}: {result['status']} - {result['message']}")
        except Exception as e:
            result["status"] = "error"
            result["message"] = str(e)
            print(f"[Error] {tool_name}: {e}")

        return result

    # ========== 文件工具测试 ==========

    def test_read(self):
        """测试 read 工具"""
        # 创建测试文件
        test_file = Path(self.test_workdir) / "test_read.txt"
        test_file.write_text("Hello, DriFox!\nTest content line 2", encoding="utf-8")

        return self._test_tool("read", self.tools.read, path=str(test_file))

    def test_write(self):
        """测试 write 工具"""
        test_file = Path(self.test_workdir) / "test_write.txt"

        def do_write():
            return self.tools.write(path="test_write.txt", content="Written content\nLine 2")

        return self._test_tool("write", do_write)

    def test_edit(self):
        """测试 edit 工具"""
        # 先写入内容
        test_file = Path(self.test_workdir) / "test_edit.txt"
        test_file.write_text("Hello World\nReplace this line", encoding="utf-8")

        def do_edit():
            return self.tools.edit(
                path="test_edit.txt",
                oldString="Replace this line",
                newString="Replaced with new line"
            )

        return self._test_tool("edit", do_edit)

    def test_multi_edit(self):
        """测试 multi_edit 工具"""
        test_file = Path(self.test_workdir) / "test_multi_edit.txt"
        test_file.write_text("Line 1\nLine 2\nLine 3", encoding="utf-8")

        def do_multi_edit():
            return self.tools.multi_edit(
                path="test_multi_edit.txt",
                edits=[
                    {"oldString": "Line 1", "newString": "First"},
                    {"oldString": "Line 3", "newString": "Third"},
                ]
            )

        return self._test_tool("multi_edit", do_multi_edit)

    def test_grep(self):
        """测试 grep 工具"""
        # 创建测试文件
        test_file = Path(self.test_workdir) / "test_grep.txt"
        test_file.write_text("def hello():\n    print('hello world')\n    return True\ndef goodbye():\n    pass", encoding="utf-8")

        return self._test_tool("grep", self.tools.grep, pattern="def ", path=self.test_workdir, include="*.txt")

    def test_list(self):
        """测试 list 工具"""
        # 创建一些测试目录和文件
        test_dir = Path(self.test_workdir) / "test_list_dir"
        test_dir.mkdir(exist_ok=True)
        (test_dir / "file1.txt").touch()
        (test_dir / "file2.py").touch()

        return self._test_tool("list", self.tools.list, path=self.test_workdir)

    def test_glob(self):
        """测试 glob 工具"""
        return self._test_tool("glob", self.tools.glob, pattern="**/*.txt", path=self.test_workdir)

    # ========== 终端工具测试 ==========

    def test_bash(self):
        """测试 bash 工具"""
        return self._test_tool("bash", self.tools.bash, command="echo 'Hello from bash'")

    # 后台任务工具需要特殊处理，这里跳过实际测试
    def test_bg_list(self):
        """测试 bg_list 工具"""
        return self._test_tool("bg_list", self.tools.bg_list)

    # ========== 诊断工具测试 ==========

    def test_get_diagnostics(self):
        """测试 get_diagnostics 工具"""
        # 创建测试 Python 文件
        test_file = Path(self.test_workdir) / "test_diag.py"
        test_file.write_text("import os\nprint('hello')\n", encoding="utf-8")

        return self._test_tool("get_diagnostics", self.tools.get_diagnostics, path=str(test_file), language="python")

    # ========== Web 工具测试 ==========

    def test_websearch(self):
        """测试 websearch 工具"""
        return self._test_tool("websearch", self.tools.websearch, query="DriFox AI", num_results=3)

    def test_webfetch(self):
        """测试 webfetch 工具"""
        return self._test_tool("webfetch", self.tools.webfetch, url="https://httpbin.org/html", format="text")

    # ========== 代码分析工具测试 ==========

    def test_scan_repo(self):
        """测试 scan_repo 工具"""
        return self._test_tool("scan_repo", self.tools.scan_repo, path=str(PROJECT_ROOT), max_depth=2)

    # ========== 任务管理工具测试 ==========

    def test_todowrite(self):
        """测试 todowrite 工具"""
        todos = [
            {"id": "1", "content": "Test task 1", "status": "pending", "priority": "high"},
            {"id": "2", "content": "Test task 2", "status": "in_progress", "priority": "medium"},
        ]
        return self._test_tool("todowrite", self.tools.todo_write, todos=todos)

    def test_todoread(self):
        """测试 todoread 工具"""
        return self._test_tool("todoread", self.tools.todoread)

    def test_stage_files(self):
        """测试 stage_files 工具"""
        files = ["app/tools/__init__.py", "app/tools/file_tools.py"]
        return self._test_tool("stage_files", self.tools.stage_files, files=files)

    # ========== 技能工具测试 ==========

    def test_list_skills(self):
        """测试 list_skills 工具"""
        return self._test_tool("list_skills", self.tools.list_skills)

    # ========== MCP 工具测试 ==========

    def test_mcp_list_servers(self):
        """测试 mcp_list_servers 工具"""
        return self._test_tool("mcp_list_servers", self.tools.mcp_list_servers)

    # ========== 桌面自动化工具测试 ==========
    # 这些需要实际的桌面环境，offscreen 模式下跳过

    def test_screenshot(self):
        """测试 screenshot 工具（在 offscreen 模式下会失败）"""
        # 在 offscreen 模式下，截图可能会失败
        try:
            return self._test_tool("screenshot", self.tools.screenshot, path=str(Path(self.test_workdir) / "test.png"))
        except Exception as e:
            print(f"[Skip] screenshot (requires display): {e}")
            return {"tool": "screenshot", "status": "skip", "message": "Requires display"}

    # ========== LSP 工具测试 ==========

    def test_lsp(self):
        """测试 lsp 工具"""
        # 创建一个测试 Python 文件
        test_file = Path(self.test_workdir) / "test_lsp.py"
        test_file.write_text(
            "def hello():\n    pass\n\nclass Test:\n    def method(self):\n        pass\n",
            encoding="utf-8"
        )

        return self._test_tool("lsp", self.tools.lsp, path=str(test_file), operation="diagnostics")

    # ========== 子智能体工具测试 ==========
    # 这些需要完整的 AgentManager 配置，跳过

    def test_subagent_para(self):
        """测试 subagent_para（跳过，需要完整配置）"""
        print("[Skip] subagent_para (requires agent manager)")
        return {"tool": "subagent_para", "status": "skip", "message": "Requires agent manager"}

    def test_subagent_status(self):
        """测试 subagent_status"""
        return self._test_tool("subagent_status", self.tools.subagent_status, task_ids=[])

    def test_subagent_dag(self):
        """测试 subagent_dag（跳过，需要完整配置）"""
        print("[Skip] subagent_dag (requires agent manager)")
        return {"tool": "subagent_dag", "status": "skip", "message": "Requires agent manager"}

    # ========== 其他工具测试 ==========

    def test_upload_file(self):
        """测试 upload_file（跳过，需要真实文件）"""
        # 创建测试文件
        test_file = Path(self.test_workdir) / "test_upload.txt"
        test_file.write_text("Test content for upload", encoding="utf-8")

        return self._test_tool("upload_file", self.tools.upload_file, local_path=str(test_file))

    def test_skill(self):
        """测试 skill（需要技能名称）"""
        # 测试加载内置 brainstorming 技能
        return self._test_tool("skill", self.tools.skill, name="brainstorming")

    def test_question(self):
        """测试 question（跳过，需要用户交互）"""
        print("[Skip] question (requires user interaction)")
        return {"tool": "question", "status": "skip", "message": "Requires user interaction"}

    def test_mouse(self):
        """测试 mouse（跳过，需要桌面环境）"""
        print("[Skip] mouse (requires desktop)")
        return {"tool": "mouse", "status": "skip", "message": "Requires desktop"}

    def test_keyboard(self):
        """测试 keyboard（跳过，需要桌面环境）"""
        print("[Skip] keyboard (requires desktop)")
        return {"tool": "keyboard", "status": "skip", "message": "Requires desktop"}


def run_all_tests() -> List[Dict[str, Any]]:
    """运行所有测试并返回结果"""
    test_suite = TestBuiltinTools()
    test_suite.setup_class()

    results = []

    # 需要测试的工具方法列表
    test_methods = [
        "test_read",
        "test_write",
        "test_edit",
        "test_multi_edit",
        "test_grep",
        "test_list",
        "test_glob",
        "test_bash",
        "test_bg_list",
        "test_get_diagnostics",
        "test_websearch",
        "test_webfetch",
        "test_scan_repo",
        "test_todowrite",
        "test_todoread",
        "test_stage_files",
        "test_list_skills",
        "test_mcp_list_servers",
        "test_screenshot",
        "test_lsp",
        "test_subagent_status",
        "test_upload_file",
        "test_skill",
    ]

    for method_name in test_methods:
        if hasattr(test_suite, method_name):
            method = getattr(test_suite, method_name)
            result = method()
            if result:
                results.append(result)

    # 打印汇总
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)

    status_counts = {"pass": 0, "fail": 0, "error": 0, "skip": 0}
    for r in results:
        status = r.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    for r in results:
        status = r.get("status", "unknown")
        tool = r.get("tool", "unknown")
        msg = r.get("message", "")
        symbol = "✓" if status == "pass" else "✗" if status == "fail" else "⚠" if status == "error" else "⊘"
        print(f"{symbol} {tool}: {status} - {msg[:80]}")

    print("-" * 60)
    print(f"总计: {len(results)} 个工具")
    print(f"通过: {status_counts.get('pass', 0)} | 失败: {status_counts.get('fail', 0)} | 错误: {status_counts.get('error', 0)} | 跳过: {status_counts.get('skip', 0)}")
    print("=" * 60)

    test_suite.teardown_class()

    return results


if __name__ == "__main__":
    run_all_tests()

# -*- coding: utf-8 -*-
"""
CodeGraph 工具集 — 语义级代码智能引擎（单入口）

将 codegraph-py 封装为 DriFox 内置工具，只有一个对外方法 `codegraph_explore`，
通过 mode 参数切换搜索/调用链/影响分析/状态查看等能力。

使用方式（LLM 视角）：
  codegraph_explore(query="ChatBackend", mode="explore")
  codegraph_explore(mode="status")
  codegraph_explore(query="send_message", mode="callers")
"""

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from app.tools.result import ToolResult

# ── 尝试导入 codegraph-py ─────────────────────────────────────────────────
try:
    from codegraph.codegraph import CodeGraph
    from codegraph.directory import is_initialized, find_nearest_codegraph_root
    from codegraph.types import SearchOptions, SearchResult, Node

    _HAS_CODEGRAPH = True
except ImportError:
    _HAS_CODEGRAPH = False
    CodeGraph = None  # type: ignore
    is_initialized = lambda _: False  # type: ignore
    find_nearest_codegraph_root = lambda _: None  # type: ignore


class CodeGraphTools:
    """CodeGraph 内置工具 — 统一代码探索入口"""

    def __init__(self, owner):
        self._owner = owner
        self._cg: Optional[CodeGraph] = None
        self._project_root: Optional[str] = None
        self._last_init_attempt = 0.0
        self._init_cooldown = 5.0

    @property
    def workdir(self) -> Path:
        return self._owner.workdir

    # ── 生命周期管理 ─────────────────────────────────────────────────────

    def _get_cg(self) -> Optional[CodeGraph]:
        if not _HAS_CODEGRAPH:
            return None
        if self._cg is not None:
            return self._cg

        now = time.time()
        if now - self._last_init_attempt < self._init_cooldown:
            return None
        self._last_init_attempt = now

        root = self._resolve_project_root()
        if not root:
            return None

        if not is_initialized(root):
            # 自动初始化 CodeGraph 索引
            logger.info(f"[CodeGraph] 自动初始化索引: {root}")
            try:
                self._cg = CodeGraph.init_sync(root)
                self._project_root = root
                logger.info("[CodeGraph] 开始全量索引（首次可能较慢）...")
                result = self._cg.index_all()
                files = getattr(result, "files_indexed", 0)
                nodes = getattr(result, "nodes_created", 0)
                edges = getattr(result, "edges_created", 0)
                logger.info(f"[CodeGraph] 索引完成: {files} 文件, {nodes} 节点, {edges} 边")
                return self._cg
            except Exception as e:
                logger.warning(f"[CodeGraph] 自动初始化失败: {e}")
                # init 失败后清理
                if self._cg is not None:
                    try:
                        self._cg.close()
                    except Exception:
                        pass
                    self._cg = None
                return None

        try:
            self._cg = CodeGraph.open_sync(root)
            self._project_root = root
            logger.info(f"[CodeGraph] 已打开索引: {root}")
            return self._cg
        except Exception as e:
            logger.warning(f"[CodeGraph] 打开索引失败: {e}")
            return None

    def _resolve_project_root(self) -> Optional[str]:
        wd = str(self.workdir.resolve())
        if is_initialized(wd):
            return wd
        nearest = find_nearest_codegraph_root(wd)
        if nearest:
            return nearest
        # 没有已有索引 → 返回工作目录自身，后续 _get_cg 会触发 auto-init
        return wd

    def _ensure_cg(self) -> Optional[CodeGraph]:
        """确保可用，返回 CodeGraph 实例或 None"""
        if not _HAS_CODEGRAPH:
            return None
        return self._get_cg()

    def cleanup(self):
        if self._cg is not None:
            try:
                self._cg.close()
            except Exception:
                pass
            self._cg = None
            self._project_root = None
            logger.info("[CodeGraph] 已释放")

    # ── 内部辅助 ─────────────────────────────────────────────────────────

    def _resolve_node(self, name: str, cg: CodeGraph) -> Optional[Node]:
        nodes = cg.get_nodes_by_name(name)
        if nodes:
            return nodes[0]
        results = cg.search_nodes(name, SearchOptions(limit=5))
        return results[0].node if results else None

    @staticmethod
    def _sig(node: Node) -> str:
        return f" {node.signature}" if node.signature else ""

    @staticmethod
    def _loc(node: Node) -> str:
        return f"{node.file_path}:{node.start_line}"

    # ── 模式实现（内部）───────────────────────────────────────────────────

    def _mode_status(self, cg: CodeGraph) -> str:
        stats = cg.get_stats()
        last_indexed = cg.get_last_indexed_at()

        lines = [f"📊 CodeGraph 索引状态 — {self._project_root}", ""]
        lines.append(f"文件: {stats.file_count:,}  |  符号: {stats.node_count:,}  |  关系: {stats.edge_count:,}")

        if stats.nodes_by_kind:
            lines.append("")
            for kind, count in sorted(stats.nodes_by_kind.items(), key=lambda x: -x[1]):
                lines.append(f"  {kind}: {count:,}")

        try:
            changes = cg.get_changed_files()
            total = len(changes.get("added", [])) + len(changes.get("modified", [])) + len(changes.get("removed", []))
            if total > 0:
                lines.append("")
                lines.append(f"⚠ 待同步 {total} 个文件变更")
                if changes.get("added"):
                    lines.append(f"  +新增 {len(changes['added'])}")
                if changes.get("modified"):
                    lines.append(f"  ~修改 {len(changes['modified'])}")
                if changes.get("removed"):
                    lines.append(f"  -删除 {len(changes['removed'])}")
        except Exception:
            pass

        if last_indexed:
            lines.append("")
            lines.append(f"最后索引: {datetime.fromtimestamp(last_indexed / 1000).strftime('%Y-%m-%d %H:%M')}")

        return "\n".join(lines)

    def _mode_sync(self, cg: CodeGraph) -> str:
        result = cg.sync()
        total = result.files_added + result.files_modified + result.files_removed
        if total == 0:
            return "索引已是最新，无需同步"
        details = []
        if result.files_added > 0:
            details.append(f"新增 {result.files_added}")
        if result.files_modified > 0:
            details.append(f"修改 {result.files_modified}")
        if result.files_removed > 0:
            details.append(f"删除 {result.files_removed}")
        return f"同步完成: {', '.join(details)}，更新 {result.nodes_updated} 个符号"

    def _mode_search(self, cg: CodeGraph, query: str, kind: Optional[str], limit: int, exact: bool) -> str:
        if not query:
            return "请提供搜索关键词"
        opts = SearchOptions(limit=limit, exact_match=exact)
        if kind:
            opts.kinds = [kind]
        results = cg.search_nodes(query, opts)
        if not results:
            return f"未找到匹配「{query}」的符号"

        from collections import defaultdict

        by_file: Dict[str, List[SearchResult]] = defaultdict(list)
        for r in results:
            by_file[r.node.file_path].append(r)

        lines = [f"🔍 搜索结果: 「{query}」 ({len(results)} 个, {len(by_file)} 个文件)"]
        for filepath, nodes in sorted(by_file.items()):
            lines.append("")
            lines.append(f"📄 {filepath}  ({len(nodes)} 个符号)")
            for r in nodes:
                n = r.node
                lines.append(f"  [{n.kind}] **{n.name}**{self._sig(n)}  → L{n.start_line}")
        return "\n".join(lines)

    def _mode_callers(self, cg: CodeGraph, symbol: str, depth: int) -> str:
        node = self._resolve_node(symbol, cg)
        if node is None:
            return f"未找到符号「{symbol}」"
        pairs = cg.get_callers(node.id, depth)
        if not pairs:
            return f"没有代码调用「{node.name}」"
        by_file: Dict[str, List] = {}
        seen = set()
        for caller, edge in pairs:
            key = self._loc(caller)
            if key in seen:
                continue
            seen.add(key)
            fp = caller.file_path
            by_file.setdefault(fp, []).append((caller, edge))
        lines = [f"⬆ {node.name} 的调用者 ({len(seen)} 处, 深度={depth})"]
        for fp in sorted(by_file):
            lines.append("")
            lines.append(f"📄 {fp}")
            for caller, edge in by_file[fp]:
                line_info = f" L{edge.line}" if edge.line else ""
                lines.append(f"  [{caller.kind}] **{caller.name}**{line_info}")
        return "\n".join(lines)

    def _mode_callees(self, cg: CodeGraph, symbol: str, depth: int) -> str:
        node = self._resolve_node(symbol, cg)
        if node is None:
            return f"未找到符号「{symbol}」"
        pairs = cg.get_callees(node.id, depth)
        if not pairs:
            return f"「{node.name}」没有调用其他代码"
        by_file: Dict[str, List] = {}
        seen = set()
        for callee, edge in pairs:
            key = self._loc(callee)
            if key in seen:
                continue
            seen.add(key)
            fp = callee.file_path
            by_file.setdefault(fp, []).append((callee, edge))
        lines = [f"⬇ {node.name} 调用的代码 ({len(seen)} 处, 深度={depth})"]
        for fp in sorted(by_file):
            lines.append("")
            lines.append(f"📄 {fp}")
            for callee, _ in by_file[fp]:
                lines.append(f"  [{callee.kind}] **{callee.name}**  → L{callee.start_line}")
        return "\n".join(lines)

    def _mode_explore(self, cg: CodeGraph, query: str, max_files: int) -> str:
        results = cg.search_nodes(query, SearchOptions(limit=max_files))
        if not results:
            return f"未找到匹配「{query}」的符号"

        from collections import defaultdict

        by_file: Dict[str, List] = defaultdict(list)
        for r in results:
            by_file[r.node.file_path].append(r.node)

        lines = [f"🔬 探索: 「{query}」\n"]
        file_idx = 0
        for filepath, nodes in sorted(by_file.items()):
            file_idx += 1
            lines.append(f"{'─' * 40}")
            lines.append(f"📄 **{filepath}**  ({len(nodes)} 个符号)")
            lines.append(f"{'─' * 40}")
            for n in nodes:
                sig = self._sig(n)
                lines.append(f"  [{n.kind}] **{n.name}**{sig}  L{n.start_line}-{n.end_line}")
                # 调用者摘要
                try:
                    callers = cg.get_callers(n.id)
                    if callers:
                        summary = ", ".join(cn.name for cn, _ in callers[:5])
                        extra = f"    ← 被 {summary}"
                        if len(callers) > 5:
                            extra += f" 等{len(callers)}处"
                        lines.append(extra)
                except Exception:
                    pass
                # 被调用者摘要
                try:
                    callees = cg.get_callees(n.id)
                    if callees:
                        summary = ", ".join(cn.name for cn, _ in callees[:5])
                        extra = f"    → 调用 {summary}"
                        if len(callees) > 5:
                            extra += f" 等{len(callees)}处"
                        lines.append(extra)
                except Exception:
                    pass
                lines.append("")

        lines.append(f"📊 总计: {len(results)} 个匹配，{len(by_file)} 个文件")
        return "\n".join(lines)

    def _mode_impact(self, cg: CodeGraph, symbol: str, depth: int) -> str:
        node = self._resolve_node(symbol, cg)
        if node is None:
            return f"未找到符号「{symbol}」"
        subgraph = cg.get_impact_radius(node.id, depth)
        files = set()
        for n in subgraph.nodes.values():
            if n.file_path:
                files.add(n.file_path)

        lines = [
            f"💥 变更影响分析: 「{symbol}」",
            f"📊 影响范围: {len(subgraph.nodes)} 符号, {len(subgraph.edges)} 关系, {len(files)} 文件 (深度={depth})",
            "",
        ]
        if len(subgraph.nodes) > 1:
            # 按文件分组受影响符号
            by_file: Dict[str, List] = {}
            for n in subgraph.nodes.values():
                if n.id == node.id:
                    continue
                by_file.setdefault(n.file_path, []).append(n)
            lines.append(f"受影响的符号 ({len(subgraph.nodes) - 1}):")
            for fp in sorted(by_file):
                lines.append(f"  📄 {fp}")
                for n in by_file[fp]:
                    lines.append(f"    [{n.kind}] **{n.name}**  L{n.start_line}")
            lines.append("")
        lines.append(f"涉及文件 ({len(files)}):")
        for f in sorted(files):
            lines.append(f"  📄 {f}")
        return "\n".join(lines)

    def _mode_files(self, cg: CodeGraph, directory: Optional[str], by_directory: bool) -> str:
        all_files = cg._queries.get_all_file_paths()
        if directory:
            all_files = [f for f in all_files if f.startswith(directory)]
        if not all_files:
            return "没有已索引的文件"

        if by_directory:
            from collections import defaultdict

            dirs: Dict[str, List[str]] = defaultdict(list)
            for f in sorted(all_files):
                d = os.path.dirname(f) or "."
                dirs[d].append(f)

            lines = [f"📁 已索引文件 ({len(all_files)} 个, {len(dirs)} 个目录)\n"]
            for d in sorted(dirs):
                lines.append(f"**{d}/**")
                for f in dirs[d]:
                    nodes = cg.get_nodes_by_file(f)
                    kinds = {}
                    for n in nodes:
                        k = n.kind
                        kinds[k] = kinds.get(k, 0) + 1
                    kind_str = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()) if k != "file")
                    lines.append(
                        f"  - {os.path.basename(f)}  ({kind_str})" if kind_str else f"  - {os.path.basename(f)}"
                    )
        else:
            lines = [f"📁 已索引文件 ({len(all_files)} 个)\n"]
            for f in sorted(all_files):
                nodes = cg.get_nodes_by_file(f)
                kinds = {}
                for n in nodes:
                    k = n.kind
                    kinds[k] = kinds.get(k, 0) + 1
                kind_str = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()) if k != "file")
                lines.append(f"- {f}  ({kind_str})" if kind_str else f"- {f}")

        return "\n".join(lines)

    # ── 唯一对外入口 ─────────────────────────────────────────────────────

    def codegraph_explore(
        self,
        query: str = "",
        mode: str = "explore",
        depth: int = 2,
        max_files: int = 12,
        kind: Optional[str] = None,
        directory: Optional[str] = None,
        limit: int = 20,
        exact: bool = False,
    ) -> ToolResult:
        """统一代码探索入口 — 通过 mode 参数切换不同能力

        可用 mode:
          - status    查看 CodeGraph 索引状态（文件/节点/边/待同步变更）
          - sync      同步索引与文件系统
          - search    搜索符号，按 kind 过滤（function/class/method/...）
          - callers   查找谁调用了指定符号
          - callees   查找指定符号调用了谁
          - explore（默认）综合探索：搜索 + 源码位置 + 调用/被调用摘要
          - impact    变更影响分析：改一个符号会波及哪些代码
          - files     列出已索引的文件

        Args:
            query: 搜索的符号名或关键词（status/sync/files 模式不需要）
            mode: 操作模式（见上方列表）
            depth: 调用链/影响分析深度（默认 2）
            max_files: explore 模式最大涉及文件数（默认 12）
            kind: search 模式按类型过滤（function/class/method/variable/...）
            directory: files 模式按目录筛选（可选）
            limit: search 模式最大返回数（默认 20）
            exact: search 模式是否精确匹配（默认模糊）
        """
        cg = self._ensure_cg()
        if cg is None:
            if not _HAS_CODEGRAPH:
                return ToolResult(False, error="codegraph-py 未安装，运行: pip install codegraph-py[all]")
            return ToolResult(False, error="CodeGraph 未初始化，请在项目根运行: codegraph init")

        # 查询前自动同步索引（sync/status 模式跳过自循环）
        if mode not in ("sync", "status"):
            try:
                sync_result = cg.sync()
                changed = sync_result.files_added + sync_result.files_modified + sync_result.files_removed
                if changed:
                    logger.info(f"[CodeGraph] 自动同步: {changed} 个文件变更")
            except Exception as e:
                logger.warning(f"[CodeGraph] 自动同步失败（不影响查询）: {e}")

        try:
            if mode == "status":
                content = self._mode_status(cg)
            elif mode == "sync":
                content = self._mode_sync(cg)
            elif mode == "search":
                content = self._mode_search(cg, query, kind, limit, exact)
            elif mode == "callers":
                content = self._mode_callers(cg, query, depth)
            elif mode == "callees":
                content = self._mode_callees(cg, query, depth)
            elif mode == "impact":
                content = self._mode_impact(cg, query, depth)
            elif mode == "files":
                content = self._mode_files(cg, directory, False)
            else:  # explore (default)
                content = self._mode_explore(cg, query, max_files)

            return ToolResult(True, content=content)
        except Exception as e:
            logger.exception(f"[CodeGraph] {mode} 失败")
            return ToolResult(False, error=f"CodeGraph {mode} 失败: {e}")

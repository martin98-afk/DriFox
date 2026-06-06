# -*- coding: utf-8 -*-
"""
工具结果持久化器 - 借鉴 Claude Code 的入口管控策略

核心职责:
  1. 单结果 > 50,000 字符 -> 持久化到磁盘
  2. 消息级预算 > 200,000 字符 -> 按大小排序持久化最大的几个
  3. 替换 content 为 <persisted-output> 块 + 2000 字节预览 + 文件路径
  4. 替换决策冻结 (预览字符串必须稳定 -> 保护 Prompt Cache)

设计原则:
  - 在 ToolExecutor 之后、消息拼接之前执行 ("入口管控")
  - 完全无 LLM API 调用 (磁盘写入是最低代价防线)
  - 失败回退: 持久化失败时保留原 content (不阻塞工具链)

参考:
  - Claude Code src/constants/toolLimits.ts
  - Claude Code 5 层上下文结构
"""
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

import orjson
from loguru import logger

from app.utils.utils import get_app_data_dir


# ============================================================
# 阈值常量 (与 Claude Code 对齐)
# ============================================================
SINGLE_RESULT_THRESHOLD = 50_000         # 单结果 > 50K 字符 -> 持久化
MESSAGE_TOTAL_THRESHOLD = 200_000        # 单条消息合计 > 200K -> 按大小截断
PREVIEW_BYTES = 2_000                    # 预览字节数

# 应被跳过的工具结果 (返回小、结构化、不能被截断)
SKIP_TOOLS = frozenset({
    "question",           # 用户问答结果
    "todowrite",          # todo 写入
    "todoread",           # todo 读取
    "list_skills",        # 技能列表
    "mcp_list_servers",   # MCP 服务器列表
    "skill",              # 技能加载结果
    "read_project_note",  # 项目笔记
    "read_persisted_output",  # 已经恢复过的内容
})


@dataclass
class PersistStats:
    """单轮工具执行的持久化统计"""

    persisted_count: int = 0
    kept_count: int = 0
    total_chars_before: int = 0
    total_chars_after: int = 0
    saved_chars: int = 0
    file_paths: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "persisted_count": self.persisted_count,
            "kept_count": self.kept_count,
            "total_chars_before": self.total_chars_before,
            "total_chars_after": self.total_chars_after,
            "saved_chars": self.saved_chars,
            "file_paths": list(self.file_paths),
        }


class ToolResultPersister:
    """工具结果持久化器 (单例, 按 session_id 隔离)"""

    def __init__(self, session_id: str):
        self._session_id = session_id
        # 路径: {app_data_dir}/projects/{session_id}/tool-results/
        self._root_dir = (
            get_app_data_dir() / "projects" / session_id / "tool-results"
        )
        self._root_dir.mkdir(parents=True, exist_ok=True)
        # 替换决策冻结表: {tool_call_id: persisted_file_path}
        # 后续每轮 LLM 调用都用同一份预览重新注入, 保证前缀字节级稳定
        self._frozen: Dict[str, str] = {}
        logger.info(f"[Persist] 工具结果持久化目录: {self._root_dir}")

    def process(
        self, tool_results: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], PersistStats]:
        """
        入口: 处理一批 tool_result, 返回 (处理后结果, 统计)

        Args:
            tool_results: chat_worker 产出的 list[dict],
                每个 dict 形如:
                {
                    "role": "tool",
                    "tool_call_id": "call_xxx",
                    "name": "read",
                    "content": "...原始内容...",
                    "success": True,
                    ...
                }

        Returns:
            (处理后 tool_results, 统计信息)
        """
        stats = PersistStats()
        if not tool_results:
            return tool_results, stats

        # 0) 统计处理前大小 (规整为 str, 避免非字符串类型让 len 报错)
        for r in tool_results:
            content = r.get("content", "")
            if not isinstance(content, str):
                content = str(content) if content is not None else ""
                r["content"] = content
            stats.total_chars_before += len(content)

        # 1) 第一轮: 单结果截断
        #    把 > 50K 的结果替换为 <persisted-output>
        for r in tool_results:
            name = r.get("name", "")
            content = r.get("content", "") or ""
            content_len = len(content)
            stats.total_chars_after += content_len

            # 跳过规则: 失败结果 / 跳过工具 / 空内容
            if not r.get("success", True):
                stats.kept_count += 1
                continue
            if name in SKIP_TOOLS:
                stats.kept_count += 1
                continue
            if content_len == 0:
                stats.kept_count += 1
                continue

            tc_id = r.get("tool_call_id", "")
            # 已被冻结的 tool_call_id (后续轮次也用同样预览)
            if tc_id in self._frozen:
                persisted_path = self._frozen[tc_id]
                new_content = self._render_persisted_block(
                    original_len=content_len,
                    file_path=persisted_path,
                    preview_text=content,
                )
                stats.saved_chars += content_len - len(new_content)
                r["content"] = new_content
                stats.total_chars_after = (
                    stats.total_chars_after - content_len + len(new_content)
                )
                stats.persisted_count += 1
                stats.file_paths.append(persisted_path)
                continue

            # 单结果 > 50K -> 持久化
            if content_len > SINGLE_RESULT_THRESHOLD:
                try:
                    persisted_path = self._persist(r, content)
                except Exception as e:
                    logger.exception(f"[Persist] 持久化失败, 保留原结果: {e}")
                    stats.kept_count += 1
                    continue
                new_content = self._render_persisted_block(
                    original_len=content_len,
                    file_path=persisted_path,
                    preview_text=content,
                )
                self._frozen[tc_id] = persisted_path
                stats.saved_chars += content_len - len(new_content)
                r["content"] = new_content
                stats.total_chars_after = (
                    stats.total_chars_after - content_len + len(new_content)
                )
                stats.persisted_count += 1
                stats.file_paths.append(persisted_path)
                continue

            stats.kept_count += 1

        # 2) 第二轮: 消息级预算
        #    总和 > 200K 时, 把"最大且未持久化"的几条再持久化
        total_after = sum(
            len(str(r.get("content", "") or "")) for r in tool_results
        )
        if total_after > MESSAGE_TOTAL_THRESHOLD:
            # 按"原始长度"降序, 取仍然超限的 (跳过已持久化的)
            candidates = sorted(
                [
                    r for r in tool_results
                    if r.get("name") not in SKIP_TOOLS
                    and r.get("success", True)
                    and r.get("tool_call_id", "") not in self._frozen
                ],
                key=lambda r: len(str(r.get("content", "") or "")),
                reverse=True,
            )
            for r in candidates:
                if total_after <= MESSAGE_TOTAL_THRESHOLD:
                    break
                content = r.get("content", "") or ""
                content_len = len(content)
                if content_len < 1000:  # 太小的就别持久化了
                    continue
                try:
                    persisted_path = self._persist(r, content)
                except Exception as e:
                    logger.exception(f"[Persist] 消息级持久化失败: {e}")
                    continue
                new_content = self._render_persisted_block(
                    original_len=content_len,
                    file_path=persisted_path,
                    preview_text=content,
                )
                self._frozen[r.get("tool_call_id", "")] = persisted_path
                saved = content_len - len(new_content)
                stats.saved_chars += saved
                total_after -= saved
                stats.persisted_count += 1
                stats.file_paths.append(persisted_path)
                r["content"] = new_content

        # 3) 最终统计
        stats.total_chars_after = sum(
            len(str(r.get("content", "") or "")) for r in tool_results
        )

        if stats.persisted_count > 0:
            logger.info(
                f"[Persist] 持久化 {stats.persisted_count} 条, "
                f"节省 {stats.saved_chars // 1024}KB "
                f"({stats.total_chars_before // 1024}KB -> "
                f"{stats.total_chars_after // 1024}KB), "
                f"冻结 {len(self._frozen)} 个 tool_call_id"
            )

        return tool_results, stats

    # ============== 私有方法 ==============

    def _persist(self, r: Dict[str, Any], content: str) -> str:
        """
        把工具结果完整内容写入磁盘
        Returns: 持久化文件绝对路径
        """
        tc_id = r.get("tool_call_id", "unknown")
        name = r.get("name", "unknown")
        # 用 tc_id + 内容 hash 作为文件名 (避免同 tool_call_id 重复写)
        content_hash = hashlib.sha256(
            content.encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        ts = int(time.time() * 1000)
        filename = f"{tc_id}_{ts}_{content_hash}.txt"
        file_path = self._root_dir / filename

        # 写完整内容
        file_path.write_text(content, encoding="utf-8", errors="replace")
        # 写元信息 (调试/审计用)
        meta = {
            "tool_call_id": tc_id,
            "tool_name": name,
            "arguments": r.get("arguments", {}),
            "original_chars": len(content),
            "saved_at": ts,
            "session_id": self._session_id,
        }
        file_path.with_suffix(".meta.json").write_text(
            orjson.dumps(meta, option=orjson.OPT_INDENT_2).decode("utf-8"),
            encoding="utf-8",
        )
        return str(file_path.resolve())

    def _render_persisted_block(
        self,
        original_len: int,
        file_path: str,
        preview_text: str,
    ) -> str:
        """
        渲染 <persisted-output> 块 - 与 Claude Code 格式对齐

        关键: 同一 file_path + original_len 渲染出的字节必须稳定 (Prompt Cache 友好)

        注意: preview_text 仅用于取前 PREVIEW_BYTES 字节, 不影响最终内容字节稳定性
        """
        preview_bytes = preview_text.encode("utf-8", errors="replace")[:PREVIEW_BYTES]
        # 解码失败时用 errors="replace", 保证不抛异常
        preview = preview_bytes.decode("utf-8", errors="replace")

        size_kb = original_len / 1024
        # ⚠️ 注意: 这里生成的字符串对同一 (file_path, original_len) 是确定的
        # (preview 可能因输入不同而不同, 但 tc_id 已冻结 -> preview 也会冻结)
        return (
            f"<persisted-output>\n"
            f"Output too large ({size_kb:.1f} KB). "
            f"Full output saved to: {file_path}\n"
            f"\n"
            f"Preview (first {PREVIEW_BYTES} B):\n"
            f"{preview}\n"
            f"...\n"
            f"</persisted-output>\n"
            f"\n"
            f"If you need to see the full content, use the "
            f"`read_persisted_output` tool with the file path above."
        )

    def cleanup_session(self) -> None:
        """会话结束时清理 (暂不删除文件, 方便调试; 后续可加保留期策略)"""
        self._frozen.clear()

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
  - 持久化时自动格式化 JSON 内容 (防止单行 JSON 导致 LLM 无法读取的死循环)

参考:
  - Claude Code src/constants/toolLimits.ts
  - Claude Code 5 层上下文结构
"""

import hashlib
import json as stdjson
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

import orjson
from loguru import logger

from app.utils.utils import get_app_data_dir

# ============================================================
# 阈值常量 (与 Claude Code 对齐)
# ============================================================
SINGLE_RESULT_THRESHOLD = 50_000  # 单结果 > 50K 字符 -> 持久化
MESSAGE_TOTAL_THRESHOLD = 200_000  # 单条消息合计 > 200K -> 按大小截断
PREVIEW_BYTES = 2_000  # 预览字节数

# 应被跳过的工具结果 (返回小、结构化、不能被截断)
SKIP_TOOLS = frozenset(
    {
        "question",  # 用户问答结果
        "todowrite",  # todo 写入
        "todoread",  # todo 读取
        "manage_skill",  # 技能管理
        "mcp_list_servers",  # MCP 服务器列表
        "skill",  # 技能加载结果
    }
)


def _shorten_value(val: Any, max_len: int = 120) -> str:
    """将 JSON 值截断为简洁的可读字符串"""
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        if len(val) > max_len:
            return stdjson.dumps(val[:max_len] + "...", ensure_ascii=False)
        return stdjson.dumps(val, ensure_ascii=False)
    if isinstance(val, dict):
        return f"{{{len(val)} keys}}"
    if isinstance(val, list):
        return f"[{len(val)} items]"
    return stdjson.dumps(val, ensure_ascii=False)[:max_len]


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
        self._root_dir = get_app_data_dir() / "projects" / session_id / "tool-results"
        self._root_dir.mkdir(parents=True, exist_ok=True)
        # 替换决策冻结表: {tool_call_id: persisted_file_path}
        # 后续每轮 LLM 调用都用同一份预览重新注入, 保证前缀字节级稳定
        self._frozen: Dict[str, str] = {}
        logger.info(f"[Persist] 工具结果持久化目录: {self._root_dir}")

    def process(self, tool_results: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], PersistStats]:
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
            # 🔒 防递归: 内容已经是 <persisted-output> 块, 不再重复固化
            # 避免: 读取→超长结果→又固化结果 的死循环
            if content.lstrip().startswith("<persisted-output>"):
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
                stats.total_chars_after = stats.total_chars_after - content_len + len(new_content)
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
                stats.total_chars_after = stats.total_chars_after - content_len + len(new_content)
                stats.persisted_count += 1
                stats.file_paths.append(persisted_path)
                continue

            stats.kept_count += 1

        # 2) 第二轮: 消息级预算
        #    总和 > 200K 时, 把"最大且未持久化"的几条再持久化
        total_after = sum(len(str(r.get("content", "") or "")) for r in tool_results)
        if total_after > MESSAGE_TOTAL_THRESHOLD:
            # 按"原始长度"降序, 取仍然超限的 (跳过已持久化的)
            candidates = sorted(
                [
                    r
                    for r in tool_results
                    if r.get("name") not in SKIP_TOOLS
                    and r.get("success", True)
                    and r.get("tool_call_id", "") not in self._frozen
                    and not str(r.get("content", "") or "").lstrip().startswith("<persisted-output>")
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
        stats.total_chars_after = sum(len(str(r.get("content", "") or "")) for r in tool_results)

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

    @staticmethod
    def _try_format_content(content: str, wrap_width: int = 100) -> str:
        """
        尝试格式化内容以提高 LLM 可读性。

        - JSON 内容: prettify 为带缩进的多行格式
        - 非 JSON 超长单行: 在合理宽度强制换行, 防止 LLM 无法定位行尾

        核心目的: 避免单行超长内容写入磁盘后, 读取时拿到单行巨文无法解析,
        导致"读取→超长结果→再次固化"的死循环。
        """
        stripped = content.strip()
        # 1) JSON 内容 -> prettify
        if stripped and stripped[0] in ("{", "["):
            try:
                parsed = orjson.loads(stripped)
                return orjson.dumps(parsed, option=orjson.OPT_INDENT_2).decode("utf-8")
            except (orjson.JSONDecodeError, TypeError, ValueError):
                pass

        # 2) 非 JSON 超长单行 -> 强制换行
        #    如果全文没有换行符且超长, 在 wrap_width 处插入换行
        #    (对 base64 等无自然断点的内容尤为关键)
        if "\n" not in content and len(content) > wrap_width:
            lines: List[str] = []
            for i in range(0, len(content), wrap_width):
                lines.append(content[i : i + wrap_width])
            return "\n".join(lines)

        return content

    def _persist(self, r: Dict[str, Any], content: str) -> str:
        """
        把工具结果完整内容写入磁盘
        Returns: 持久化文件绝对路径
        """
        tc_id = r.get("tool_call_id", "unknown")
        name = r.get("name", "unknown")

        # 写入前格式化: 如果内容是 JSON, prettify 为多行格式
        formatted = self._try_format_content(content)

        # 用格式化后的内容计算 hash
        content_hash = hashlib.sha256(formatted.encode("utf-8", errors="replace")).hexdigest()[:16]
        ts = int(time.time() * 1000)
        filename = f"{tc_id}_{ts}_{content_hash}.txt"
        file_path = self._root_dir / filename

        # 写完整内容 (已格式化为多行, LLM 可直接读取)
        file_path.write_text(formatted, encoding="utf-8", errors="replace")
        # 写元信息 (调试/审计用)
        meta = {
            "tool_call_id": tc_id,
            "tool_name": name,
            "arguments": r.get("arguments", {}),
            "original_chars": len(content),
            "formatted_chars": len(formatted),
            "was_formatted": formatted != content,
            "saved_at": ts,
            "session_id": self._session_id,
        }
        file_path.with_suffix(".meta.json").write_text(
            orjson.dumps(meta, option=orjson.OPT_INDENT_2).decode("utf-8"),
            encoding="utf-8",
        )
        return str(file_path.resolve())

    @staticmethod
    def _render_json_preview(content: str, max_chars: int = 2000) -> str:
        """
        为 JSON 内容生成结构化预览 (而非原始字节截断)。

        对于单行 JSON 这种 LLM 无法直接解析的格式，生成带结构的预览，
        展示根级 key/array 数量和部分示例值，避免截断在 JSON 中间。

        Returns:
            格式化后的预览字符串 (可能为空: 非 JSON 或解析失败)
        """
        stripped = content.strip()
        if not stripped or stripped[0] not in ("{", "["):
            return ""

        try:
            parsed = orjson.loads(stripped)
        except (orjson.JSONDecodeError, TypeError, ValueError):
            return ""

        # 收集结构信息
        lines: List[str] = []
        if isinstance(parsed, dict):
            lines.append(f"JSON Object with {len(parsed)} keys:")
            # 展示前几对 key-value (值太长则截断)
            for i, (k, v) in enumerate(parsed.items()):
                if i >= 8:  # 最多展示 8 个 key
                    remaining = len(parsed) - i
                    lines.append(f"  ... and {remaining} more keys")
                    break
                val_str = _shorten_value(v, 120)
                lines.append(f'  "{k}": {val_str}')
        elif isinstance(parsed, list):
            lines.append(f"JSON Array with {len(parsed)} items:")
            for i, item in enumerate(parsed):
                if i >= 6:  # 最多展示 6 个
                    remaining = len(parsed) - i
                    lines.append(f"  ... and {remaining} more items")
                    break
                val_str = _shorten_value(item, 160)
                lines.append(f"  [{i}] {val_str}")

        preview = "\n".join(lines)
        # 确保不超过 max_chars
        if len(preview) > max_chars:
            preview = preview[:max_chars] + "\n... (truncated)"

        return preview

    def _render_persisted_block(
        self,
        original_len: int,
        file_path: str,
        preview_text: str,
    ) -> str:
        """
        渲染 <persisted-output> 块 - 与 Claude Code 格式对齐

        关键: 同一 file_path + original_len 渲染出的字节必须稳定 (Prompt Cache 友好)

        增强:
          - JSON 内容自动转为结构化预览 (展示 key/数量/示例值)
          - 非 JSON 内容保持原始字节截断
        """
        size_kb = original_len / 1024

        # 尝试生成 JSON 结构化预览
        json_preview = self._render_json_preview(preview_text, max_chars=PREVIEW_BYTES)

        if json_preview:
            # JSON 结构化预览
            block = (
                f"<persisted-output>\n"
                f"Output too large ({size_kb:.1f} KB). "
                f"Full output saved to: {file_path}\n"
                f"\n"
                f"Preview (structured):\n"
                f"{json_preview}\n"
                f"</persisted-output>\n"
                f"\n"
                f"The full content was truncated due to size limits. "
                f"Use the file path above to read it if needed."
            )
        else:
            # 非 JSON: 原始字节截断
            preview_bytes = preview_text.encode("utf-8", errors="replace")[:PREVIEW_BYTES]
            preview = preview_bytes.decode("utf-8", errors="replace")
            block = (
                f"<persisted-output>\n"
                f"Output too large ({size_kb:.1f} KB). "
                f"Full output saved to: {file_path}\n"
                f"\n"
                f"Preview (first {PREVIEW_BYTES} B):\n"
                f"{preview}\n"
                f"...\n"
                f"</persisted-output>\n"
                f"\n"
                f"The full content was truncated due to size limits. "
                f"Use the file path above to read it if needed."
            )

        return block

    def cleanup_session(self) -> None:
        """会话结束时清理 (暂不删除文件, 方便调试; 后续可加保留期策略)"""
        self._frozen.clear()

/**
 * useStreamingMessage：流式会话的 settled/active 切分 + 250ms 静默升级 + finished 定型。
 *
 * - delta 密集期：全部走 LiveTail 命令式通道，零 React 重渲
 * - 250ms 无 delta：活动段固化 settledText（跑管线缓存住），settledLen 边界前移
 * - finished：全量定型写 chatLog（blocks 快照含 reasoning/tool_result），清流式态
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { useStreamStore, type StreamPhase } from "../../stores/streamStore";
import { useChatLog } from "./chatLog";
import { fingerprint } from "./fingerprint";
import type { MessageBlock, StreamingToolSnapshot } from "./blocks";

const SETTLE_SILENCE_MS = 250;

export interface StreamingMessageState {
  phase: StreamPhase;
  /** 尚无任何 delta（骨架态） */
  awaitingFirstDelta: boolean;
  /** 已固化正文（升级后的 stable 段） */
  settledText: string;
  /** settled 边界（LiveTail 截取起点），ref 保证命令式通道读到最新 */
  settledLenRef: { current: number };
  /** 工具卡快照（toolOrder 序，useShallow 保引用稳定） */
  tools: StreamingToolSnapshot[];
}

export function useStreamingMessage(sid: string, opts?: { model?: string }): StreamingMessageState {
  const [settledText, setSettledText] = useState("");
  const settledLenRef = useRef(0);
  const finalizedRef = useRef(false);
  const modelRef = useRef(opts?.model);

  const phase = useStreamStore((st) => st.streams[sid]?.phase ?? "idle");

  // await 首包判断（低频：phase/首 delta 各触发一次重算）
  const awaitingFirstDelta = useStreamStore((st) => {
    const s = st.streams[sid];
    if (!s) return false;
    return s.phase === "streaming" && s.content === "" && s.pendingContent === "" && s.reasoning === "" && s.pendingReasoning === "";
  });

  useEffect(() => {
    let timer: number | null = null;
    // 上次工具数快照：toolOrder 增长 = 新块顶替活动段（R4 🟡4）→ 立即升级不等 250ms
    let lastToolCount = 0;

    /** 升级：固化 committed 前缀到 settled（250ms 静默 / 块切换共用） */
    const settleNow = () => {
      const cur = useStreamStore.getState().streams[sid];
      if (!cur) return;
      const nextLen = cur.content.length;
      if (nextLen > settledLenRef.current) {
        settledLenRef.current = nextLen;
        setSettledText(cur.content);
      }
    };

    const finalize = () => {
      if (finalizedRef.current) return;
      finalizedRef.current = true;
      const s = useStreamStore.getState().streams[sid];
      if (!s) return;
      const blocks: MessageBlock[] = [];
      if (s.reasoning) blocks.push({ type: "reasoning", content: s.reasoning });
      for (const id of s.toolOrder) {
        const t = s.tools[id];
        if (!t) continue;
        blocks.push({ type: "tool_result", tool_call_id: t.toolCallId, tool_name: t.toolName, result: t.result ?? t.argsText });
      }
      if (s.content) blocks.push({ type: "text", content: s.content });
      if (blocks.length === 0) return;
      useChatLog.getState().appendMessage(sid, {
        id: fingerprint(sid, "assistant", s.lastSeq, s.content.length),
        role: "assistant",
        blocks,
        model: modelRef.current,
        ts: Date.now(),
      });
      // 清流式态：定型卡顶上，流式视图卸载
      useStreamStore.getState().clearSession(sid);
    };

    const un = useStreamStore.subscribe((st) => {
      const s = st.streams[sid];
      if (!s) return;

      if (s.phase === "finished" || s.phase === "error") {
        if (timer !== null) clearTimeout(timer);
        finalize();
        return;
      }

      if (s.phase === "streaming") {
        // 块切换即升级（R4 🟡4）：tool.started 新块插入顶替活动段，立即固化
        if (s.toolOrder.length > lastToolCount) {
          lastToolCount = s.toolOrder.length;
          if (timer !== null) clearTimeout(timer);
          settleNow();
          return;
        }
        // 250ms 静默升级：delta 每次重置定时器，静默到期固化 committed 前缀
        if (timer !== null) clearTimeout(timer);
        timer = window.setTimeout(() => {
          settleNow();
        }, SETTLE_SILENCE_MS);
      }
    });

    return () => {
      un();
      if (timer !== null) clearTimeout(timer);
    };
  }, [sid]);

  // 会话级 tools 快照：useShallow 保引用稳定（reduceStream 中未变化的 tool 对象保持引用）
  const tools = useStreamStore(
    useShallow((st) => {
      const s = st.streams[sid];
      if (!s) return EMPTY_TOOLS;
      return s.toolOrder.map((id) => s.tools[id]).filter((t): t is NonNullable<typeof t> => Boolean(t));
    }),
  );

  return useMemo(
    () => ({ phase, awaitingFirstDelta, settledText, settledLenRef, tools }),
    [phase, awaitingFirstDelta, settledText, tools],
  );
}

const EMPTY_TOOLS: never[] = [];

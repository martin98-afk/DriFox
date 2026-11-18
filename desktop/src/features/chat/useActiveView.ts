/**
 * useActiveView：ChatArea 唯一订阅点（T10 数据流走线）。
 * 组合 chatLog（定型）+ streamStore（流式）产出渲染 items。
 */

import { useMemo } from "react";
import { useStreamStore } from "../../stores/streamStore";
import { useChatLog } from "./chatLog";
import { useStreamingMessage, type StreamingMessageState } from "./useStreamingMessage";
import type { ChatMessage } from "./blocks";

export interface ActiveItem {
  kind: "msg" | "streaming" | "skeleton";
  msg?: ChatMessage;
  index: number;
}

export interface ActiveView {
  items: ActiveItem[];
  streaming: boolean;
  /** 流式态（settled/骨架判断 + LiveTail 边界 ref + tools 快照） */
  streamingState: StreamingMessageState;
}

export function useActiveView(sid: string | null, opts?: { model?: string }): ActiveView {
  const messages = useChatLog((st) => (sid ? st.messagesBySid[sid] : undefined)) ?? EMPTY_MSGS;
  const streaming = useStreamStore((st) => (sid ? st.streams[sid]?.phase === "streaming" : false));
  const streamingState = useStreamingMessage(sid ?? "", { model: opts?.model });

  return useMemo(() => {
    const items: ActiveItem[] = messages.map((m, index) => ({ kind: "msg" as const, msg: m, index }));
    if (sid && streaming) {
      items.push({ kind: streamingState.awaitingFirstDelta ? "skeleton" : "streaming", index: items.length });
    }
    return { items, streaming, streamingState };
  }, [sid, messages, streaming, streamingState]);
}

const EMPTY_MSGS: ChatMessage[] = [];

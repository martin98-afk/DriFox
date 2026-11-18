/**
 * 会话消息日志（features/chat 内轻量 store）：定型消息 per-sid 缓存。
 * 与 F1 stores/ 解耦（F2 边界不动 stores/），useActiveView 组合本源与 streamStore。
 */

import { create } from "zustand";
import type { ChatMessage } from "./blocks";

interface ChatLogState {
  messagesBySid: Record<string, ChatMessage[]>;
  /** 追加定型消息（message.received / 流式 finished 定型 / 用户发送） */
  appendMessage: (sid: string, msg: ChatMessage) => void;
  /** 替换最后一条（定型覆盖占位时用） */
  replaceLast: (sid: string, msg: ChatMessage) => void;
  clear: (sid: string) => void;
}

export const useChatLog = create<ChatLogState>((set) => ({
  messagesBySid: {},

  appendMessage: (sid, msg) =>
    set((st) => {
      const list = st.messagesBySid[sid] ?? [];
      return { messagesBySid: { ...st.messagesBySid, [sid]: [...list, msg] } };
    }),

  replaceLast: (sid, msg) =>
    set((st) => {
      const list = st.messagesBySid[sid] ?? [];
      if (list.length === 0) return { messagesBySid: { ...st.messagesBySid, [sid]: [msg] } };
      const next = [...list];
      next[next.length - 1] = msg;
      return { messagesBySid: { ...st.messagesBySid, [sid]: next } };
    }),

  clear: (sid) =>
    set((st) => {
      const { [sid]: _removed, ...rest } = st.messagesBySid;
      return { messagesBySid: rest };
    }),
}));

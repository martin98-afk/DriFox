/**
 * 流式域 store：per-sid 流状态机 + sid 路由收口。
 *
 * 职责：
 * - delta 双缓冲：content/reasoning 各设 pending 缓冲，rAF 每帧批量提交，避免逐 token 重渲染
 * - per-sid seq 游标：回放与实时双路径共用去重
 * - tool 卡状态机：started(构建) → args_delta(累积) → result(落定)
 * - permission/question 队列：入队等 UI 处理，consume 后出队
 * - sid 路由收口：非当前会话事件入 backlog，switchSession 时按 seq 升序回放
 *
 * 依赖方向：streamStore → sessionStore（读 currentSid / 切换时 setCurrent），反向禁止。
 */

import { create } from "zustand";
import type {
  ContextUpdatedPayload,
  PermissionRequestedPayload,
  QuestionAskedPayload,
  RetryStatusPayload,
  ToolArgsDeltaPayload,
  ToolResultPayload,
  ToolStartedPayload,
  WsEnvelope,
} from "../api/ws";
import { useSessionStore } from "./sessionStore";

// ---------- 类型 ----------

export type StreamPhase = "idle" | "streaming" | "finished" | "error";
export type ToolCardPhase = "running" | "done";

export interface ToolCard {
  toolCallId: string;
  toolName: string;
  arguments: unknown;
  /** args_delta 累积的参数文本（arguments 结构化视图的流式补充） */
  argsText: string;
  result: unknown;
  phase: ToolCardPhase;
}

export interface PermissionItem {
  toolCallId: string;
  toolName: string;
  arguments: unknown;
  /** F8：b 表定稿顶层字段（原在 arguments 内，联调后 arguments 兑底可下版删） */
  riskLevel?: "danger" | "warn" | "info";
  previewPayload?: unknown;
}

export interface QuestionItem {
  id: string;
  questions: unknown;
  extra?: unknown;
}

export interface RetryInfo {
  errorType: string;
  attempt: number;
  maxRetries: number;
  waitTime: number;
}

export interface SidStream {
  sid: string;
  /** 已提交（flush 后）正文 */
  content: string;
  /** 已提交思考文本 */
  reasoning: string;
  /** 双缓冲：待提交增量 */
  pendingContent: string;
  pendingReasoning: string;
  phase: StreamPhase;
  /** per-sid 下行 seq 游标 */
  lastSeq: number;
  tools: Record<string, ToolCard>;
  toolOrder: string[];
  permissions: PermissionItem[];
  questions: QuestionItem[];
  retry: RetryInfo | null;
  error: string | null;
  contextUsage: { tokenCount: number; limit: number } | null;
}

// ---------- 纯 reducer ----------

function emptyStream(sid: string): SidStream {
  return {
    sid,
    content: "",
    reasoning: "",
    pendingContent: "",
    pendingReasoning: "",
    phase: "idle",
    lastSeq: 0,
    tools: {},
    toolOrder: [],
    permissions: [],
    questions: [],
    retry: null,
    error: null,
    contextUsage: null,
  };
}

/** 把 pending 并入 committed（finished / 回放落定 / 手动同步共用） */
function commit(s: SidStream): SidStream {
  return {
    ...s,
    content: s.content + s.pendingContent,
    reasoning: s.reasoning + s.pendingReasoning,
    pendingContent: "",
    pendingReasoning: "",
  };
}

function reduceStream(s: SidStream, env: WsEnvelope): SidStream {
  // per-sid seq 去重：<= 游标丢弃（seq<=0 视为无游标事件照常处理）
  if (env.seq > 0) {
    if (env.seq <= s.lastSeq) return s;
  }
  const base: SidStream = { ...s, lastSeq: Math.max(s.lastSeq, env.seq) };

  switch (env.name) {
    case "stream.started":
      return { ...base, phase: "streaming", error: null, retry: null };

    case "stream.content_delta":
      return { ...base, pendingContent: base.pendingContent + (env.payload as { text: string }).text };

    case "stream.reasoning_delta":
      return { ...base, pendingReasoning: base.pendingReasoning + (env.payload as { text: string }).text };

    case "stream.finished":
      // final_text 为最终全文，覆盖并同步清空缓冲
      return { ...commit(base), phase: "finished", content: (env.payload as { final_text: string }).final_text };

    case "tool.started": {
      const p = env.payload as ToolStartedPayload;
      if (base.tools[p.tool_call_id]) return base; // 已存在不重建（重发场景）
      return {
        ...base,
        tools: {
          ...base.tools,
          [p.tool_call_id]: {
            toolCallId: p.tool_call_id,
            toolName: p.tool_name,
            arguments: p.arguments,
            argsText: "",
            result: null,
            phase: "running",
          },
        },
        toolOrder: [...base.toolOrder, p.tool_call_id],
      };
    }

    case "tool.args_delta": {
      const p = env.payload as ToolArgsDeltaPayload;
      const t = base.tools[p.tool_call_id];
      if (!t) return base;
      return { ...base, tools: { ...base.tools, [p.tool_call_id]: { ...t, argsText: t.argsText + p.delta } } };
    }

    case "tool.result": {
      const p = env.payload as ToolResultPayload;
      const t = base.tools[p.tool_call_id];
      if (!t) return base;
      return { ...base, tools: { ...base.tools, [p.tool_call_id]: { ...t, result: p.result, phase: "done" } } };
    }

    case "permission.requested": {
      const p = env.payload as PermissionRequestedPayload;
      if (base.permissions.some((x) => x.toolCallId === p.tool_call_id)) return base;
      return {
        ...base,
        permissions: [
          ...base.permissions,
          {
            toolCallId: p.tool_call_id,
            toolName: p.tool_name,
            arguments: p.arguments,
            riskLevel: p.risk_level,
            previewPayload: p.preview_payload,
          },
        ],
      };
    }

    case "question.asked": {
      const p = env.payload as QuestionAskedPayload;
      if (base.questions.some((x) => x.id === p.id)) return base;
      return { ...base, questions: [...base.questions, { id: p.id, questions: p.questions, extra: p.extra }] };
    }

    case "retry.status": {
      const p = env.payload as RetryStatusPayload;
      return {
        ...base,
        retry: { errorType: p.error_type, attempt: p.attempt, maxRetries: p.max_retries, waitTime: p.wait_time },
      };
    }

    case "retry.resolved":
      return { ...base, retry: null };

    case "conversation.error":
      return { ...commit(base), phase: "error", error: (env.payload as { error: string }).error };

    case "context.updated": {
      const p = env.payload as ContextUpdatedPayload;
      return { ...base, contextUsage: { tokenCount: p.token_count, limit: p.limit } };
    }

    default:
      // 其余事件不入流式域（session 域归 sessionStore，remote-ui/toast/ui.exclusive 归 App 层）
      return base;
  }
}

function applyToStream(streams: Record<string, SidStream>, env: WsEnvelope): Record<string, SidStream> {
  const sid = env.sid;
  if (!sid) return streams;
  const prev = streams[sid] ?? emptyStream(sid);
  const next = reduceStream(prev, env);
  return { ...streams, [sid]: next };
}

// ---------- rAF 批量 flush（模块级调度，非响应式） ----------

const dirtySids = new Set<string>();
let rafId: number | null = null;

function scheduleFlush(sid: string): void {
  dirtySids.add(sid);
  if (rafId !== null) return;
  rafId = requestAnimationFrame(() => {
    rafId = null;
    const sids = [...dirtySids];
    dirtySids.clear();
    useStreamStore.getState().flush(sids);
  });
}

// ---------- store ----------

interface StreamState {
  streams: Record<string, SidStream>;
  /** 非当前会话事件的积压队列，switchSession 时按 seq 回放 */
  backlog: WsEnvelope[];
  /** sid 路由收口：WS 业务事件统一入口 */
  applyEvent: (env: WsEnvelope) => void;
  /** rAF 到点批量提交指定会话的 pending */
  flush: (sids: string[]) => void;
  /** 手动同步提交（发送前/落屏前调用） */
  flushNow: (sid: string) => void;
  /** 切换会话收口：setCurrent + 该 sid backlog 按 seq 回放 + 同步提交 */
  switchSession: (sid: string) => void;
  /** UI 决策权限后出队 */
  consumePermission: (sid: string, toolCallId: string) => void;
  /** UI 回答问题后出队 */
  consumeQuestion: (sid: string, id: string) => void;
  /** 会话删除时清理流状态与积压 */
  clearSession: (sid: string) => void;
}

export const useStreamStore = create<StreamState>((set) => ({
  streams: {},
  backlog: [],

  applyEvent: (env) => {
    const sid = env.sid;
    if (!sid) return; // 无 sid 的控制事件不走流式域
    const current = useSessionStore.getState().currentSid;
    if (current !== sid) {
      set((st) => ({ backlog: [...st.backlog, env] }));
      return;
    }
    set((st) => ({ streams: applyToStream(st.streams, env) }));
    if (env.name === "stream.content_delta" || env.name === "stream.reasoning_delta") {
      scheduleFlush(sid);
    }
  },

  flush: (sids) =>
    set((st) => {
      const streams = { ...st.streams };
      for (const sid of sids) {
        const s = streams[sid];
        if (s && (s.pendingContent || s.pendingReasoning)) streams[sid] = commit(s);
      }
      return { streams };
    }),

  flushNow: (sid) =>
    set((st) => {
      const s = st.streams[sid];
      if (!s || (!s.pendingContent && !s.pendingReasoning)) return st;
      return { streams: { ...st.streams, [sid]: commit(s) } };
    }),

  switchSession: (sid) => {
    useSessionStore.getState().setCurrent(sid);
    set((st) => {
      const mine = st.backlog
        .filter((e) => e.sid === sid)
        .sort((a, b) => a.seq - b.seq);
      if (mine.length === 0) return st;
      let streams = st.streams;
      for (const env of mine) streams = applyToStream(streams, env);
      // 回放落定：delta 直接提交，UI 一步到位
      const s = streams[sid];
      if (s) streams = { ...streams, [sid]: commit(s) };
      return { streams, backlog: st.backlog.filter((e) => e.sid !== sid) };
    });
  },

  consumePermission: (sid, toolCallId) =>
    set((st) => {
      const s = st.streams[sid];
      if (!s) return st;
      return {
        streams: {
          ...st.streams,
          [sid]: { ...s, permissions: s.permissions.filter((x) => x.toolCallId !== toolCallId) },
        },
      };
    }),

  consumeQuestion: (sid, id) =>
    set((st) => {
      const s = st.streams[sid];
      if (!s) return st;
      return {
        streams: {
          ...st.streams,
          [sid]: { ...s, questions: s.questions.filter((x) => x.id !== id) },
        },
      };
    }),

  clearSession: (sid) =>
    set((st) => {
      const { [sid]: _removed, ...streams } = st.streams;
      return { streams, backlog: st.backlog.filter((e) => e.sid !== sid) };
    }),
}));

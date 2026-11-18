/**
 * WebSocket 客户端：对齐 DriFox 网关服务契约。
 *
 * 协议要点：
 * - 端点 ws://127.0.0.1:{port}/ws?since={last_seq}；端口读 window.__DRIFOX_SERVER__.port，缺省 fallback 8765
 * - 信封双向统一 {v:1, type:"event"|"client_cmd"|"ack"|"error", name, seq, ts, sid, payload}
 * - 连接建立服务端首推 hello{server_version, seq, active_sessions}
 * - 上行 client_cmd（send/stop/permission/ping）逐条 ack，payload 带客户端 cid
 * - 下行按 seq 去重升序（seq <= 游标的事件丢弃）
 * - 收 resync 或 hello.seq 跳变（游标回退）→ 触发 onResyncNeeded，由上层全量拉取刷新
 * - 重连指数退避 1s→10s，重连 URL 带 since={last_seq}
 */

// ---------- 全局注入 ----------

declare global {
  interface Window {
    /** Tauri 主进程注入的服务端信息；缺省 fallback VITE_SERVER_PORT / 8765 */
    __DRIFOX_SERVER__?: { port: number; token?: string };
  }
}

/** 解析服务端端口（__DRIFOX_SERVER__.port ?? VITE_SERVER_PORT ?? 8765）；REST 同源复用 */
export function resolveServerPort(): number {
  const envPort = Number(import.meta.env.VITE_SERVER_PORT);
  return window.__DRIFOX_SERVER__?.port ?? (Number.isFinite(envPort) && envPort > 0 ? envPort : 8765);
}

/** 解析 WS 握手 token（__DRIFOX_SERVER__.token ?? VITE_SERVER_TOKEN；M2b：query token 不匹配 close 4401） */
export function resolveServerToken(): string {
  return window.__DRIFOX_SERVER__?.token ?? String(import.meta.env.VITE_SERVER_TOKEN ?? "");
}

// ---------- 连接状态 ----------

export type WsStatus = "idle" | "connecting" | "open" | "closed" | "reconnecting";

// ---------- 信封 ----------

export type EnvelopeType = "event" | "client_cmd" | "ack" | "error";

export interface WsEnvelope<P = unknown> {
  v: 1;
  type: EnvelopeType;
  name: string;
  seq: number;
  ts: number;
  /** 业务事件携带 session_id；hello / 控制类为 null。全局事件服务端用空串 */
  sid: string | null;
  payload: P;
  /** M2b 实装：上行 client_cmd 的命令参数读 args 字段（payload 同值保留兼容） */
  args?: unknown;
  /** 上行 client_cmd 的客户端 id；服务端 ack 原样带回 payload.cid */
  cid?: string;
}

// ---------- 领域类型（REST / WS 共用，单源在此） ----------

export interface SessionInfo {
  id: string;
  title?: string;
  project?: string;
  agent?: string;
  created_at?: string;
  updated_at?: string;
  [key: string]: unknown;
}

export interface MessageInfo {
  id?: string;
  role?: string;
  content?: string;
  ts?: number;
  [key: string]: unknown;
}

// ---------- 下行事件 payload（discriminated union 按 name 判别） ----------

export interface HelloPayload {
  server_version: string;
  active_sessions: number;
  /** M2b 🟡2 修正：hello payload 不再携带 seq（信封 seq 承载，双 seq 错位会诱发误判跳变） */
}

/** 服务端要求全量刷新（重连补漏失败 / 游标失配时下发） */
export interface ResyncPayload {
  reason?: string;
}

export interface SessionDeletedPayload {
  session_id: string;
}

export interface MessagesUpdatedPayload {
  messages: MessageInfo[];
}

export interface StreamContentDeltaPayload {
  text: string;
}

export interface StreamReasoningDeltaPayload {
  text: string;
}

export interface StreamFinishedPayload {
  final_text: string;
}

export interface ToolStartedPayload {
  tool_call_id: string;
  tool_name: string;
  arguments: unknown;
}

export interface ToolArgsDeltaPayload {
  tool_call_id: string;
  delta: string;
}

export interface ToolResultPayload {
  tool_call_id: string;
  tool_name: string;
  result: unknown;
}

export interface PermissionRequestedPayload {
  tool_call_id: string;
  tool_name: string;
  arguments: unknown;
  /** M2b b/c 表定稿补充 */
  preview_payload?: unknown;
  risk_level?: "danger" | "warn" | "info";
}

export interface QuestionAskedPayload {
  id: string;
  questions: unknown;
  extra?: unknown;
}

export interface RetryStatusPayload {
  error_type: string;
  attempt: number;
  max_retries: number;
  wait_time: number;
}

export interface ContextUpdatedPayload {
  token_count: number;
  limit: number;
}

export interface ConversationErrorPayload {
  error: string;
}

export interface ErrorMessagePayload {
  message: string;
}

export interface InterjectRecoveredPayload {
  count: number;
}

export interface RemoteUiRenderPayload {
  window_id: string;
  revision: number;
  tree: unknown;
}

export interface RemoteUiPatchPayload {
  window_id: string;
  revision: number;
  ops: unknown;
}

export interface RemoteUiResyncPayload {
  window_id: string;
}

/** 上行：插件远程 UI 交互回传 */
export interface RemoteUiActionPayload {
  window_id: string;
  node_id: string;
  event: string;
  value?: unknown;
  handler_id?: string;
}

export interface ToastShowPayload {
  title: string;
  message: string;
  level?: string;
}

/** M2b b 表定稿：file.pick（上行回传选中文件） */
export interface FilePickPayload {
  window_id: string;
  node_id: string;
  /** 裸 base64 无前缀 */
  data_b64: string;
  name: string;
}

/** M2b b 表定稿：排队消息更新（live 通道） */
export interface QueueUpdatedPayload {
  items: Array<{ msg_id: string; summary: string }>;
}

export interface UiExclusivePayload {
  source_id: string;
  entered: boolean;
}

/** M2b b 表定稿：压缩状态 */
export interface CompactionStatusPayload {
  phase: "running" | "done";
  percent?: number;
  message?: string;
}

/** M2b b 表定稿：hook 状态 */
export interface HookStatusPayload {
  event_name: string;
  status_message: string;
  is_start: boolean;
  output?: string;
}

/** M2b b 表定稿：空 payload 事件（sid 信封承载） */
export interface EmptyPayload {
  [key: string]: never;
}

/**
 * 下行事件表：key 即信封 name。T13 b/c 表定稿项全部 typed，unknown 宽松占位清零；
 * 服务端字段若有出入联调时只改本表一处。
 */
export interface EventMap {
  "hello": HelloPayload;
  "resync": ResyncPayload;
  "session.created": SessionInfo;
  "session.changed": SessionInfo;
  "session.deleted": SessionDeletedPayload;
  "message.received": MessageInfo;
  "messages.updated": MessagesUpdatedPayload;
  "stream.started": EmptyPayload;
  "stream.content_delta": StreamContentDeltaPayload;
  "stream.reasoning_delta": StreamReasoningDeltaPayload;
  "stream.finished": StreamFinishedPayload;
  "tool.started": ToolStartedPayload;
  "tool.args_delta": ToolArgsDeltaPayload;
  "tool.result": ToolResultPayload;
  "permission.requested": PermissionRequestedPayload;
  "question.asked": QuestionAskedPayload;
  "retry.status": RetryStatusPayload;
  "retry.resolved": EmptyPayload;
  "compaction.status": CompactionStatusPayload;
  "context.updated": ContextUpdatedPayload;
  "conversation.error": ConversationErrorPayload;
  "error": ErrorMessagePayload;
  "executor.ready": EmptyPayload;
  "hook.status": HookStatusPayload;
  "interject.recovered": InterjectRecoveredPayload;
  "queue.updated": QueueUpdatedPayload;
  "remote-ui.render": RemoteUiRenderPayload;
  "remote-ui.patch": RemoteUiPatchPayload;
  "remote-ui.resync": RemoteUiResyncPayload;
  "remote-ui.action": RemoteUiActionPayload;
  "file.pick": FilePickPayload;
  "toast.show": ToastShowPayload;
  "ui.exclusive": UiExclusivePayload;
}

export type ServerEventName = keyof EventMap;

/** 分发给 handler 的信封元信息（payload 之外的字段） */
export interface EventMeta {
  seq: number;
  ts: number;
  sid: string | null;
}

// ---------- 上行 client_cmd ----------

export type ClientCmdName = keyof ClientCmdMap;

export interface ClientCmdMap {
  send: { session_id: string; content: string; agent?: string };
  /** 契约未细写 payload，按会话级停止建模 */
  stop: { session_id: string };
  /** T13 b 表双通道定稿；M2b 服务端实装仅 ping/send/stop，本命令待服务端补（发未知命令服务端回 error ack ok:false） */
  permission: { tool_call_id: string; decision: "approve" | "deny"; auto_allow?: string; session_allow?: boolean; reason?: string };
  /** M2b 实装：args 透传 Qt send/stop */
  ping: Record<string, never>;
  /** F7 补：live 删除排队消息上行（T13 b 表；服务端待实装） */
  "queue.remove": { msg_id: string };
  /** F7 补：WS 通道提问应答（T13 b 表；REST /api/questions/{id}/answer 已实装，双通道二选一） */
  question_answer: { question_id: string; answers: string[] };
}

/** ack 载荷：逐条带回客户端 cid */
export interface AckPayload {
  cid?: string;
  ok?: boolean;
  [key: string]: unknown;
}

// ---------- 常量 ----------

const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 10000;
const ACK_TIMEOUT_MS = 10000;

type EventHandler = (payload: never, meta: EventMeta) => void;

// ---------- WsClient ----------

export class WsClient {
  private ws: WebSocket | null = null;
  private handlers = new Map<string, Set<EventHandler>>();
  private statusListeners = new Set<(s: WsStatus) => void>();
  private resyncListeners = new Set<(reason: string) => void>();
  private anyListeners = new Set<(env: WsEnvelope) => void>();
  /** 下行 seq 游标：已处理的最大 seq */
  private lastSeq = 0;
  /** 上一次 hello 的 seq，用于跳变（游标回退）检测 */
  private lastHelloSeq: number | null = null;
  private retry = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = true;
  private status: WsStatus = "idle";
  private cidCounter = 0;
  private pendingAcks = new Map<string, (ack: AckPayload) => void>();

  /** 建立连接；断开后自动重连（带 since 游标），除非先调 close() */
  connect(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.retry = 0;
    this.open();
  }

  /** 主动关闭；之后不再自动重连，未决 ack 全部拒绝 */
  close(): void {
    this.stopped = true;
    this.clearTimer();
    this.rejectAllPending("ws closed by client");
    this.ws?.close();
    this.ws = null;
    this.setStatus("closed");
  }

  /** 当前下行 seq 游标（重连 since / 调试用） */
  get seq(): number {
    return this.lastSeq;
  }

  /** 注册下行事件处理器（按 name 判别，payload typed），返回取消函数。收到时已按 seq 去重升序 */
  on<K extends ServerEventName>(event: K, handler: (payload: EventMap[K], meta: EventMeta) => void): () => void {
    let set = this.handlers.get(event);
    if (!set) {
      set = new Set();
      this.handlers.set(event, set);
    }
    set.add(handler as EventHandler);
    return () => {
      set!.delete(handler as EventHandler);
    };
  }

  /** 订阅全事件透传（已过 seq 去重升序），live 总分发用 */
  onAny(handler: (env: WsEnvelope) => void): () => void {
    this.anyListeners.add(handler);
    return () => {
      this.anyListeners.delete(handler);
    };
  }

  /** 订阅连接状态变化（UI 状态栏用）；注册即回调当前值 */
  onStatus(listener: (s: WsStatus) => void): () => void {
    this.statusListeners.add(listener);
    listener(this.status);
    return () => {
      this.statusListeners.delete(listener);
    };
  }

  /** 订阅「需全量拉取刷新」信号（服务端 resync / hello.seq 游标回退时触发） */
  onResyncNeeded(listener: (reason: string) => void): () => void {
    this.resyncListeners.add(listener);
    return () => {
      this.resyncListeners.delete(listener);
    };
  }

  /**
   * 发送 client_cmd 并等待对应 ack（按 cid 匹配）；未连接或超时（10s）reject。
   * @param sid 业务命令可带会话 id 写入信封顶层，缺省 null
   */
  request<K extends ClientCmdName>(name: K, payload: ClientCmdMap[K], sid?: string): Promise<AckPayload> {
    const cid = `c${++this.cidCounter}`;
    const env: WsEnvelope = {
      v: 1,
      type: "client_cmd",
      name,
      seq: 0,
      ts: Date.now(),
      sid: sid ?? null,
      payload,
      // M2b 实装：服务端读 msg.args 透传 Qt，payload 同值保留兼容
      args: payload,
      cid,
    };
    if (this.ws?.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error(`ws not open (status=${this.status})`));
    }
    return new Promise<AckPayload>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingAcks.delete(cid);
        reject(new Error(`ws cmd "${name}" ack timeout (${ACK_TIMEOUT_MS}ms)`));
      }, ACK_TIMEOUT_MS);
      this.pendingAcks.set(cid, (ack) => {
        clearTimeout(timer);
        resolve(ack);
      });
      this.ws!.send(JSON.stringify(env));
    });
  }

  private open(): void {
    this.setStatus(this.retry === 0 ? "connecting" : "reconnecting");
    const port = resolveServerPort();
    const token = resolveServerToken();
    const tokenQs = token ? `&token=${encodeURIComponent(token)}` : "";
    const ws = new WebSocket(`ws://127.0.0.1:${port}/ws?since=${this.lastSeq}${tokenQs}`);
    this.ws = ws;

    ws.onopen = () => {
      this.retry = 0;
      this.setStatus("open");
    };

    ws.onmessage = (ev) => {
      // 单条 JSON 信封；解析失败丢弃，避免毒消息打断分发
      try {
        this.handleEnvelope(JSON.parse(ev.data as string) as WsEnvelope);
      } catch {
        // TODO: 接协议错误上报通道
      }
    };

    ws.onclose = (ev) => {
      this.ws = null;
      this.rejectAllPending("ws connection lost");
      if (this.stopped) return;
      // M2b 鉴权拒绝（close 4401）：重连无意义，停机交由上层提示
      if (ev.code === 4401) {
        this.stopped = true;
        this.setStatus("closed");
        this.fireResync("auth-rejected-4401");
        return;
      }
      this.scheduleReconnect();
    };

    ws.onerror = () => {
      // onclose 会随后触发，重连逻辑统一在 onclose 处理
    };
  }

  private handleEnvelope(env: WsEnvelope): void {
    if (env.v !== 1) return;
    switch (env.type) {
      case "event":
        this.dispatchEvent(env);
        break;
      case "ack": {
        // cid 兼容信封顶层与 payload 两种带回位置
        const cid = env.cid ?? (env.payload as AckPayload | undefined)?.cid;
        if (cid) {
          const pending = this.pendingAcks.get(cid);
          if (pending) {
            this.pendingAcks.delete(cid);
            pending(env.payload as AckPayload);
          }
        }
        break;
      }
      case "error":
        // 信封级错误按 "error" 事件分发（与 name="error" 的事件共用通道）
        this.dispatchEvent({ ...env, name: "error" });
        break;
      case "client_cmd":
        // 下行不应出现 client_cmd，忽略
        break;
    }
  }

  private dispatchEvent(env: WsEnvelope): void {
    // hello 特判：信封 seq 游标回退（服务端重启，M2b 后 hello payload 不带 seq）→ 要求全量刷新；不 return，继续走通用分发
    if (env.name === "hello") {
      if (this.lastHelloSeq !== null && env.seq < this.lastHelloSeq) {
        this.fireResync("hello-seq-regressed");
      }
      this.lastHelloSeq = env.seq;
    }
    if (env.name === "resync") {
      this.fireResync((env.payload as ResyncPayload)?.reason ?? "server-resync");
    }

    // seq 去重升序：<= 游标视为重复/乱序旧包丢弃（seq<=0 视为无游标事件）
    if (typeof env.seq === "number" && env.seq > 0) {
      if (env.seq <= this.lastSeq) return;
      this.lastSeq = env.seq;
    }

    const meta: EventMeta = { seq: env.seq, ts: env.ts, sid: env.sid };
    const set = this.handlers.get(env.name);
    if (set) for (const h of set) (h as (p: unknown, m: EventMeta) => void)(env.payload, meta);
    for (const l of this.anyListeners) l(env);
  }

  private fireResync(reason: string): void {
    for (const l of this.resyncListeners) l(reason);
  }

  private rejectAllPending(reason: string): void {
    for (const resolve of this.pendingAcks.values()) resolve({ ok: false, error: reason });
    this.pendingAcks.clear();
  }

  private scheduleReconnect(): void {
    const delay = Math.min(RETRY_BASE_MS * 2 ** this.retry, RETRY_MAX_MS);
    this.retry += 1;
    this.setStatus("reconnecting");
    this.timer = setTimeout(() => {
      if (!this.stopped) this.open();
    }, delay);
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  private setStatus(s: WsStatus): void {
    this.status = s;
    for (const l of this.statusListeners) l(s);
  }
}

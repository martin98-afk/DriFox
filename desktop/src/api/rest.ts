/**
 * REST typed client：对齐 DriFox 网关服务契约（http://127.0.0.1:{port}/api）。
 *
 * - 端口复用 ws.ts 的 resolveServerPort()（window.__DRIFOX_SERVER__.port，fallback 8765）
 * - 全端点 typed；响应按裸 JSON 建模（服务端未定义统一包络）
 * - 非 2xx 抛 RestError（携带 status 与 body）
 */

import { resolveServerPort } from "./ws";
import type { ContextUpdatedPayload, MessageInfo, SessionInfo } from "./ws";

// ---------- 错误 ----------

export class RestError extends Error {
  constructor(
    public status: number,
    public body: string,
  ) {
    super(`REST ${status}: ${body}`);
    this.name = "RestError";
  }
}

// ---------- 领域类型（复用/补全 ws.ts 单源类型） ----------

export type Session = SessionInfo;
export type Message = MessageInfo;

export interface HealthInfo {
  status?: string;
  version?: string;
  [key: string]: unknown;
}

export interface ProjectInfo {
  name: string;
  [key: string]: unknown;
}

export interface SendResult {
  /** 服务端可能回执消息 id */
  message_id?: string;
  [key: string]: unknown;
}

/** T13 b 表定稿：decision 统一 approve/deny（废弃 allow 拼写）；auto_allow 等扩展字段后置 */
export type PermissionDecision = "approve" | "deny";

export interface ModelConfig {
  provider?: string;
  model?: string;
  [key: string]: unknown;
}

export interface AgentInfo {
  name: string;
  description?: string;
  [key: string]: unknown;
}

export interface PluginInfo {
  name: string;
  enabled?: boolean;
  version?: string;
  [key: string]: unknown;
}

export type ContextUsage = ContextUpdatedPayload;

export interface InputHistoryItem {
  content?: string;
  ts?: number;
  [key: string]: unknown;
}

export interface ListSessionsOpts {
  project?: string;
  limit?: number;
  offset?: number;
}

export interface CreateSessionPayload {
  title?: string;
  project?: string;
  agent?: string;
}

export interface UpdateSessionPayload {
  title?: string;
  /** M2b SessionPatchReq 仅 title/agent，project 传了会被服务端忽略 */
  agent?: string;
}

/** M2b 实装：会话列表返回包络（非裸数组） */
export interface ListSessionsResp {
  items: Session[];
  total: number;
}

/** M2b PermissionDecision body */
export interface PermissionDecideBody {
  decision: PermissionDecision;
  /** tool | round | session（作用域映射） */
  auto_allow?: string;
  session_allow?: boolean;
  reason?: string;
}

export interface SendMessagePayload {
  content: string;
  attachments?: unknown[];
}

// ---------- fetch 基座 ----------

function qs(params?: object): string {
  if (!params) return "";
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined) usp.set(k, String(v));
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const port = resolveServerPort();
  const res = await fetch(`http://127.0.0.1:${port}${path}`, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    throw new RestError(res.status, await res.text().catch(() => ""));
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ---------- 全端点 typed client ----------

export const restApi = {
  // 健康
  health: () => request<HealthInfo>("GET", "/api/health"),

  // 会话 CRUD
  /** M2b：服务端返回 {items,total} 包络，此处解包为 Session[]（sessionStore 兼容不变） */
  listSessions: async (opts?: ListSessionsOpts) => {
    const resp = await request<ListSessionsResp>("GET", `/api/sessions${qs(opts)}`);
    return resp.items ?? [];
  },
  createSession: (payload?: CreateSessionPayload) =>
    request<Session>("POST", "/api/sessions", payload ?? {}),
  getSession: (id: string) => request<Session>("GET", `/api/sessions/${encodeURIComponent(id)}`),
  updateSession: (id: string, patch: UpdateSessionPayload) =>
    request<Session>("PATCH", `/api/sessions/${encodeURIComponent(id)}`, patch),
  deleteSession: (id: string) =>
    request<void>("DELETE", `/api/sessions/${encodeURIComponent(id)}`),

  // 项目
  listProjects: () => request<ProjectInfo[]>("GET", "/api/projects"),

  // 会话消息
  sendMessage: (id: string, payload: SendMessagePayload) =>
    request<SendResult>("POST", `/api/sessions/${encodeURIComponent(id)}/send`, payload),
  stopSession: (id: string) =>
    request<void>("POST", `/api/sessions/${encodeURIComponent(id)}/stop`),

  // 权限 / 提问
  /** M2b body：{decision, auto_allow?, session_allow?, reason?} */
  decidePermission: (toolCallId: string, body: PermissionDecideBody) =>
    request<void>("POST", `/api/permissions/${encodeURIComponent(toolCallId)}`, body),
  /** M2b body：{id, answers}（id 与 path 参数双保险，服务端取 or） */
  answerQuestion: (id: string, answers: string[]) =>
    request<void>("POST", `/api/questions/${encodeURIComponent(id)}/answer`, { id, answers }),

  // 模型配置
  getModelConfig: () => request<ModelConfig>("GET", "/api/config/model"),
  putModelConfig: (cfg: ModelConfig) => request<ModelConfig>("PUT", "/api/config/model", cfg),

  // agents
  listAgents: () => request<AgentInfo[]>("GET", "/api/agents"),

  // 插件
  listPlugins: () => request<PluginInfo[]>("GET", "/api/plugins"),
  enablePlugin: (name: string) =>
    request<void>("POST", `/api/plugins/${encodeURIComponent(name)}/enable`),
  disablePlugin: (name: string) =>
    request<void>("POST", `/api/plugins/${encodeURIComponent(name)}/disable`),

  // 上下文用量
  getContextUsage: () => request<ContextUsage>("GET", "/api/context/usage"),

  // 输入历史
  getInputHistory: () => request<InputHistoryItem[]>("GET", "/api/input-history"),
};

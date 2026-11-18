/**
 * mockDriver（d 表联调开关骨架）：VITE_API_MODE=mock 时驱动「发送→骨架→流式→定型」闭环。
 * fixtures 与 T13 b 表 payload 定稿同构（防 mock 通过 live 挂）：
 * - stream.started{} 空 payload；session 域 payload 不重复 session_id（信封 sid 承载）
 * - tool.started{tool_call_id,tool_name,arguments} / tool.args_delta{tool_call_id,delta} / tool.result{tool_call_id,tool_name,result}
 * - stream.finished{final_text}；context.updated{token_count,limit}
 * 回放节奏：seq 间隔 30ms；流中段插入 250ms 静默演示升级；事件统一走 streamStore.applyEvent 收口。
 * F3 扩展：3 项目×2-3 会话树 / 按需历史种子 / 新建会话 / 会话切换清流式态。
 */

import { useSessionStore } from "../../stores/sessionStore";
import { useStreamStore } from "../../stores/streamStore";
import { usePluginStore } from "../../stores/pluginStore";
import { useChatLog } from "./chatLog";
import { fingerprint } from "./fingerprint";
import { wireFloatingEvent } from "../floating/bridge";
import { useFloatingStore } from "../floating/floatingStore";

const MODE = String(import.meta.env.VITE_API_MODE ?? "mock");

export function isMockMode(): boolean {
  return MODE === "mock";
}

let seqCounter = 0;
let sessionCounter = 0;

function env(name: string, payload: unknown, sid: string | null) {
  return { v: 1 as const, type: "event" as const, name, seq: ++seqCounter, ts: Date.now(), sid, payload };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function currentSid(): string {
  return useSessionStore.getState().currentSid ?? "";
}

/** 事件入流式收口 + 浮动卡桥接双路（permission/question 数据进 store，卡片壳进 floatingStore） */
function emit(name: string, payload: unknown, sid: string | null): void {
  wireFloatingEvent(name, payload);
  useStreamStore.getState().applyEvent(env(name, payload, sid));
}

// ---------- 会话树 mock 数据（3 项目 × 2-3 会话） ----------

interface MockProject {
  name: string;
  sessions: string[];
}

const MOCK_PROJECTS: MockProject[] = [
  { name: "DriFox", sessions: ["Tauri 复刻 DriFox 应用", "桌面端主题体系", "性能基准对照"] },
  { name: "助手中心", sessions: ["人格创建向导", "技能市场盘点"] },
  { name: "网关", sessions: ["钉钉适配器联调", "飞书通道压测", "消息队列优化"] },
];

const LAST_SID_KEY = "drifox.lastSid";

/** 会话树初始化：3 项目×2-3 会话入 sessionStore，当前会话从 localStorage 恢复（选中态持久化） */
export function startMock(): void {
  usePluginStore.getState().loadMockPlugins(); // F5：设置页 fixture
  for (const proj of MOCK_PROJECTS) {
    for (const title of proj.sessions) {
      sessionCounter += 1;
      useSessionStore.getState().upsert({
        id: `mock-s${sessionCounter}`,
        title,
        project: proj.name,
        agent: "frontend-developer",
      });
    }
  }
  const order = useSessionStore.getState().order;
  const saved = localStorage.getItem(LAST_SID_KEY);
  const initial = saved && order.includes(saved) ? saved : order[0];
  useSessionStore.getState().setCurrent(initial ?? null);
}

// ---------- 历史种子（会话切换真实生效：载入对应 mock 消息集） ----------

const SEED_REPLIES = [
  "会话历史验证。这条消息来自 mock 种子数据，验证定型卡渲染。\n\n- 列表项一\n- 列表项二\n\n`inline code` 与 **加粗** 均正常。",
  "第二段历史。包含一个闭合代码块：\n\n```json\n{\"ok\": true, \"n\": 42}\n```\n\n表格：\n\n| 键 | 值 |\n|---|---|\n| mode | mock |\n| seed | on |",
];

/** 为指定会话生成定型历史消息集（chatLog 已有则跳过） */
export function seedHistory(sid: string): void {
  const log = useChatLog.getState().messagesBySid[sid];
  if (log && log.length > 0) return;
  const s = useSessionStore.getState().sessions[sid];
  const base = Date.now() - 3600_000;
  useChatLog.getState().appendMessage(sid, {
    id: fingerprint(sid, "user", "seed1"),
    role: "user",
    text: `看看「${s?.title ?? sid}」的进展。`,
    ts: base,
  });
  useChatLog.getState().appendMessage(sid, {
    id: fingerprint(sid, "assistant", "seed1"),
    role: "assistant",
    blocks: [
      { type: "reasoning", content: "用户要进展汇报。整理当前状态给出一句话结论与要点。" },
      { type: "text", content: SEED_REPLIES[0] },
    ],
    model: "glm-5.3-flash",
    elapsedS: 12,
    tokPerS: 48,
    ts: base + 30_000,
  });
  useChatLog.getState().appendMessage(sid, {
    id: fingerprint(sid, "user", "seed2"),
    role: "user",
    text: "再给个数据示例。",
    ts: base + 60_000,
  });
  useChatLog.getState().appendMessage(sid, {
    id: fingerprint(sid, "assistant", "seed2"),
    role: "assistant",
    blocks: [{ type: "text", content: SEED_REPLIES[1] }],
    model: "glm-5.3-flash",
    elapsedS: 8,
    tokPerS: 61,
    ts: base + 90_000,
  });
}

/** 会话树切换：清流式状态（新旧都清）+ 按需载入历史集 + 持久化选中态 */
export function switchMockSession(nextSid: string): void {
  const prev = useSessionStore.getState().currentSid;
  if (prev && prev !== nextSid) {
    useStreamStore.getState().clearSession(prev);
  }
  useStreamStore.getState().clearSession(nextSid);
  useSessionStore.getState().setCurrent(nextSid);
  localStorage.setItem(LAST_SID_KEY, nextSid);
  seedHistory(nextSid);
}

/** 新建对话：空会话（无消息）→ 主区欢迎卡；派发聚焦事件给 InputArea */
export function createMockSession(): string {
  sessionCounter += 1;
  const id = `mock-s${sessionCounter}`;
  useSessionStore.getState().upsert({ id, title: "新对话", project: "DriFox", agent: "frontend-developer" });
  switchMockSession(id);
  window.dispatchEvent(new CustomEvent("drifox:focus-input"));
  return id;
}

/** 会话树删除入口：乐观移除 + undo 快照（60s TTL 到期真删除生效，撤销则回滚） */
export function deleteMockSessionWithUndo(sid: string): void {
  const s = useSessionStore.getState().sessions[sid];
  if (!s) return;
  useFloatingStore.getState().setUndo({
    sid,
    snapshot: { id: s.id, title: s.title, project: s.project, agent: s.agent },
  });
  useSessionStore.getState().removeLocal(sid);
}

/** 会话树重命名（右键菜单，mock 本地） */
export function renameMockSession(sid: string, title: string): void {
  const s = useSessionStore.getState().sessions[sid];
  if (!s) return;
  useSessionStore.getState().upsert({ ...s, title });
}

// ---------- 典型对话流回放 ----------

const REPLY_1 = `已完成扫描。DriFox 桌面端复刻进度如下：

| 模块 | 状态 | 完成度 |
|---|---|---|
| API 层 | 已交付 | 100% |
| Chat 管线 | 本轮 | 90% |
| 外壳体系 | 本轮 | 80% |

代码示例：

\`\`\`python
def greet(name: str) -> str:
    return f"hello, {name}"
\`\`\`

输入框支持 \`↑/↓\` 历史导航与 **Shift+Enter** 换行。`;

const REPLY_2 = `公式渲染验证。行内公式 $E = mc^2$，展示公式：

$$\\int_0^1 x^2 \\, dx = \\frac{1}{3}$$

流程未闭合 fence 时会渲染等高骨架占位，闭合后复原。`;

function chunks(text: string, size = 4): string[] {
  const out: string[] = [];
  for (let i = 0; i < text.length; i += size) out.push(text.slice(i, i + size));
  return out;
}

const REPLY_3 = `现在演示浮动卡交互链路：重试倒计时 → Toast 三级 → 排队消息 → 三档权限卡（注意高危档防误触）→ 提问卡。按卡片提示操作即可。`;

/** 条件等待（人工裁决/关闭后推进；超时兜底推进，卡保留给人工） */
async function waitFor(cond: () => boolean, timeoutMs: number): Promise<void> {
  const t0 = Date.now();
  while (!cond()) {
    if (Date.now() - t0 > timeoutMs) return;
    await sleep(200);
  }
}

/** 浮动卡全链演示（F4 fixture）：retry→resolved / toast 三级 / queue / 三档 permission / question 两题 */
async function demoFloatingFlow(sid: string): Promise<void> {
  // 1) retry.status → 3s 后 resolved（RetryChip 出现与撤除）
  emit("retry.status", { error_type: "rate_limit", attempt: 1, max_retries: 3, wait_time: 3 }, sid);
  await sleep(3300);
  emit("retry.resolved", {}, sid);
  await sleep(400);

  // 2) toast 三级（toast.show 全局 sid=null）
  emit("toast.show", { title: "提示", message: "这是一条 info 级通知", level: "info" }, null);
  await sleep(400);
  emit("toast.show", { title: "警告", message: "上下文用量接近阈值", level: "warn" }, null);
  await sleep(400);
  emit("toast.show", { title: "失败", message: "示例 error 级通知", level: "error" }, null);
  await sleep(600);

  // 3) 排队消息（interject.recovered + queue.updated）
  emit("interject.recovered", { count: 2 }, sid);
  emit(
    "queue.updated",
    { items: [{ msg_id: "q1", summary: "补一条：把性能基准跑一遍" }, { msg_id: "q2", summary: "顺便更新 CHANGELOG" }] },
    sid,
  );
  await sleep(800);

  // 4) 三档 permission 依次（等裁决后推进；超时 30s 兜底）
  const permGone = (id: string) => () => {
    const s = useStreamStore.getState().streams[sid];
    return !s?.permissions.some((p) => p.toolCallId === id);
  };
  emit(
    "permission.requested",
    { tool_call_id: "perm_warn", tool_name: "read_file", arguments: { path: "secrets.env" }, risk_level: "warn", preview_payload: { lines: 12 } },
    sid,
  );
  await waitFor(permGone("perm_warn"), 30_000);
  await sleep(500);
  emit(
    "permission.requested",
    { tool_call_id: "perm_info", tool_name: "web_search", arguments: { query: "tauri 2 release" }, risk_level: "info" },
    sid,
  );
  await waitFor(permGone("perm_info"), 30_000);
  await sleep(500);
  emit(
    "permission.requested",
    { tool_call_id: "perm_danger", tool_name: "delete_file", arguments: { path: "logs/" }, risk_level: "danger", preview_payload: { files: 137 } },
    sid,
  );
  await waitFor(permGone("perm_danger"), 30_000);
  await sleep(600);

  // 5) question 两题（单选 + 多选，分页导航）
  emit(
    "question.asked",
    {
      id: "qz_demo_1",
      questions: [
        { label: "优先推进哪个模块？", description: "单选", options: ["主题体系", "联调波次", "打包发布"], multiple: false },
        { label: "关注哪些质量项？", description: "多选", options: ["可访问性", "性能", "内存占用"], multiple: true },
      ],
      extra: null,
    },
    sid,
  );
  await waitFor(() => {
    const s = useStreamStore.getState().streams[sid];
    return !s?.questions.some((x) => x.id === "qz_demo_1");
  }, 60_000);

  // 6) 收尾 toast
  emit("toast.show", { title: "演示完成", message: "浮动卡链路走完", level: "info" }, null);
}

async function streamDeltas(sid: string, text: string, silenceMid = false): Promise<void> {
  const parts = chunks(text);
  const mid = silenceMid ? Math.floor(parts.length / 2) : parts.length;
  for (let i = 0; i < parts.length; i++) {
    if (i === mid) await sleep(300); // 250ms 静默：演示升级定型
    emit("stream.content_delta", { text: parts[i] }, sid);
    await sleep(30); // d 表：seq 间隔 30ms
  }
}

/** 用户发送 → 回放典型对话流（首轮带工具卡演示，次轮带 reasoning + 公式） */
export async function mockSend(text: string): Promise<void> {
  const sid = currentSid();
  if (!sid) return;
  const t0 = Date.now();
  useChatLog.getState().appendMessage(sid, {
    id: fingerprint(sid, "user", text, Date.now()),
    role: "user",
    text,
    ts: Date.now(),
  });

  await sleep(350);
  emit("session.created", { id: sid, title: useSessionStore.getState().sessions[sid]?.title ?? "" }, sid);
  emit("stream.started", {}, sid);

  const logLen = (useChatLog.getState().messagesBySid[sid] ?? []).length;

  if (logLen <= 2) {
    // 第一轮：工具卡演示（tool.* 独立通道，不进 markdown）
    const callId = `call_${fingerprint(sid, "tool")}`;
    emit("tool.started", { tool_call_id: callId, tool_name: "read_file", arguments: { path: "desktop/src/App.tsx" } }, sid);
    await sleep(150);
    for (const d of ['{"pa', 'th": "desktop/src/A', 'pp.tsx"}']) {
      emit("tool.args_delta", { tool_call_id: callId, delta: d }, sid);
      await sleep(30);
    }
    await sleep(200);
    emit("tool.result", { tool_call_id: callId, tool_name: "read_file", result: "OK 42 lines" }, sid);
    await streamDeltas(sid, REPLY_1, true);
  } else if (logLen === 5) {
    // 第三轮：短流式 + 浮动卡全链演示（F4）
    await streamDeltas(sid, REPLY_3, true);
    await demoFloatingFlow(sid);
  } else {
    // 其余轮：reasoning + 公式
    for (const p of chunks("用户想验证公式渲染与思考卡流式。准备展示行内与展示公式各一条。", 6)) {
      emit("stream.reasoning_delta", { text: p }, sid);
      await sleep(30);
    }
    await streamDeltas(sid, REPLY_2, true);
  }

  // context.updated + finished（final_text 与累计 content 一致）
  const s = useStreamStore.getState().streams[sid];
  const finalText = (s?.content ?? "") + (s?.pendingContent ?? "") || text;
  emit("context.updated", { token_count: 1024 + text.length, limit: 8192 }, sid);
  emit("stream.finished", { final_text: finalText }, sid);

  // meta 补齐（定型卡由 useStreamingMessage finalize 写入后 replaceLast 覆盖 meta）
  await sleep(50);
  const log = useChatLog.getState().messagesBySid[sid];
  const last = log[log.length - 1];
  if (last && last.role === "assistant") {
    const elapsedS = Math.max(1, Math.round((Date.now() - t0) / 100) / 10);
    const chars = finalText.length;
    useChatLog.getState().replaceLast(sid, {
      ...last,
      model: "glm-5.3-flash",
      elapsedS,
      tokPerS: Math.round(chars / 1.6 / Math.max(elapsedS, 1)),
    });
  }
}

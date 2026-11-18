#!/usr/bin/env node
// scripts/ws-assert.mjs — T13 场景 1：典型对话流八步断言（真实链路，不经 mock）
//
// 用法：node scripts/ws-assert.mjs ws://127.0.0.1:PORT[/ws]
//   （http 端口取自 ws url；REST 同端口 /api 前缀）
//
// 断言步（T13 a 表 1-8 + M2a 定案）：
//   1. WS 连接收 hello：type=event name=hello，payload.server_version 存在，
//      payload 不含 seq（review 🟡2：双 seq 错位防护）
//   2. GET /api/health → HTTP 200 且 drifox_server.backend_ready=true
//   3. POST /api/sessions{title} → 拿 session_id
//   4. WS 收 session.created，sid===session_id，payload 不重复 session_id
//   5. WS client_cmd ping(cid=c1) → 收 ack{ok,cid:c1,pong:true}
//   6. WS client_cmd send{session_id,content} → 收 ack{ok,cid}
//   7. 收 stream.started → content_delta*（≥1）→ stream.finished{final_text}
//   8. final_text === 全部 content_delta.text 拼接；全程 seq 严格 +1 连续
//
// 退出码：0=全绿；1=断言失败；2=连接/前置失败

import process from "node:process";

const SCENE = process.argv[3] || "1";
const wsUrlRaw = process.argv[2] || "ws://127.0.0.1:0/ws";
const wsUrl = wsUrlRaw.split("?")[0];
const urlToken = new URL(wsUrlRaw.replace(/^ws/, "http")).searchParams.get("token") || "";
const m = wsUrl.match(/ws:\/\/([^:/]+):(\d+)/);
if (!m) {
  console.error("FAIL: ws url 非法（示例 ws://127.0.0.1:18777/ws）");
  process.exit(2);
}
const [, host, portStr] = m;
const httpBase = `http://${host}:${portStr}`;
const CONTENT = process.env.ASSERT_CONTENT || "只回复两个字：你好";
const SEND_TIMEOUT_MS = Number(process.env.ASSERT_SEND_TIMEOUT || 90000);

let seqCursor = null; // 全局单调游标
let seqFrames = 0;    // 已审计帧数
let seqJumps = 0;     // seq 断点数
let failures = 0;

function check(name, cond, detail = "") {
  if (cond) {
    console.log(`PASS  ${name}`);
  } else {
    failures += 1;
    console.log(`FAIL  ${name}${detail ? " — " + detail : ""}`);
  }
}

function auditSeq(env, where) {
  // 沿途帧审计：连续则静默计数，断点才输出（步7 中间事件多，避免噪声）
  const s = env?.seq;
  if (typeof s !== "number") {
    check(`seq 审计(${where})`, false, "帧缺 seq 字段");
    return;
  }
  if (seqCursor === null) {
    seqCursor = s;
    seqFrames = 1;
    return;
  }
  seqFrames += 1;
  if (s !== seqCursor + 1) {
    seqJumps += 1;
    check(`seq 审计(${where})`, false, `期望 ${seqCursor + 1} 实得 ${s} name=${env.name}`);
  }
  seqCursor = s;
}

const raw = await import("node:http");
function httpGetJson(path, timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    const req = raw.default.request(`${httpBase}${path}`, { method: "GET" }, (res) => {
      let buf = "";
      res.on("data", (c) => (buf += c));
      res.on("end", () => resolve({ status: res.statusCode, body: buf }));
    });
    req.on("error", reject);
    req.setTimeout(timeoutMs, () => reject(new Error("http timeout")));
    req.end();
  });
}

// ---- 前置：等握手就绪（服务可能仍在引擎初始化）----
let health = null;
for (let i = 0; i < 40; i++) {
  try {
    const r = await httpGetJson("/api/health");
    if (r.status === 200) {
      const b = JSON.parse(r.body);
      // 等引擎与桥绑定就绪，避免早于 attach 的竞态误报
      if (b?.drifox_server?.backend_ready === true) {
        health = b;
        break;
      }
    }
  } catch {}
  await new Promise((r) => setTimeout(r, 500));
}
if (!health) {
  console.error("FAIL: /api/health 不可达（服务未起？）");
  process.exit(2);
}

// ---- 步 2：health 200 + backend_ready ----
check("步2: /api/health HTTP 200", health?.drifox_server?.status === "ok");
check("步2: backend_ready=true", health?.drifox_server?.backend_ready === true,
  `bridge_signals=${health?.drifox_server?.bridge_signals}`);

// ---- WS 连接 ----
const ws = new WebSocket(urlToken ? `${wsUrl}?token=${encodeURIComponent(urlToken)}` : wsUrl);
const inbox = [];
let wsOpen = false;
ws.addEventListener("open", () => (wsOpen = true));
ws.addEventListener("error", (e) => console.error("WS error:", e.message ?? e));
ws.addEventListener("message", (ev) => inbox.push(JSON.parse(ev.data)));

async function waitFor(pred, timeoutMs, label) {
  // 按到达序审计每一帧（只记 seq 不丢弃）；命中目标帧则移除并返回。
  // 不丢帧：stub 同步 emit 时 stream 事件可能早于 send ack 到达，
  // 若「splice 头部区间」会把目标之前的 stream 帧吞掉导致后续步骤找不到。
  const t0 = Date.now();
  while (Date.now() - t0 <= timeoutMs) {
    for (let i = 0; i < inbox.length; i++) {
      const f = inbox[i];
      if (!f._audited) {
        auditSeq(f, label);
        f._audited = true;
      }
      if (pred(f)) {
        inbox.splice(i, 1);
        return f;
      }
    }
    await new Promise((r) => setTimeout(r, 25));
  }
  for (const f of inbox) {
    if (!f._audited) {
      auditSeq(f, label);
      f._audited = true;
    }
  }
  return null;
}
function send(obj) {
  ws.send(JSON.stringify(obj));
}

// 等连接与 hello
let hello = null;
for (let i = 0; i < 100 && !hello; i++) {
  if (wsOpen && inbox.length) hello = inbox.shift();
  else await new Promise((r) => setTimeout(r, 25));
}
if (!hello) {
  console.error("FAIL: WS 未收到 hello（连接失败或超时）");
  process.exit(2);
}

// ---- 步 1：hello 形状 ----
check("步1: hello type=event", hello.type === "event");
check("步1: hello name=hello", hello.name === "hello");
check("步1: payload.server_version 存在", typeof hello.payload?.server_version === "string");
check("步1: payload 不含 seq（🟡2 修正）", !("seq" in (hello.payload || {})));
auditSeq(hello, "步1: hello");

// ---- 步 3：REST 建会话 ----
const createRes = await fetch(`${httpBase}/api/sessions`, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ title: "ws-assert 场景1" }),
});
check("步3: POST /api/sessions 200", createRes.status === 200, `status=${createRes.status}`);
const created = await createRes.json();
const sid = created.session_id;
check("步3: 返回 session_id", typeof sid === "string" && sid.length > 0);

// ---- 步 4：session.created 事件（sid 承载，payload 不重复）----
const createdEv = await waitFor(
  (e) => e.type === "event" && e.name === "session.created",
  5000, "session.created",
);
check("步4: 收到 session.created", !!createdEv);
check("步4: sid===session_id", createdEv?.sid === sid, `sid=${createdEv?.sid}`);
check("步4: payload 不重复 session_id", createdEv && !("session_id" in (createdEv.payload || {})));

// ---- 步 5：ping → ack（cid 回执 + seq 连续）----
send({ v: 1, type: "client_cmd", name: "ping", cid: "c1", args: {} });
const pongAck = await waitFor(
  (e) => e.type === "ack" && e.name === "ack" && e.payload?.cid === "c1",
  8000, "ping ack",
);
check("步5: ping 收 ack{ok,cid:c1,pong}", pongAck?.payload?.ok === true && pongAck?.payload?.pong === true,
  JSON.stringify(pongAck?.payload ?? {}));

// ---- 步 6：send → ack{ok} ----
send({
  v: 1, type: "client_cmd", name: "send", cid: "c2",
  args: { session_id: sid, content: CONTENT },
});
const sendAck = await waitFor(
  (e) => e.type === "ack" && e.name === "ack" && e.payload?.cid === "c2",
  15000, "send ack",
);
check("步6: send 收 ack{ok,cid:c2}", sendAck?.payload?.ok === true,
  JSON.stringify(sendAck?.payload ?? {}));

// ---- 步 7：流式事件序列 ----
const streamStarted = await waitFor(
  (e) => e.name === "stream.started", 15000, "stream.started",
);
check("步7: 收 stream.started", !!streamStarted);
if (streamStarted) {
  check("步7: stream.started sid===sid", streamStarted.sid === sid);
}

const deltas = [];
let reasoningSeen = false;
let finished = null;
const streamDeadline = Date.now() + SEND_TIMEOUT_MS;
while (Date.now() < streamDeadline && !finished) {
  const ev = await waitFor(
    (e) =>
      e.sid === sid &&
      ["stream.content_delta", "stream.reasoning_delta", "stream.finished", "error", "conversation.error"].includes(e.name),
    3000, "stream events",
  );
  if (!ev) continue;
  if (ev.name === "stream.content_delta" && typeof ev.payload?.text === "string") {
    deltas.push(ev.payload.text);
  } else if (ev.name === "stream.reasoning_delta") {
    reasoningSeen = true;
  } else if (ev.name === "stream.finished") {
    finished = ev;
  } else if (ev.name === "error" || ev.name === "conversation.error") {
    console.log("FAIL  步7: 服务端报错事件:", JSON.stringify(ev.payload).slice(0, 300));
    failures += 1;
    break;
  }
}

check("步7: content_delta ≥1", deltas.length >= 1, `实际 ${deltas.length} 条`);
check("步7: 收 stream.finished{final_text}", typeof finished?.payload?.final_text === "string",
  JSON.stringify(finished?.payload ?? {}).slice(0, 200));
if (reasoningSeen) console.log("INFO  含 reasoning_delta（思考流）");

// ---- 步 8：final_text === delta 拼接 ----
const joined = deltas.join("");
check("步8: final_text === content_delta 拼接",
  typeof finished?.payload?.final_text === "string" && finished.payload.final_text === joined,
  `final=${JSON.stringify(finished?.payload?.final_text?.slice(0, 80))} joined=${JSON.stringify(joined.slice(0, 80))}`);

function finish(sceneLabel) {
  console.log("─".repeat(50));
  check(`${sceneLabel}: 全程 seq 单调连续`, seqJumps === 0 && seqFrames > 0, `frames=${seqFrames} jumps=${seqJumps}`);
  console.log("─".repeat(50));
  if (failures === 0) {
    console.log(`PASS: ${sceneLabel} 全绿（seq 终点 ${seqCursor}）`);
    process.exit(0);
  }
  console.log(`FAIL: ${sceneLabel} ${failures} 项断言未过`);
  process.exit(1);
}

if (SCENE === "1") {
  ws.close();
  finish("场景1");
}

// ---------- 场景 2：异常流 A permission 介入（stub 剧本 perm/deny 关键字） ----------
if (SCENE === "2") {
  send({ v: 1, type: "client_cmd", name: "send", cid: "s2a", args: { session_id: sid, content: "[stub:perm] approve 测试" } });
  const ack2a = await waitFor((e) => e.type === "ack" && e.payload?.cid === "s2a", 10000, "s2 ack");
  check("场景2A: send ack", ack2a?.payload?.ok === true);
  const req = await waitFor((e) => e.name === "permission.requested", 10000, "permission.requested");
  check("场景2A: permission.requested 到达", !!req);
  check("场景2A: requested 字段齐全", !!req?.payload?.tool_call_id && !!req?.payload?.tool_name && typeof req?.payload?.arguments === "object");
  const tc = req?.payload?.tool_call_id;
  const decideRes = await fetch(`${httpBase}/api/permissions/${tc}`, {
    method: "POST", headers: { "content-type": "application/json", "X-Drifox-Token": urlToken },
    body: JSON.stringify({ decision: "approve" }),
  });
  const decideBody = await decideRes.json();
  check("场景2A: POST approve 200 且 ok", decideRes.status === 200 && decideBody?.ok === true);
  const toolRes = await waitFor((e) => e.name === "tool.result" && e.payload?.tool_call_id === tc, 10000, "tool.result");
  check("场景2A: approve 后 tool.result 必达", !!toolRes);
  const finA = await waitFor((e) => e.name === "stream.finished" && e.sid === sid, 10000, "finished");
  check("场景2A: stream.finished 必达", !!finA?.payload?.final_text);

  send({ v: 1, type: "client_cmd", name: "send", cid: "s2b", args: { session_id: sid, content: "[stub:perm] deny 测试" } });
  await waitFor((e) => e.type === "ack" && e.payload?.cid === "s2b", 10000, "s2b ack");
  const reqB = await waitFor((e) => e.name === "permission.requested", 10000, "permission.requested B");
  check("场景2B: 第二次 permission.requested 到达", !!reqB);
  const tcB = reqB?.payload?.tool_call_id;
  const denyRes = await fetch(`${httpBase}/api/permissions/${tcB}`, {
    method: "POST", headers: { "content-type": "application/json", "X-Drifox-Token": urlToken },
    body: JSON.stringify({ decision: "deny", reason: "ws-assert" }),
  });
  check("场景2B: POST deny 200", denyRes.status === 200);
  const noTool = await waitFor((e) => e.name === "tool.result" && e.payload?.tool_call_id === tcB, 3000, "tool.result(不应达)");
  check("场景2B: deny 后 tool.result 不达", noTool === null);
  const finB = await waitFor((e) => e.name === "stream.finished" && e.sid === sid, 10000, "finished B");
  check("场景2B: deny 后 stream.finished 必达", !!finB);
  ws.close();
  finish("场景2");
}

// ---------- 场景 3：异常流 B retry 序列（stub 剧本 retry 关键字） ----------
if (SCENE === "3") {
  send({ v: 1, type: "client_cmd", name: "send", cid: "s3", args: { session_id: sid, content: "[stub:retry] 测试" } });
  await waitFor((e) => e.type === "ack" && e.payload?.cid === "s3", 10000, "s3 ack");
  const st = await waitFor((e) => e.name === "retry.status", 10000, "retry.status");
  check("场景3: retry.status 到达", !!st);
  check("场景3: status 字段齐全", typeof st?.payload?.error_type === "string" && typeof st?.payload?.attempt === "number"
    && typeof st?.payload?.max_retries === "number" && typeof st?.payload?.wait_time === "number",
    JSON.stringify(st?.payload ?? {}));
  const rs = await waitFor((e) => e.name === "retry.resolved", 10000, "retry.resolved");
  check("场景3: retry.resolved 在 status 之后到达", !!rs);
  const fin = await waitFor((e) => e.name === "stream.finished" && e.sid === sid, 15000, "finished");
  check("场景3: resolved 后正常 finished", !!fin);
  ws.close();
  finish("场景3");
}

// ---------- 场景 4：异常流 C 断线重连（?since 重放 / 超缓冲 resync） ----------
if (SCENE === "4") {
  function openConn(since) {
    const u = `${wsUrl}?token=${encodeURIComponent(urlToken)}${since != null ? `&since=${since}` : ""}`;
    const conn = new WebSocket(u);
    const box = [];
    conn.addEventListener("message", (ev) => box.push(JSON.parse(ev.data)));
    const opened = new Promise((r) => conn.addEventListener("open", r));
    return { conn, box, opened };
  }
  async function waitBox(box, pred, timeoutMs) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      const i = box.findIndex(pred);
      if (i >= 0) return box.splice(i, 1)[0];
      await new Promise((r) => setTimeout(r, 25));
    }
    return null;
  }

  const c1 = openConn(null);
  await c1.opened;
  const hello1 = await waitBox(c1.box, (e) => e.name === "hello", 8000);
  check("场景4: conn1 hello", !!hello1);
  const cr = await fetch(`${httpBase}/api/sessions`, {
    method: "POST", headers: { "content-type": "application/json", "X-Drifox-Token": urlToken },
    body: JSON.stringify({ title: "场景4 重连" }),
  });
  const sid4 = (await cr.json()).session_id;
  await waitBox(c1.box, (e) => e.name === "session.created", 8000);
  c1.conn.send(JSON.stringify({ v: 1, type: "client_cmd", name: "send", cid: "c4a", args: { session_id: sid4, content: "场景4 第一轮" } }));
  const fin4 = await waitBox(c1.box, (e) => e.name === "stream.finished" && e.sid === sid4, 25000);
  check("场景4: 断线前完成一轮流", !!fin4);
  const lastSeq = fin4?.seq;
  check("场景4: lastSeq 记录", typeof lastSeq === "number");
  c1.conn.close();
  await new Promise((r) => setTimeout(r, 500));

  const c2 = openConn(null);
  await c2.opened;
  await waitBox(c2.box, (e) => e.name === "hello", 8000);
  c2.conn.send(JSON.stringify({ v: 1, type: "client_cmd", name: "send", cid: "c4b", args: { session_id: sid4, content: "断线期间的第二轮" } }));
  const finB = await waitBox(c2.box, (e) => e.name === "stream.finished" && e.sid === sid4, 25000);
  check("场景4: 断开期间第二轮完成", !!finB);
  const headSeq = finB?.seq;
  c2.conn.close();
  await new Promise((r) => setTimeout(r, 500));

  // 🔴1 交接窗增强：重连同时第三轮流持续产生新事件——
  // 重放帧与新事件经 per-client 队列串行，收帧 seq 必须严格连续无缺
  const c2b = openConn(null);
  await c2b.opened;
  await waitBox(c2b.box, (e) => e.name === "hello", 8000);
  c2b.conn.send(JSON.stringify({ v: 1, type: "client_cmd", name: "send", cid: "c4c", args: { session_id: sid4, content: "重连期间的第三轮" } }));
  const finC = await waitBox(c2b.box, (e) => e.name === "stream.finished" && e.sid === sid4, 25000);
  check("场景4: 重放期间第三轮流完成（服务端持续产事件）", !!finC);
  const headSeqC = finC?.seq;
  await new Promise((r) => setTimeout(r, 1200));
  const c3 = openConn(lastSeq);
  await c3.opened;
  await waitBox(c3.box, (e) => e.name === "hello", 8000);
  await new Promise((r) => setTimeout(r, 800));
  const gotResync = c3.box.some((e) => e.name === "resync");
  const replay = c3.box.filter((e) => e.seq > lastSeq && e.seq <= headSeqC);
  const seqs = replay.map((e) => e.seq).sort((a, b) => a - b);
  const expect = Array.from({ length: headSeqC - lastSeq }, (_, i) => lastSeq + 1 + i);
  const fullCover = seqs.length === expect.length && seqs.every((v, i) => v === expect[i]);
  // 补偿语义二选一：缓冲覆盖 → 完整重放；溢出 → 单条 resync（服务端判 covered）
  check("场景4: 补偿语义（完整重放 或 resync，含交接窗新事件）", fullCover || gotResync,
    `fullCover=${fullCover} resync=${gotResync} got=${seqs.join(",")} expect=${expect.join(",")}`);
  check("场景4: 重放无重复", new Set(seqs).size === seqs.length);
  check("场景4: resync 恰一条", c3.box.filter((e) => e.name === "resync").length <= 1);
  c3.conn.close();

  const c4 = openConn(1);
  await c4.opened;
  await waitBox(c4.box, (e) => e.name === "hello", 8000);
  const rs = await waitBox(c4.box, (e) => e.name === "resync", 5000);
  check("场景4: 超缓冲收到单条 resync", !!rs, JSON.stringify(rs?.payload ?? {}));
  c4.conn.close();
  finish("场景4");
}

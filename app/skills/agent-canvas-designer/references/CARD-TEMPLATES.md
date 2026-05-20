# 节点流程图 HTML 模板

用于 Visual Companion 展示的画布节点图模板。复用 brainstorming 的 server.cjs。

## 快速上手

**服务器启动**（复用 brainstorming）：
```xml
<bg_start command="powershell -Command \&quot;$env:BRAINSTORM_DIR='C:\tmp\brainstorm'; node server.cjs\&quot;" cwd="D:/work/DriFoxx/app/skills/brainstorming/scripts" />
```

**内容写入路径**：`C:/tmp/brainstorm/content/`（自动刷新）

**读取用户选择**：`C:/tmp/brainstorm/state/events`

---

## 模板 1：水平流程图

适用于线性流程展示，节点从左到右排列。

```html
<style>
  .flow-row {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0;
    margin: 2rem 0;
    flex-wrap: wrap;
  }
  .node-box {
    display: inline-flex;
    flex-direction: column;
    align-items: center;
    padding: 0.75rem 1.25rem;
    border-radius: 8px;
    font-weight: 600;
    font-size: 0.85rem;
    min-width: 100px;
    text-align: center;
    cursor: pointer;
    border: 2px solid transparent;
  }
  .node-box:hover {
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
  }
  .node-box .node-type {
    font-size: 0.65rem;
    font-weight: 400;
    opacity: 0.7;
    margin-top: 0.15rem;
  }
  .arrow {
    display: inline-flex;
    align-items: center;
    font-size: 1.5rem;
    color: #999;
    margin: 0 0.5rem;
  }
  .branch-group {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.25rem;
    margin: 0 0.5rem;
  }
  .branch-label {
    font-size: 0.7rem;
    color: #666;
    background: #f5f5f5;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
  }
  /* 节点颜色 */
  .node-start { background: #d1fae5; color: #065f46; }
  .node-llm { background: #dbeafe; color: #1e40af; }
  .node-classifier { background: #fef3c7; color: #92400e; }
  .node-agent { background: #ede9fe; color: #5b21b6; }
  .node-reply { background: #d1fae5; color: #065f46; }
  .node-end { background: #fce4ec; color: #c62828; }
  .node-container { background: #f3e5f5; color: #6a1b9a; }
  .node-rag { background: #e0f7fa; color: #00695c; }
  .node-code { background: #e8f5e9; color: #1b5e20; }
</style>

<div class="section-title">当前流程</div>
<p>描述文字...</p>

<div class="flow-row">
  <div class="node-box node-start" data-choice="start">
    用户输入
    <span class="node-type">start</span>
  </div>
  <div class="arrow">→</div>

  <div class="node-box node-classifier" data-choice="classifier">
    意图分类
    <span class="node-type">classifier</span>
  </div>
  <div class="arrow">→</div>

  <div class="branch-group">
    <div class="branch-label">技术</div>
    <div class="node-box node-agent" data-choice="tech">
      Agent
      <span class="node-type">agent</span>
    </div>
  </div>
  <div class="branch-group">
    <div class="branch-label">订单</div>
    <div class="node-box node-llm" data-choice="order">
      LLM
      <span class="node-type">llm</span>
    </div>
  </div>
  <div class="branch-group">
    <div class="branch-label">投诉</div>
    <div class="node-box node-container" data-choice="complaint">
      人工
      <span class="node-type">container</span>
    </div>
  </div>

  <div class="arrow">→</div>
  <div class="node-box node-reply" data-choice="reply">
    回复
    <span class="node-type">reply</span>
  </div>
  <div class="arrow">→</div>
  <div class="node-box node-end" data-choice="end">
    结束
    <span class="node-type">end</span>
  </div>
</div>

<p style="margin-top:1rem;">点击节点查看配置，或回复 <strong>OK</strong> 确认</p>
```

---

## 模板 2：节点选择网格

适用于让用户选择节点类型。

```html
<style>
  .node-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.75rem;
    margin: 1rem 0;
  }
  .node-card {
    padding: 1rem;
    border-radius: 8px;
    border: 2px solid #e5e5e5;
    cursor: pointer;
    transition: all 0.15s;
    text-align: center;
  }
  .node-card:hover { border-color: #c0c0c0; transform: translateY(-1px); }
  .node-card.selected { border-color: #4f46e5; background: #f0f0ff; }
  .node-card .emoji { font-size: 1.5rem; }
  .node-card .name { font-weight: 600; margin-top: 0.25rem; }
  .node-card .desc { font-size: 0.75rem; color: #888; }
</style>

<div class="section-title">选择节点类型</div>
<p>这个位置应该用什么类型的节点？</p>

<div class="node-grid">
  <div class="node-card" data-choice="llm">
    <div class="emoji">🧠</div>
    <div class="name">LLM</div>
    <div class="desc">大语言模型处理</div>
  </div>
  <div class="node-card" data-choice="classifier">
    <div class="emoji">🔀</div>
    <div class="name">Classifier</div>
    <div class="desc">意图分类/路由</div>
  </div>
  <div class="node-card" data-choice="agent">
    <div class="emoji">🛠️</div>
    <div class="name">Agent</div>
    <div class="desc">工具调用</div>
  </div>
  <div class="node-card" data-choice="rag">
    <div class="emoji">📚</div>
    <div class="name">RAG</div>
    <div class="desc">知识检索</div>
  </div>
  <div class="node-card" data-choice="container">
    <div class="emoji">👤</div>
    <div class="name">Container</div>
    <div class="desc">人工审核</div>
  </div>
  <div class="node-card" data-choice="code">
    <div class="emoji">⚡</div>
    <div class="name">Code</div>
    <div class="desc">代码执行</div>
  </div>
</div>
```

---

## 模板 3：配置确认卡片

展示节点详细配置供用户确认。

```html
<style>
  .config-card {
    background: #fff;
    border: 2px solid #e5e5e5;
    border-radius: 8px;
    padding: 1rem 1.25rem;
    margin: 1rem 0;
    cursor: pointer;
    transition: all 0.15s;
  }
  .config-card:hover { border-color: #c0c0c0; }
  .config-card.selected { border-color: #4f46e5; background: #f0f0ff; }
  .config-card .config-title { font-weight: 600; margin-bottom: 0.5rem; }
  .config-card .config-row { font-size: 0.8rem; color: #666; padding: 0.15rem 0; }
  .config-card .config-row .key { color: #888; }
</style>

<div class="section-title">配置确认</div>
<p>以下配置是否正确？点击卡片选择。</p>

<div class="config-card" data-choice="confirm">
  <div class="config-title">✅ 确认以下配置</div>
  <div class="config-row"><span class="key">分类器 system_prompt:</span> 你是一个意图分类器...</div>
  <div class="config-row"><span class="key">类别:</span> 技术支持, 订单查询, 投诉建议</div>
  <div class="config-row"><span class="key">默认分支:</span> 其他</div>
</div>

<div class="config-card" data-choice="edit">
  <div class="config-title">✏️ 需要修改</div>
  <div class="config-row">我想修改分类器配置</div>
</div>
```

---

## 文件命名约定

| 场景 | 文件名 |
|------|--------|
| 首次流程草图 | `flow-v1.html` |
| 调整后 | `flow-v2.html` |
| 节点选择 | `node-pick.html` |
| 配置确认 | `config-confirm.html` |
| 已处理/返回终端 | `done.html` |

**不重用文件名** — 每次写入新文件确保浏览器自动刷新加载到最新内容。

---

## 清除旧画面

当需要回到终端交互时，写入清除页面：

```html
<p>继续在终端交互...</p>
```

---

## 节点颜色速查

| 节点类型 | CSS class | 颜色 |
|----------|-----------|------|
| start | `node-start` | 🟢 绿色 |
| llm | `node-llm` | 🔵 蓝色 |
| classifier | `node-classifier` | 🟡 黄色 |
| agent | `node-agent` | 🟣 紫色 |
| rag | `node-rag` | 🩵 青色 |
| container | `node-container` | 🟣 浅紫 |
| code | `node-code` | 🟢 浅绿 |
| reply | `node-reply` | 🟢 绿色 |
| end | `node-end` | 🔴 红色 |

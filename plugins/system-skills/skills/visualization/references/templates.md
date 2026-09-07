# 默认模板库（直接套用，变量已适配 DriFox）

组件模板按需取用。通用规则先行，各模板代码可直接复制后替换文案与数值。

## 通用规则

- 卡片 `background:var(--panel)`、`border:0.5px solid var(--border)`、圆角 12px
- 界面间距用 rem（1/1.5/2），组件内部用 px（8/12/16）
- 表格数据仍用 markdown 表写在正文，不塞进 widget
- 界面色一律 CSS 变量，禁硬编码 hex（数据编码用 9-ramp，见 html-widget.md）

## 指标卡（指标展示/状态总览首选项）

```html
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px">
  <div style="background:var(--panel-soft);border-radius:8px;padding:1rem">
    <div style="font:400 13px sans-serif;color:var(--text-muted)">CPU 占用</div>
    <div style="font:500 24px sans-serif;color:var(--text);margin-top:4px">37%</div>
  </div>
</div>
```

带环比时在数值右侧加 `<span style="font:400 12px sans-serif;color:var(--success)">▲ 2.1%</span>`（恶化用 `var(--danger)` + ▼）。

## 方案对比卡（选型/方案对比首选项）

特色方案加 2px 边框 + 徽章：

```html
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px">
  <div style="background:var(--panel);border:2px solid var(--accent);border-radius:12px;padding:1rem 1.25rem">
    <span style="background:var(--accent-soft);color:var(--accent-text);font-size:12px;padding:2px 8px;border-radius:4px">推荐</span>
    <p style="font:500 15px sans-serif;color:var(--text);margin:8px 0 4px">方案 A</p>
    <p style="font:400 13px sans-serif;color:var(--text-secondary);margin:0">要点一句话</p>
  </div>
</div>
```

非特色方案去掉 2px 边框改 `0.5px solid var(--border)`，删徽章行。

## 数据记录卡（实体/配置/联系人展示）

44px 头像圆（`var(--accent-soft)` 底 + `var(--accent-text)` 字）+ 15px/500 姓名 + 13px 副题，外层 `var(--panel)` 卡、0.5px `var(--border)`、圆角 12px、padding 1rem 1.25rem。

## 交互解释器（参数调节/行为演示）

HTML 控件（slider/toggle）+ 实时数值显示；追问入口用 `context-tag[data-type="ask"]`。逻辑用纯 CSS（checkbox hack）或静态展示，无 script。控件细节见 html-widget.md 的 Host API 节。

## 2×2 决策矩阵（SVG，选型定位/需求排期）

横轴成本/复杂度，纵轴价值/收益。数据点 `cx/cy` 按坐标填，标签放点右侧 12px。viewBox 680，绘图区 [80,600]×[40,360]，中线 340/200。

```html
<style>
.t{font:400 12px sans-serif;fill:var(--text-muted)}
.th{font:500 14px sans-serif;fill:var(--text)}
</style>
<svg viewBox="0 0 680 420" width="100%" role="img">
  <title>决策矩阵</title>
  <rect x="80" y="40" width="260" height="160" fill="#EAF3DE" opacity="0.5" rx="8"/>
  <rect x="340" y="40" width="260" height="160" fill="#E6F1FB" opacity="0.5" rx="8"/>
  <rect x="80" y="200" width="260" height="160" fill="#F1EFE8" opacity="0.5" rx="8"/>
  <rect x="340" y="200" width="260" height="160" fill="#FCEBEB" opacity="0.5" rx="8"/>
  <line x1="80" y1="200" x2="600" y2="200" style="stroke:var(--border-strong)" stroke-width="1"/>
  <line x1="340" y1="40" x2="340" y2="360" style="stroke:var(--border-strong)" stroke-width="1"/>
  <text x="96" y="62" class="th">速赢（低成本·高价值）</text>
  <text x="356" y="62" class="th">战略投入</text>
  <text x="96" y="342" class="th">顺手做</text>
  <text x="356" y="342" class="th">暂缓</text>
  <text x="340" y="396" class="t" text-anchor="middle">成本 / 复杂度 →</text>
  <text x="40" y="200" class="t" text-anchor="middle" transform="rotate(-90 40 200)">价值 / 收益 →</text>
  <circle cx="180" cy="120" r="8" fill="#3B6D11"/>
  <text x="194" y="124" class="th">方案 A</text>
</svg>
```

禁旋转文字的例外：仅 y 轴名一处的 `rotate(-90)` 允许（svg-guide 基本规范禁的是内容文字旋转）。

## 垂直时间线（HTML，版本演进/事件序列/里程碑）

```html
<div style="border-left:2px solid var(--border);margin-left:8px;padding-left:20px">
  <div style="position:relative;margin-bottom:1.25rem">
    <span style="position:absolute;left:-25px;top:5px;width:8px;height:8px;border-radius:50%;background:var(--accent)"></span>
    <div style="font:500 14px sans-serif;color:var(--text)">v2.0 发布</div>
    <div style="font:400 13px sans-serif;color:var(--text-secondary);margin-top:2px">2026-09-01 · 一句话要点</div>
  </div>
  <div style="position:relative;margin-bottom:1.25rem">
    <span style="position:absolute;left:-25px;top:5px;width:8px;height:8px;border-radius:50%;background:var(--border-strong)"></span>
    <div style="font:500 14px sans-serif;color:var(--text)">v1.5 发布</div>
    <div style="font:400 13px sans-serif;color:var(--text-secondary);margin-top:2px">2026-08-10 · 一句话要点</div>
  </div>
</div>
```

当前/最新节点用 `var(--accent)`，历史节点用 `var(--border-strong)`。节点 ≤6，超出砍旧留新。

## 进度步骤条（HTML，实施路线/任务状态）

横向，≤5 步。圆点 24px，连线与圆心同高（`margin-bottom` 抵消下方文字高度）。

```html
<div style="display:flex;align-items:flex-start">
  <div style="display:flex;flex-direction:column;align-items:center;min-width:56px">
    <span style="width:24px;height:24px;border-radius:50%;background:var(--accent);color:var(--accent-text);font:500 12px sans-serif;display:flex;align-items:center;justify-content:center">✓</span>
    <span style="font:400 12px sans-serif;color:var(--text);margin-top:6px">已完成</span>
  </div>
  <div style="flex:1;height:2px;background:var(--accent);margin:11px 8px 0"></div>
  <div style="display:flex;flex-direction:column;align-items:center;min-width:56px">
    <span style="width:24px;height:24px;border-radius:50%;background:var(--panel);border:2px solid var(--accent);color:var(--accent-text);font:500 12px sans-serif;display:flex;align-items:center;justify-content:center">2</span>
    <span style="font:400 12px sans-serif;color:var(--text);margin-top:6px">进行中</span>
  </div>
  <div style="flex:1;height:2px;background:var(--border);margin:11px 8px 0"></div>
  <div style="display:flex;flex-direction:column;align-items:center;min-width:56px">
    <span style="width:24px;height:24px;border-radius:50%;background:var(--panel);border:1px solid var(--border);color:var(--text-muted);font:500 12px sans-serif;display:flex;align-items:center;justify-content:center">3</span>
    <span style="font:400 12px sans-serif;color:var(--text-muted);margin-top:6px">未开始</span>
  </div>
</div>
```

纵向 checklist 变体（修复建议/待办清单）：

```html
<div style="font:400 14px sans-serif;color:var(--text);line-height:1.9">
  <div><span style="color:var(--success)">✓</span> 已完成项</div>
  <div><span style="color:var(--accent)">●</span> 进行中项</div>
  <div><span style="color:var(--text-muted)">○</span> 未开始项</div>
</div>
```

## sparkline（指标卡迷你趋势）

无轴无刻度，尾点高亮，嵌在指标卡数值下方：

```html
<svg viewBox="0 0 120 36" width="120" height="36" style="display:block;margin-top:6px" aria-hidden="true">
  <polyline points="0,28 15,25 30,27 45,20 60,22 75,14 90,16 105,9 118,6"
    fill="none" style="stroke:var(--accent)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="118" cy="6" r="2.5" style="fill:var(--accent)"/>
</svg>
```

y 值范围 4~32（viewBox 高 36 上下留 4px）。8~12 个点足够表达走势，不必还原全部采样。

## 状态徽章组（服务/任务状态总览）

透明底 + 1px 语义色边 + 语义色字，不硬编码底色：

```html
<div style="display:flex;flex-wrap:wrap;gap:8px">
  <span style="font:400 12px sans-serif;padding:3px 10px;border-radius:999px;border:1px solid var(--success);color:var(--success)">● API 网关</span>
  <span style="font:400 12px sans-serif;padding:3px 10px;border-radius:999px;border:1px solid var(--accent);color:var(--accent-text)">● 任务队列</span>
  <span style="font:400 12px sans-serif;padding:3px 10px;border-radius:999px;border:1px solid var(--danger);color:var(--danger)">● 定时任务</span>
  <span style="font:400 12px sans-serif;padding:3px 10px;border-radius:999px;border:1px solid var(--border-strong);color:var(--text-muted)">● 旧版服务</span>
</div>
```

语义约定：`--success` 正常 / `--accent-text` 进行或关注 / `--danger` 异常 / `--text-muted` 停用或不相关。

// 一次性校验：分组流式工具栏跟随态（group_dom.GROUP_JS）
// 逻辑与浏览器语义对齐（scrollTop 赋值钳到 max；值未变不派发 scroll）。
const fs = require('fs');
const vm = require('vm');

const srcPath = process.argv[2] || 'app/widgets/render/group_dom.py';
const src = fs.readFileSync(srcPath, 'utf8');
const m = src.match(/GROUP_JS = """([\s\S]*?)"""/);
if (!m) throw new Error('GROUP_JS 未找到');
let js = m[1];
const cut = js.indexOf('document.addEventListener');
if (cut < 0) throw new Error('裁剪点未找到');
js = js.slice(0, cut); // 只取跟随链（_applyMessageGroups 依赖完整 DOM）

let rafQueue = [];
global.requestAnimationFrame = (fn) => { rafQueue.push(fn); return rafQueue.length; };
function flushRaf() { const q = rafQueue; rafQueue = []; q.forEach((fn) => fn()); }

class El {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.children = [];
    this.attrs = {};
    this.listeners = {};
    this.parentNode = null;
    this.className = '';
    this._scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
  }
  get firstElementChild() { return this.children[0] || null; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  toggleAttribute(k, on) { if (on) this.attrs[k] = ''; else delete this.attrs[k]; return !!on; }
  removeAttribute(k) { delete this.attrs[k]; }
  get scrollTop() { return this._scrollTop; }
  set scrollTop(v) {
    const max = Math.max(0, this.scrollHeight - this.clientHeight);
    const nv = Math.max(0, Math.min(v, max));
    if (nv === this._scrollTop) return; // 值未变：浏览器不派发 scroll
    this._scrollTop = nv;
    if (typeof this.onscroll === 'function') this.onscroll();
  }
}
global.document = { createElement: (t) => new El(t), addEventListener() {} };
global.matchMedia = () => ({ matches: false });
global.reportHeightDebounced = () => {};
let roCb = null;
global.ResizeObserver = class {
  constructor(cb) { roCb = cb; }
  observe() {}
  unobserve() {}
};

vm.runInThisContext(js);

const panel = _createGroupPanel();
const items = panel.firstElementChild;
panel.clientHeight = 264;
panel.toggleAttribute('data-live', true);

let failed = 0;
function check(name, cond, detail) {
  if (cond) { console.log(`PASS ${name}`); return; }
  failed++;
  console.log(`FAIL ${name}: ${detail}`);
}
function grow(h) { panel.scrollHeight = h; roCb([{ target: items }]); }
function userScrollTo(top) { panel._scrollTop = top; panel.onscroll(); }

// 1. 内容增长 → RO 补滚贴底
grow(1000);
check('增长后贴底', panel.scrollTop === 736 && panel._followTail === true, `top=${panel.scrollTop} follow=${panel._followTail}`);
flushRaf();

// 2. 程序置底派生的 scroll 不得关闭跟随
check('程序滚底不关跟随', panel._followTail === true && panel._progDepth === 0, `follow=${panel._followTail} depth=${panel._progDepth}`);

// 3. 内容再增长 → 继续补滚（旧实现此时 _lastFollowTop 已失真）
grow(1200);
check('二次增长继续贴底', panel.scrollTop === 936 && panel._followTail === true, `top=${panel.scrollTop}`);
flushRaf();

// 4. 真离底 110px（用户阅读）→ 关跟随，RO 不再补滚
panel.scrollHeight = 1300;
userScrollTo(926);
check('离开底部关闭跟随', panel._followTail === false, `follow=${panel._followTail}`);
const before = panel.scrollTop;
grow(1500);
check('非跟随态不补滚', panel.scrollTop === before, `top=${panel.scrollTop} before=${before}`);

// 5. 距底 10px（旧阈值 4px 会判离开）→ 恢复跟随
panel.clientHeight = 264;
panel.scrollHeight = 1500;
userScrollTo(1226); // max=1236 → 距底 10
check('24px 容差内恢复跟随', panel._followTail === true, `follow=${panel._followTail}`);
grow(1600);
check('恢复后被拉回底部', panel.scrollTop === 1336, `top=${panel.scrollTop}`);

// 6. 上滚 wheel 立即停跟随；贴顶时上滚不算（滚轮是给外层的）
panel._scrollTop = 1000;
const wheel = panel.listeners.wheel[0];
wheel({ deltaY: -120 });
check('上滚立即停跟随', panel._followTail === false, `follow=${panel._followTail}`);
panel._followTail = true;
panel._scrollTop = 0;
wheel({ deltaY: -120 });
check('贴顶上滚不停跟随', panel._followTail === true, `follow=${panel._followTail}`);

process.exit(failed ? 1 : 0);

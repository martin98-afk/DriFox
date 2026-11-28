
var _groupHeightSync = false;
// 跟随态判定的距底容差。旧值 4px 且只由 scroll 事件推断：一次布局重排引起的
// scroll（非用户滚轮）差出几像素就会把 _followTail 永久关掉，而关闭之后没有
// 任何恢复路径 —— 表现为「没动滚轮，工具栏自己就不置底了」。24px 与主程序
// AT_BOTTOM_TOLERANCE 同口径。
var _GROUP_FOLLOW_TOL = 24;
// 程序置底令牌：panel.scrollTop = scrollHeight 会派发 scroll，必须与用户滚动
// 区分。旧实现用 _lastFollowTop 等值比较，内容在同一帧继续增长时基准立即失效
// → 自身事件被当成用户滚动 → 跟随被误关。改为深度令牌 + 下一帧清零
// （同 app/main_widget._programmatic_scroll、骨架 _progScroll 语义）。
function _followGroupTail(panel) {
    panel._progDepth = (panel._progDepth || 0) + 1;
    if (!panel._progRaf) {
        panel._progRaf = requestAnimationFrame(function() {
            panel._progRaf = 0;
            panel._progDepth = 0;
        });
    }
    panel.scrollTop = panel.scrollHeight;
}
// 观察内容的自然高度。外层限高后，继续追加工具或异步展开结果仍能收到通知。
var _groupResize = new ResizeObserver(function(entries) {
    entries.forEach(function(entry) {
        var panel = entry.target.parentNode;
        if (!panel || !panel.hasAttribute('data-live') || !panel.clientHeight || !panel._followTail) return;
        _followGroupTail(panel);
    });
});
function _createGroupPanel() {
    var panel = document.createElement('div');
    panel.className = 'dfx-group-body';
    var items = document.createElement('div');
    items.className = 'dfx-group-items';
    panel.appendChild(items);
    panel._followTail = true;
    panel.onscroll = function() {
        if (!panel.hasAttribute('data-live') || !panel.clientHeight) return;
        // 程序置底引起的 scroll 不改变跟随态（rAF 兜底清零，无需逐个配平）
        if (panel._progDepth > 0) return;
        panel._followTail = panel.scrollHeight - panel.clientHeight - panel.scrollTop <= _GROUP_FOLLOW_TOL;
    };
    // 上滚意图同步置位（scroll 事件异步派发，抢不过同一帧的 RO 补滚）。
    // 门控：容器真能滚、且不在顶部 —— 否则滚轮是给外层聊天列表的冒泡残留。
    panel.addEventListener('wheel', function(e) {
        if (e.deltaY >= 0 || !panel.hasAttribute('data-live')) return;
        if (panel.scrollHeight <= panel.clientHeight || panel.scrollTop <= 0) return;
        panel._followTail = false;
    }, {passive: true});
    return panel;
}
document.addEventListener('keydown', function(e) { if (e.key === 'Tab') document.body.classList.add('dfx-key-focus'); }, true);
document.addEventListener('mousedown', function() { document.body.classList.remove('dfx-key-focus'); }, true);
function _setGroupVisibility(node, hidden, animate) {
    var previous = node._groupMotion;
    if (previous && !hidden) return;
    var wasHidden = node.hasAttribute('data-group-hidden');
    if (!previous && wasHidden === hidden) return;
    if (previous) {
        previous.cancel();
        node._groupMotion = null;
        node.removeAttribute('data-group-motion');
    }
    if (animate) _groupHeightSync = true;
    // 收起只提交最终高度，避免 WebView 和外层滚动区逐帧交错移动正文。
    if (hidden || !animate || matchMedia('(prefers-reduced-motion: reduce)').matches) {
        node.toggleAttribute('data-group-hidden', hidden);
        return;
    }
    node.removeAttribute('data-group-hidden');
    var to = node.getBoundingClientRect().height;
    if (to < 1) return;
    var motion = node.animate([
        {height:'0px', opacity:0, overflow:'hidden'},
        {height:to+'px', opacity:1, overflow:'hidden'}
    ], {duration:180, easing:'cubic-bezier(.22,.61,.36,1)', fill:'both'});
    node._groupMotion = motion;
    node.setAttribute('data-group-motion', '');
    motion.onfinish = function() {
        if (node._groupMotion !== motion) return;
        node.removeAttribute('data-group-motion');
        node._groupMotion = null; motion.cancel();
        if (node.isConnected) reportHeightDebounced();
    };
}
window._applyMessageGroups = function(groups, streaming) {
    // 完成态布局（含历史回放）已定型，让外层采用本批实测高度。
    if (streaming === false && groups.length) _groupHeightSync = true;
    var root = document.getElementById('content-placeholder');
    var nodes = window._blockNodes(root);
    var manual = root._groupOverrides || (root._groupOverrides = new Map());
    var buttons = new Map();
    root.querySelectorAll(':scope > .dfx-group-button').forEach(function(b) {
        buttons.set(b.dataset.groupStart, b);
    });
    if (streaming === undefined) streaming = root._groupsStreaming;
    root._groupsStreaming = streaming;
    var slots = [], assigned = new Set();
    groups.forEach(function(g) {
        var first = nodes[g[0]], key = String(g[0]);
        if (!first) return;
        var b = buttons.get(key);
        if (!b) {
            b = document.createElement('button'); b.type = 'button';
            b.className = 'dfx-group-button'; b.dataset.groupStart = key;
            b.onclick = function() {
                console.log('pywebview_action:group_toggle');
                manual.set(key, b.getAttribute('aria-expanded') !== 'true');
                window._applyMessageGroups(root._currentGroups);
                reportHeight();
            };
        }
        buttons.delete(key);
        var panel = b._panel;
        if (!panel) { panel = _createGroupPanel(); b._panel = panel; }
        var items = panel.firstElementChild;
        slots[g[0]] = [b,panel];
        if (panel.hasAttribute('data-live') !== !!streaming) {
            if (streaming) {
                // 新一轮流式从跟随态起步：上一轮用户上滚留下的 false 不能继承
                panel._followTail = true;
                panel._progDepth = 0;
                _groupResize.observe(items);
            } else {
                _groupResize.unobserve(items);
            }
        }
        panel.toggleAttribute('data-live', !!streaming);
        for (var i=g[0]; i<g[1]; i++) {
            assigned.add(i);
            if (nodes[i] && nodes[i].parentNode !== items) items.insertBefore(nodes[i], items.children[i-g[0]] || null);
        }
        // 已结束的分组在手动展开后仍使用完成态标题。
        b.toggleAttribute('data-group-complete', !g[2]);
        if (!g[2]) {
            var toolIcon = panel.querySelector('.tool-block img');
            var src = toolIcon ? toolIcon.getAttribute('src') : '';
            if (b._groupIconSrc !== src) {
                b._groupIconSrc = src;
                b.toggleAttribute('data-qrc-icon', src.startsWith('qrc:'));
                if (src) b.style.setProperty('--dfx-group-icon', 'url(' + JSON.stringify(src) + ')');
                else b.style.removeProperty('--dfx-group-icon');
            }
        }
        var opened = manual.has(key) ? manual.get(key) : g[2];
        var label = g[3], failed = g[4] || '';
        if (b.textContent !== label + failed) {
            var summary = document.createElement('span'); summary.className = 'dfx-group-label';
            summary.textContent = label; b.textContent = ''; b.appendChild(summary);
            if (failed) {
                var badge = document.createElement('span'); badge.className = 'dfx-group-fail';
                badge.textContent = failed; b.appendChild(badge);
            }
        }
        var last = b.getAttribute('aria-expanded');
        if (last !== String(opened)) b.setAttribute('aria-expanded', String(opened));
        _setGroupVisibility(panel, !opened, last !== null && last !== String(opened));
    });
    // 从尾向前仅校正变动项。已稳定的正文/图表保持挂载。
    var next = null;
    for (var i=nodes.length-1; i>=0; i--) {
        var row = slots[i] || (assigned.has(i) ? [] : [nodes[i]]);
        for (var j=row.length-1; j>=0; j--) {
            var el=row[j];
            if (el.parentNode !== root || el.nextElementSibling !== next) root.insertBefore(el,next);
            next=el;
        }
    }
    buttons.forEach(function(b,k) {
        _groupResize.unobserve(b._panel.firstElementChild);
        b._panel.remove(); b.remove(); manual.delete(k);
    });
    // 搬移/重排后补一次置底：令牌同步置位，顺带吞掉重排引起的 scroll 事件
    // （否则它会被当成用户滚动把跟随关掉，之后再没有补滚路径）。
    root.querySelectorAll('.dfx-group-body[data-live]').forEach(function(panel) {
        if (panel._followTail && panel.clientHeight) _followGroupTail(panel);
    });
    root._currentGroups = groups;
};

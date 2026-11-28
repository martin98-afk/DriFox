"""就地插入标题，不包裹或重建 patchBlocks 的子节点。"""
GROUP_CSS = """
.dfx-group-body { display:flow-root; }
.dfx-group-body[data-live] { max-height:264px; overflow-y:auto; }
.dfx-group-body[data-group-hidden] { display:none !important; }
body.dfx-grouped #tool-section { display:none !important; }
"""

GROUP_JS = """
var _groupHeightSync = false;
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
        if (!panel) { panel = document.createElement('div'); panel.className = 'dfx-group-body'; b._panel = panel; }
        slots[g[0]] = [b,panel];
        var follow = panel.scrollHeight - panel.clientHeight - panel.scrollTop < 4;
        panel.toggleAttribute('data-live', !!streaming);
        for (var i=g[0]; i<g[1]; i++) {
            assigned.add(i);
            if (nodes[i] && nodes[i].parentNode !== panel) panel.insertBefore(nodes[i], panel.children[i-g[0]] || null);
        }
        if (follow && streaming) panel.scrollTop = panel.scrollHeight;
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
    buttons.forEach(function(b,k) { if(b._panel)b._panel.remove(); b.remove(); manual.delete(k); });
    root._currentGroups = groups;
};
"""

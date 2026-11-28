"""就地插入标题，不包裹或重建 patchBlocks 的子节点。"""
GROUP_CSS = """
#content-placeholder > [data-group-member] { display:flow-root; }
#content-placeholder > [data-group-hidden] { display:none !important; }
body.dfx-grouped #tool-section { display:none !important; }
"""

GROUP_JS = """
document.addEventListener('keydown', function(e) { if (e.key === 'Tab') document.body.classList.add('dfx-key-focus'); }, true);
document.addEventListener('mousedown', function() { document.body.classList.remove('dfx-key-focus'); }, true);
function _setGroupVisibility(node, hidden, animate) {
    var previous = node._groupMotion;
    if (previous && previous.hidden === hidden) return;
    var wasHidden = node.hasAttribute('data-group-hidden');
    if (!previous && wasHidden === hidden) return;
    var from = wasHidden ? 0 : node.getBoundingClientRect().height;
    var alpha = wasHidden ? 0 : parseFloat(getComputedStyle(node).opacity);
    if (previous) { previous.animation.cancel(); node._groupMotion = null; }
    if (!animate || matchMedia('(prefers-reduced-motion: reduce)').matches) {
        node.toggleAttribute('data-group-hidden', hidden);
        return;
    }
    node.removeAttribute('data-group-hidden');
    var to = hidden ? 0 : node.getBoundingClientRect().height;
    if (Math.abs(to-from) < 1) { node.toggleAttribute('data-group-hidden', hidden); return; }
    var motion = node.animate([
        {height:from+'px', opacity:alpha, overflow:'hidden'},
        {height:to+'px', opacity:hidden ? 0 : 1, overflow:'hidden'}
    ], {duration:180, easing:'cubic-bezier(.22,.61,.36,1)', fill:'both'});
    node._groupMotion = {hidden:hidden, animation:motion};
    motion.onfinish = function() {
        if (!node._groupMotion || node._groupMotion.animation !== motion) return;
        node.toggleAttribute('data-group-hidden', hidden);
        node._groupMotion = null; motion.cancel();
        if (node.isConnected) reportHeightDebounced();
    };
}
window._applyMessageGroups = function(groups) {
    var root = document.getElementById('content-placeholder');
    var nodes = window._blockNodes(root);
    var manual = root._groupOverrides || (root._groupOverrides = new Map());
    var buttons = new Map();
    root.querySelectorAll(':scope > .dfx-group-button').forEach(function(b) {
        buttons.set(b.dataset.groupStart, b);
    });
    var hidden = new Set(), changed = new Set(), members = new Set();
    groups.forEach(function(g) {
        var first = nodes[g[0]], key = String(g[0]);
        if (!first) return;
        var b = buttons.get(key);
        if (!b) {
            b = document.createElement('button'); b.type = 'button';
            b.className = 'dfx-group-button'; b.dataset.groupStart = key;
            b.onclick = function() {
                manual.set(key, b.getAttribute('aria-expanded') !== 'true');
                window._applyMessageGroups(root._currentGroups);
                reportHeightDebounced();
            };
        }
        buttons.delete(key);
        if (first.previousElementSibling !== b) root.insertBefore(b, first);
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
        for (var i=g[0]; i<g[1]; i++) {
            members.add(i); if (!opened) hidden.add(i);
            if (last !== null && last !== String(opened)) changed.add(i);
        }
    });
    buttons.forEach(function(b,k) { b.remove(); manual.delete(k); });
    nodes.forEach(function(n,i) {
        n.toggleAttribute('data-group-member', members.has(i));
        _setGroupVisibility(n, hidden.has(i), changed.has(i));
    });
    root._currentGroups = groups;
};
"""

"""就地插入标题，不包裹或重建 patchBlocks 的子节点。"""
GROUP_CSS = """
#content-placeholder > [data-group-hidden] { display:none !important; }
body.dfx-grouped #tool-section { display:none !important; }
"""

GROUP_JS = """
document.addEventListener('keydown', function(e) { if (e.key === 'Tab') document.body.classList.add('dfx-key-focus'); }, true);
document.addEventListener('mousedown', function() { document.body.classList.remove('dfx-key-focus'); }, true);
window._applyMessageGroups = function(groups) {
    var root = document.getElementById('content-placeholder');
    var nodes = window._blockNodes(root);
    var manual = root._groupOverrides || (root._groupOverrides = new Map());
    var buttons = new Map();
    root.querySelectorAll(':scope > .dfx-group-button').forEach(function(b) {
        buttons.set(b.dataset.groupStart, b);
    });
    var hidden = new Set();
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
        if (b.getAttribute('aria-expanded') !== String(opened)) b.setAttribute('aria-expanded', String(opened));
        if (!opened) for (var i=g[0]; i<g[1]; i++) hidden.add(i);
    });
    buttons.forEach(function(b,k) { b.remove(); manual.delete(k); });
    nodes.forEach(function(n,i) {
        if (hidden.has(i) !== n.hasAttribute('data-group-hidden')) n.toggleAttribute('data-group-hidden', hidden.has(i));
    });
    root._currentGroups = groups;
};
"""

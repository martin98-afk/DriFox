"""就地插入标题，不包裹或重建 patchBlocks 的子节点。"""
GROUP_CSS = """
.dfx-group-button { display:block; width:100%; text-align:left; font:inherit;
 color:var(--text-secondary); background:transparent; border:0; cursor:pointer;
 padding:5px 2px; margin:2px 0; }
.dfx-group-button::before { content:'▸'; display:inline-block; width:1.2em; }
.dfx-group-button[aria-expanded=true]::before { content:'▾'; }
#content-placeholder > [data-group-hidden] { display:none !important; }
body.dfx-grouped #tool-section { display:none !important; }
"""

GROUP_JS = """
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
        var label = g[3];
        if (b.textContent !== label) b.textContent = label;
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

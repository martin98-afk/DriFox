from app.plugins.contracts.message_layout import OutputMode


def grouped_runs(kinds, *, streaming):
    start = None
    for index, kind in enumerate(kinds):
        if kind in ('reasoning', 'tool'):
            if start is None:
                start = index
        elif kind != 'blank' and start is not None:
            yield start, index, False
            start = None
    if start is not None:
        yield start, len(kinds), streaming


CSS = '''
.dfx-group-button { appearance:none; display:flex; align-items:center; width:100%; gap:.45em;
 background:transparent; border:0; text-align:left; font:inherit; font-size:.88em;
 line-height:1.5; color:var(--text-secondary); cursor:pointer; padding:4px 0; margin:2px 0; }
.dfx-group-button::before { content:''; width:.38em; height:.38em; margin:0 .4em 0 .2em;
 border-right:1.3px solid currentColor; border-bottom:1.3px solid currentColor;
 transform:rotate(-45deg); transition:transform 120ms ease; }
.dfx-group-button[aria-expanded=true]::before { transform:rotate(45deg); }
.dfx-group-button:hover { color:var(--text); }
.dfx-group-button:focus { outline:1px dotted currentColor; outline-offset:1px; }
.dfx-group-button .dfx-group-fail { color:var(--danger); }
@media(prefers-reduced-motion:reduce){.dfx-group-button::before {transition:none;}}
'''

COMPACT = OutputMode('compact', '简洁', '每段正文前收起本批思考与工具，失败次数单独标出。', grouped_runs, CSS)

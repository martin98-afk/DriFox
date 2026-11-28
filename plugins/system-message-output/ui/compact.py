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
#content-placeholder > .dfx-group-button { appearance:none; display:flex; align-items:center; width:100%; gap:.45em;
 background:transparent; border:0; outline:none; text-align:left; font:inherit; font-size:.82em;
 line-height:1.5; color:var(--text-muted); cursor:pointer; padding:4px 0; margin:2px 0; }
.dfx-group-button::before { content:''; width:.38em; height:.38em; margin:0 .4em 0 .2em;
 border-right:1.3px solid currentColor; border-bottom:1.3px solid currentColor; opacity:.76;
 transform:rotate(-45deg); transition:transform 120ms ease; }
.dfx-group-button[aria-expanded=true]::before { transform:rotate(45deg); }
body.dfx-key-focus .dfx-group-button:focus .dfx-group-label { text-decoration:underline; }
.dfx-group-button .dfx-group-fail { color:var(--danger); }
@media(prefers-reduced-motion:reduce){.dfx-group-button::before {transition:none;}}
'''

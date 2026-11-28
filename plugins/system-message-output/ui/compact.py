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
.dfx-group-button:not([data-group-complete])::before, .dfx-group-button[data-group-complete]::after {
 content:''; flex:0 0 auto; width:.38em; height:.38em; margin:0 .4em 0 .2em;
 border-right:1.3px solid currentColor; border-bottom:1.3px solid currentColor; opacity:.76;
 transform:rotate(-45deg); transition:transform 120ms ease; }
.dfx-group-button:not([data-group-complete])[aria-expanded=true]::before,
.dfx-group-button[data-group-complete][aria-expanded=true]::after { transform:rotate(45deg); }
.dfx-group-button[data-group-complete]::before {
 content:''; flex:0 0 1em; height:1em; background:currentColor;
 -webkit-mask:var(--dfx-group-icon, url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.8' stroke-linejoin='round'%3E%3Cpath d='M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5z'/%3E%3C/svg%3E")) center / contain no-repeat;
}
.dfx-group-button[data-group-complete] .dfx-group-label {
 min-width:0; overflow:hidden; white-space:nowrap; text-overflow:ellipsis;
}
body.dfx-key-focus .dfx-group-button:focus .dfx-group-label { text-decoration:underline; }
.dfx-group-button .dfx-group-fail { flex:none; color:var(--danger); }
@media(prefers-reduced-motion:reduce){.dfx-group-button::before,.dfx-group-button::after {transition:none;}}
'''

"""Persistent green cabinet shell, independent of editorial implementation."""
import re

CSS = '''
:root{--bg:#eef2f1;--surface:#fff;--ink:#17332f;--muted:#617570;--line:#dce5e1;--blue:#087f73;--blue2:#e0f3ec}
*{box-sizing:border-box}body{margin:0;max-width:none;padding:0;background:var(--bg);color:var(--ink);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.app{display:grid;grid-template-columns:244px minmax(0,1fr);min-height:100vh}.side{background:#fff;border-right:1px solid var(--line);padding:28px 16px;display:flex;flex-direction:column}.brand{font-size:22px;font-weight:750;padding:0 12px 30px}.brand span{display:block;font-size:11px;letter-spacing:.1em;color:var(--muted);text-transform:uppercase}.nav{display:grid;gap:7px}.nav a{color:var(--muted);text-decoration:none;padding:12px;border-radius:9px;font-weight:600}.nav a:hover,.nav a.active{background:var(--blue2);color:var(--blue)}.side-foot{margin-top:auto;padding:24px 12px;color:var(--muted);font-size:12px}.main{width:100%;max-width:1480px;padding:32px 48px;min-width:0}.main h1{font-size:30px;margin:4px 0 8px;letter-spacing:-.6px}.eyebrow,.sub{color:var(--muted)}.top{margin-bottom:28px}.panel,.row-card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:20px;margin:12px 0}.row-card h3{margin-top:0}a{color:var(--blue)}button{font:inherit;border:1px solid var(--line);background:#fff;color:var(--ink);padding:9px 14px;border-radius:8px;cursor:pointer}button:hover{background:var(--blue2)}#summary{padding:16px 20px;background:var(--blue2);border-radius:10px;margin:18px 0;color:var(--ink)}#notice{color:#a46a00}#content{overflow-wrap:anywhere}
@media(max-width:760px){.app{grid-template-columns:1fr}.side{padding:16px;border-right:0;border-bottom:1px solid var(--line)}.brand{padding:0 8px 12px}.nav{display:flex;flex-wrap:wrap;gap:3px}.nav a{padding:8px}.side-foot{display:none}.main{padding:20px 16px}}
'''


def green_shell(page):
    """Keep current actions and API code while preserving the approved shell."""
    if 'data-design="materials-path-v2"' in page:
        return page
    page = re.sub(r'<style>.*?</style>', '<style>'+CSS+'</style>', page, count=1, flags=re.S)
    page = re.sub(r'<main[^>]*>', '''<div class="app" data-design="materials-path-v2"><aside class="side"><div class="brand">Kovalsky<span>Newsroom</span></div><nav class="nav"><a class="active" href="?view=pipeline" onclick="show('news');return false">Материалы</a><a href="?view=edition" onclick="showEdition();return false">Редакция</a><a href="?view=published" onclick="show('posts');return false">Публикации</a><a href="?view=sources" onclick="show('sources');return false">Источники</a><a href="?view=resources" onclick="show('resources');return false">Расходы</a></nav><div class="side-foot">Кабинет редакции<br>Зелёный интерфейс · materials-path-v2</div></aside><main class="main">''', page, count=1)
    page = page.replace('<h1>Kovalsky</h1>', '<header class="top"><div class="eyebrow">Kovalsky · Редакция</div><h1 id="heading">Материалы</h1><div class="sub">Источники, подготовка и публикации</div></header>')
    page = page.replace('</main>', '</main></div>', 1)
    return page

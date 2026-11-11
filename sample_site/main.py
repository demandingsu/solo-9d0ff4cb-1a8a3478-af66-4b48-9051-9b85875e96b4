"""A small paginated newspaper scan site used for the offline demo.

URL layout (relative links work both directly and through the web app's
``/sample`` proxy):

    /                                      issue catalogue
    /issues/{slug}/page/{n}.html           paginated scan directory
    /scan/{slug}/{scan_id}.html            scan detail
    /img/{slug}-{scan_id}.svg              placeholder scan image
"""

from __future__ import annotations

import html
from math import ceil

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .data import ISSUE_ORDER, PAGE_SIZE, get_issue, scan_edition, scan_image_path, scan_issue

app = FastAPI(title="本地分页样本站点", docs_url=None, redoc_url=None)

STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0; background: #f4f1e8; color: #28251f;
  font-family: "Noto Serif SC", "Songti SC", "SimSun", Georgia, serif;
  line-height: 1.65;
}
.page { width: min(920px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 56px; }
.masthead { border-bottom: 3px double #8c2d22; padding-bottom: 12px; margin-bottom: 24px; }
.masthead a { color: #8c2d22; text-decoration: none; }
.eyebrow { margin: 0; color: #8c2d22; font-size: 13px; letter-spacing: .18em; text-transform: uppercase; }
h1 { margin: 4px 0 6px; font-size: clamp(26px, 4vw, 38px); line-height: 1.2; }
.lede { margin: 0; color: #645d51; }
.meta { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1px; margin: 22px 0 26px; background: #d8d1c2; border: 1px solid #d8d1c2; }
.meta > div { background: #fffdf7; padding: 12px 16px; }
.meta dt { color: #857d6e; font-size: 12px; letter-spacing: .12em; }
.meta dd { margin: 2px 0 0; font-size: 20px; font-weight: 700; }
table.scan-list { width: 100%; border-collapse: collapse; background: #fffdf7; border: 1px solid #d8d1c2; }
table.scan-list th, table.scan-list td { padding: 12px 14px; border-bottom: 1px solid #e7e1d5; text-align: left; vertical-align: top; }
table.scan-list th { background: #efe9dc; font-size: 13px; letter-spacing: .08em; color: #645d51; }
a { color: #8c2d22; }
a:hover { text-decoration-thickness: 2px; }
.detail-link { display: inline-block; padding: 3px 10px; border: 1px solid #8c2d22; border-radius: 999px; text-decoration: none; font-size: 13px; }
.detail-link:hover { background: #8c2d22; color: #fffdf7; }
.pagination { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 26px 0 0; }
.pagination a, .pagination span { min-width: 36px; padding: 6px 10px; border: 1px solid #c9c0af; border-radius: 4px; text-align: center; text-decoration: none; background: #fffdf7; }
.pagination a[aria-current="page"] { background: #8c2d22; border-color: #8c2d22; color: #fffdf7; }
.pagination .disabled { color: #a39b8c; background: #eee9de; }
.issue-grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); margin: 26px 0; }
.issue-card { display: block; padding: 20px; background: #fffdf7; border: 1px solid #d8d1c2; border-top: 4px solid #8c2d22; text-decoration: none; color: inherit; }
.issue-card:hover { border-color: #8c2d22; }
.issue-card h2 { margin: 0 0 6px; font-size: 22px; }
.issue-card p { margin: 0; color: #645d51; font-size: 14px; }
.ownership { display: inline-block; padding: 4px 12px; background: #efe9dc; border-radius: 4px; font-weight: 700; }
.scan-figure { margin: 26px 0 0; padding: 18px; background: #fffdf7; border: 1px solid #d8d1c2; text-align: center; }
.scan-figure img { max-width: 100%; width: 420px; border: 1px solid #d8d1c2; }
.scan-figure figcaption { margin-top: 10px; color: #645d51; font-size: 13px; }
.scan-figure.missing { color: #8c2d22; border-style: dashed; }
.footer { margin-top: 32px; color: #857d6e; font-size: 13px; }
@media (max-width: 640px) { .meta { grid-template-columns: 1fr; } table.scan-list th:nth-child(2), table.scan-list td:nth-child(2) { display: none; } }
"""


def shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} · 样本报纸扫描站</title>
  <style>{STYLE}</style>
</head>
<body>
<div class="page">
  <header class="masthead">
    <p class="eyebrow">Local Sample Archive</p>
    <h1>样本报纸扫描站</h1>
    <p class="lede">断网演示用的本地分页目录 · 详情页 · 占位图片</p>
  </header>
  {body}
  <p class="footer">样本站只提供演示数据，所有扫描图片均为占位 SVG。</p>
</div>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def catalogue() -> HTMLResponse:
    cards = []
    for slug in ISSUE_ORDER:
        issue = get_issue(slug)
        if issue is None:
            continue
        cards.append(
            f"""<a class="issue-card" href="issues/{slug}/page/1.html">
  <h2>{html.escape(issue['title'])}</h2>
  <p>{html.escape(issue['description'])}</p>
  <p style="margin-top:12px"><span class="ownership">{html.escape(issue['issue'])} · {html.escape(issue['edition'])}</span>
  <span style="margin-left:8px">共 {issue['total_pages']} 页</span></p>
</a>"""
        )
    body = f"""
<h1>选择一期目录</h1>
<p class="lede">每个目录分多页列出扫描详情。进入目录后，把浏览器地址栏中的 URL 交给验收网页即可。</p>
<div class="issue-grid">{''.join(cards)}</div>"""
    return HTMLResponse(shell("样本目录", body))


@app.get("/issues/{slug}/", response_class=HTMLResponse)
async def issue_root(slug: str) -> RedirectResponse:
    if get_issue(slug) is None:
        raise HTTPException(status_code=404, detail="期号不存在")
    return RedirectResponse(url=f"/issues/{slug}/page/1.html", status_code=307)


@app.get("/issues/{slug}/page/{page_no}.html", response_class=HTMLResponse)
async def directory(slug: str, page_no: int) -> HTMLResponse:
    issue = get_issue(slug)
    if issue is None:
        raise HTTPException(status_code=404, detail="期号不存在")
    scans = issue["scans"]
    total_pages = max(1, ceil(len(scans) / PAGE_SIZE))
    if page_no < 1 or page_no > total_pages:
        raise HTTPException(status_code=404, detail="目录页不存在")

    start = (page_no - 1) * PAGE_SIZE
    page_scans = scans[start : start + PAGE_SIZE]

    rows = []
    for offset, scan in enumerate(page_scans, start=start + 1):
        rows.append(
            f"""<tr>
  <td>{offset:02d}</td>
  <td>期版一致的扫描件</td>
  <td>{html.escape(scan['pages'])} 页</td>
  <td><a class="detail-link" href="../../../scan/{slug}/{scan['id']}.html">查看详情</a></td>
</tr>"""
        )

    pagination = []
    if page_no > 1:
        pagination.append(f'<a href="{page_no - 1}.html">上一页</a>')
    else:
        pagination.append('<span class="disabled">上一页</span>')
    for number in range(1, total_pages + 1):
        if number == page_no:
            pagination.append(f'<a href="{number}.html" aria-current="page">{number}</a>')
        else:
            pagination.append(f'<a href="{number}.html">{number}</a>')
    if page_no < total_pages:
        pagination.append(f'<a rel="next" href="{page_no + 1}.html">下一页 ›</a>')
    else:
        pagination.append('<span class="disabled">下一页 ›</span>')

    body = f"""
<h1>报纸扫描目录</h1>
<p class="lede">{html.escape(issue['title'])} · 第 {page_no} / {total_pages} 个目录页</p>
<div class="meta">
  <div><dt>期号</dt><dd class="issue-no">{html.escape(issue['issue'])}</dd></div>
  <div><dt>版次</dt><dd class="edition">{html.escape(issue['edition'])}</dd></div>
  <div><dt>总页数</dt><dd class="total-pages">{issue['total_pages']}</dd></div>
</div>
<table class="scan-list">
  <thead><tr><th>序号</th><th>记录</th><th>页码</th><th>详情</th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
<nav class="pagination" aria-label="目录分页">{''.join(pagination)}</nav>"""
    return HTMLResponse(shell(f"{issue['issue']} 目录", body))


@app.get("/scan/{slug}/{scan_id}.html", response_class=HTMLResponse)
async def detail(slug: str, scan_id: str) -> HTMLResponse:
    issue = get_issue(slug)
    if issue is None:
        raise HTTPException(status_code=404, detail="期号不存在")
    scan = next((item for item in issue["scans"] if item["id"] == scan_id), None)
    if scan is None:
        raise HTTPException(status_code=404, detail="扫描详情不存在")
    if scan.get("broken"):
        return HTMLResponse(
            shell(
                "详情不存在",
                """<h1>404 · 扫描详情抓取失败</h1>
<p class="lede">该条目录记录指向的详情页没有被归档，验收时将被标记为抓取失败。</p>""",
            ),
            status_code=404,
        )

    issue_no = scan_issue(scan, issue)
    edition = scan_edition(scan, issue)
    image_path = scan_image_path(slug, scan)
    notes = []
    if issue_no != issue["issue"]:
        notes.append("这条扫描声明的期号与目录不一致。")
    if edition != issue["edition"]:
        notes.append("这条扫描声明的版次与目录不一致。")
    if not scan.get("image"):
        notes.append("这条扫描没有图片链接。")

    if scan.get("image"):
        figure = f"""<figure class="scan-figure">
  <a class="scan-image" href="../../{image_path.lstrip('/')}"><img class="scan" src="../../{image_path.lstrip('/')}" alt="{html.escape(slug)} {html.escape(scan_id)} 扫描图"></a>
  <figcaption>扫描图片占位件 · {html.escape(slug)}-{html.escape(scan_id)}.svg</figcaption>
</figure>"""
    else:
        figure = """<figure class="scan-figure missing">
  <p><strong>图片链接缺失</strong></p>
  <p>这条记录只有页码，没有扫描图片链接，不能计入有效覆盖。</p>
</figure>"""

    note_html = ""
    if notes:
        note_html = '<p class="lede" style="margin-top:14px;color:#8c2d22">' + " ".join(notes) + "</p>"

    body = f"""
<h1>扫描详情</h1>
<div class="meta">
  <div><dt>所属期版</dt><dd class="ownership">{html.escape(issue_no)} {html.escape(edition)}</dd></div>
  <div><dt>页码</dt><dd class="pages">{html.escape(scan['pages'])}</dd></div>
  <div><dt>扫描记录</dt><dd>{html.escape(scan_id)}</dd></div>
</div>
{note_html}
{figure}
<p><a href="../../issues/{slug}/page/1.html">返回该期目录</a></p>"""
    # The detail markup also exposes machine-readable metadata so that the
    # auditor can work even when the visible labels change.
    meta = f"""<meta name="issue" content="{html.escape(issue_no)}">
<meta name="edition" content="{html.escape(edition)}">
<meta name="pages" content="{html.escape(scan['pages'])}">"""
    page = shell(f"{issue_no} {edition} {scan['pages']}", body)
    page = page.replace("</head>", f"  {meta}\n</head>")
    return HTMLResponse(page)


@app.get("/img/{filename}")
async def placeholder_image(filename: str) -> Response:
    stem = filename.rsplit(".", 1)[0]
    safe = html.escape(stem)
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="420" height="560" viewBox="0 0 420 560">
  <rect width="420" height="560" fill="#e9e2d3"/>
  <rect x="18" y="18" width="384" height="524" fill="#fffdf7" stroke="#b9ae99"/>
  <rect x="38" y="44" width="344" height="10" fill="#8c2d22"/>
  <rect x="38" y="68" width="230" height="6" fill="#b9ae99"/>
  <g fill="#d8d1c2">
    <rect x="38" y="110" width="344" height="14"/><rect x="38" y="142" width="344" height="14"/>
    <rect x="38" y="174" width="300" height="14"/><rect x="38" y="224" width="344" height="14"/>
    <rect x="38" y="256" width="344" height="14"/><rect x="38" y="288" width="250" height="14"/>
    <rect x="38" y="338" width="344" height="14"/><rect x="38" y="370" width="320" height="14"/>
    <rect x="38" y="402" width="344" height="14"/><rect x="38" y="434" width="210" height="14"/>
  </g>
  <text x="210" y="512" font-family="serif" font-size="16" fill="#645d51" text-anchor="middle">{safe}</text>
</svg>"""
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "sample-site"}

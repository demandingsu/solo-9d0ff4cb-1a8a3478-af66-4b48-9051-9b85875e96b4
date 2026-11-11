"""FastAPI web interface for the newspaper scan acceptance audit."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .scraper import (
    DirectoryUnavailable,
    InvalidDirectoryUrl,
    inspect_issue,
)

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_SITE_BASE_URL = os.getenv("SAMPLE_SITE_BASE_URL", "").strip().rstrip("/")
SAMPLE_SITE_PREFIX = os.getenv("SAMPLE_SITE_PREFIX", "/sample").strip().rstrip("/")
if not SAMPLE_SITE_PREFIX.startswith("/"):
    SAMPLE_SITE_PREFIX = "/" + SAMPLE_SITE_PREFIX

app = FastAPI(title="报纸扫描验收台", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

DEMO_ISSUES = [
    {
        "path": "/issues/clean/page/1.html",
        "title": "示例 A · 完整期",
        "description": "6 页均由唯一有效扫描覆盖。",
        "expected": "合格",
    },
    {
        "path": "/issues/conflict/page/1.html",
        "title": "示例 B · 重扫与缺页",
        "description": "第 3 页重扫；第 4、5 页缺扫。",
        "expected": "不合格",
    },
    {
        "path": "/issues/pending/page/1.html",
        "title": "示例 C · 详情 404",
        "description": "详情抓取失败，整期待核实。",
        "expected": "待核实",
    },
]


def _demo_context() -> tuple[list[dict], bool]:
    if not SAMPLE_SITE_BASE_URL:
        return [], False
    items = [
        {
            "url": f"{SAMPLE_SITE_PREFIX}{item['path']}",
            "title": item["title"],
            "description": item["description"],
            "expected": item["expected"],
        }
        for item in DEMO_ISSUES
    ]
    return items, True


def _resolve_input(raw: str, request: Request) -> str:
    value = (raw or "").strip()
    if not value:
        raise InvalidDirectoryUrl("请输入该期目录 URL。")
    if value.startswith("//"):
        value = f"{request.url.scheme}:{value}"
    elif value.startswith("/"):
        value = urljoin(str(request.base_url), value)
    elif "://" not in value:
        value = "http://" + value

    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise InvalidDirectoryUrl("目录 URL 必须使用 http:// 或 https://。")
    if not parsed.hostname:
        raise InvalidDirectoryUrl("目录 URL 缺少有效主机名。")
    return value.split("#", 1)[0]


def _render_index(request: Request, *, url: str = "", error: str = "", status_code: int = 200):
    demo_issues, demo_enabled = _demo_context()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "url": url,
            "error": error,
            "demo_issues": demo_issues,
            "demo_enabled": demo_enabled,
            "sample_prefix": SAMPLE_SITE_PREFIX,
        },
        status_code=status_code,
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _render_index(request)


@app.post("/inspect", response_class=HTMLResponse)
async def inspect(request: Request, url: str = Form("")) -> HTMLResponse:
    try:
        directory_url = _resolve_input(url, request)
    except InvalidDirectoryUrl as exc:
        return _render_index(request, url=url, error=str(exc), status_code=400)

    try:
        report = await inspect_issue(directory_url)
    except InvalidDirectoryUrl as exc:
        return _render_index(request, url=url, error=str(exc), status_code=400)
    except DirectoryUnavailable as exc:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"url": directory_url, "message": str(exc), "sample_prefix": SAMPLE_SITE_PREFIX},
            status_code=200,
        )

    return templates.TemplateResponse(
        request,
        "result.html",
        {
            "report": report,
            "page_status_labels": {
                "covered": "已覆盖",
                "conflict": "重扫冲突",
                "missing": "待补扫",
            },
            "sample_prefix": SAMPLE_SITE_PREFIX,
        },
    )


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "web"}


if SAMPLE_SITE_BASE_URL:

    @app.get(SAMPLE_SITE_PREFIX)
    async def sample_redirect() -> RedirectResponse:
        return RedirectResponse(url=f"{SAMPLE_SITE_PREFIX}/", status_code=307)

    @app.get(f"{SAMPLE_SITE_PREFIX}/{{path:path}}", response_class=Response)
    async def sample_proxy(request: Request, path: str) -> Response:
        """Serve the demo sample site through this app (same-origin demo links)."""
        target = f"{SAMPLE_SITE_BASE_URL}/{quote(path, safe='/')}"
        if request.url.query:
            target += f"?{request.url.query}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                upstream = await client.get(target, follow_redirects=False)
        except httpx.HTTPError as exc:
            return Response(
                content=f"样本站点不可达：{type(exc).__name__}: {exc}",
                status_code=502,
                media_type="text/plain; charset=utf-8",
            )

        headers = {
            "content-type": upstream.headers.get("content-type", "text/html; charset=utf-8"),
            "cache-control": "no-store",
        }
        location = upstream.headers.get("location")
        if location:
            absolute = urljoin(target, location)
            upstream_root = urlparse(SAMPLE_SITE_BASE_URL)
            redirected = urlparse(absolute)
            if redirected.netloc == upstream_root.netloc:
                new_path = redirected.path
                if upstream_root.path and new_path.startswith(upstream_root.path):
                    new_path = new_path[len(upstream_root.path) :]
                location = f"{SAMPLE_SITE_PREFIX}{new_path}"
                if redirected.query:
                    location += f"?{redirected.query}"
            headers["location"] = location

        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=headers,
        )

"""Crawl a newspaper issue's scan directory and audit page coverage.

The directory is paginated HTML.  Each directory page lists links to scan
detail pages.  A detail page declares the issue/edition it belongs to, the
page number (single page or a spread such as ``12-13``) and an image link.

Only details whose issue/edition matches the directory and which carry an
image link may cover pages.  A page covered by two different scans is a
rescan conflict; a page with no accepted scan is pending rescan.  Any failed
fetch makes the whole issue "needs verification" (待核实).
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

USER_AGENT = "NewspaperScanAudit/1.0 (offline demo)"
DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
MAX_DIRECTORY_PAGES = 100
MAX_DETAIL_PAGES = 2000
MAX_PAGES_PER_SCAN = 300
DETAIL_CONCURRENCY = 8

PAGINATION_MARKERS = (
    "pagination",
    "pager",
    "page-nav",
    "page-navigation",
    "paging",
    "paginate",
    "分页",
)
NEXT_TEXTS = (
    "下一页",
    "下页",
    "下一頁",
    "next",
    "next page",
    "nextpage",
    "›",
    "»",
    "→",
)
PREV_TEXTS = ("上一页", "上页", "上一頁", "前一页", "前页", "prev", "previous", "‹", "«", "←")
NAV_TEXTS = ("首页", "目录", "返回首页", "回首页", "返回", "home")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg")


class InvalidDirectoryUrl(ValueError):
    """The URL supplied by the user is not a usable http(s) URL."""


class DirectoryUnavailable(RuntimeError):
    """The first directory page could not be read or identified."""


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _nth_sibling(tag: Tag) -> Tag | None:
    """Return the next meaningful sibling, skipping whitespace-only strings."""
    sibling = tag.next_sibling
    while sibling is not None:
        if isinstance(sibling, Tag):
            return sibling
        if clean_text(str(sibling)):
            return None
        sibling = sibling.next_sibling
    return None


def _labeled_value(root: Tag, *labels: str) -> str:
    """Read a value from common ``dt/dd``, ``th/td`` or label/value markup."""
    wanted = {clean_text(label).rstrip("：:") for label in labels}
    for tag in root.find_all(["dt", "th", "label", "span", "div", "li", "p", "strong"]):
        own = clean_text(tag.get_text(" ", strip=True)).rstrip("：:")
        for label in wanted:
            # "期号" followed by its own value in one element.
            if own.startswith(label) and len(own) > len(label):
                rest = own[len(label) :].lstrip("：: ").strip()
                if rest:
                    return rest
            if own != label:
                continue
            sibling = _nth_sibling(tag)
            if sibling is not None:
                value = clean_text(sibling.get_text(" ", strip=True))
                if value:
                    return value
            parent = tag.parent
            if parent is not None and not isinstance(parent, BeautifulSoup):
                whole = clean_text(parent.get_text(" ", strip=True))
                match = re.search(rf"{re.escape(label)}\s*[：:]\s*(.+)$", whole)
                if match:
                    return clean_text(match.group(1))
    return ""


def _meta_value(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        for attribute in ("name", "property", "itemprop"):
            tag = soup.find("meta", attrs={attribute: name})
            if tag and tag.get("content"):
                return clean_text(str(tag["content"]))
    return ""


def _class_value(root: Tag, *class_names: str) -> str:
    for name in class_names:
        for tag in root.find_all(class_=name):
            value = clean_text(tag.get_text(" ", strip=True))
            if value:
                return value
    return ""


def _class_marker(tag: Tag) -> str:
    classes = tag.get("class") or []
    return " ".join(classes).lower()


# ---------------------------------------------------------------------------
# Issue / edition / page-number normalisation
# ---------------------------------------------------------------------------

_CN_DIGIT_MAP = {
    "零": "0",
    "〇": "0",
    "一": "1",
    "二": "2",
    "两": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
}


def _replace_chinese_numbers(value: str) -> str:
    """Replace simple Chinese numerals (up to 99) with Arabic digits."""

    def convert(match: re.Match[str]) -> str:
        text = match.group(0)
        if "十" in text:
            left, _, right = text.partition("十")
            tens = int(_CN_DIGIT_MAP.get(left, "1")) if left else 1
            ones = int(_CN_DIGIT_MAP.get(right, "0")) if right else 0
            return str(tens * 10 + ones)
        return "".join(_CN_DIGIT_MAP.get(char, char) for char in text)

    pattern = "十[一二三四五六七八九]?|[一二两三四五六七八九]十[一二三四五六七八九]?|[一二两三四五六七八九]"
    return re.sub(pattern, convert, value)


_ISSUE_DATE_RE = re.compile(
    r"\d{4}\s*[年./\-]\s*\d{1,2}\s*[月./\-]\s*\d{1,2}\s*日?|\d{8}"
)


def normalize_issue(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").strip().lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"^(期号|所属期号|所属期版)\s*[：:]?\s*", "", text)
    if text.startswith("第"):
        text = text[1:]
    if text.endswith("期"):
        text = text[:-1]
    date = re.fullmatch(r"(\d{4})[年./\-](\d{1,2})[月./\-](\d{1,2})日?", text)
    if date:
        return f"{int(date.group(1)):04d}-{int(date.group(2)):02d}-{int(date.group(3)):02d}"
    compact = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", text)
    if compact:
        return f"{compact.group(1)}-{compact.group(2)}-{compact.group(3)}"
    return text


def normalize_edition(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").strip().lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"^(版次|所属期版|期版)\s*[：:]?\s*", "", text)
    text = _replace_chinese_numbers(text)
    if text.startswith("第"):
        text = text[1:]
    if text.endswith("版"):
        text = text[:-1]
    return text


def split_ownership(value: str | None) -> tuple[str, str]:
    """Split a combined ownership label such as "2024-05-01 第3版"."""
    text = unicodedata.normalize("NFKC", value or "")
    text = re.sub(r"(所属期版|所属期号|期号|版次)\s*[：:]?\s*", " ", text)
    text = clean_text(text)
    if not text:
        return "", ""

    match = _ISSUE_DATE_RE.search(text)
    if match:
        issue = clean_text(match.group(0))
        rest = clean_text(text[: match.start()] + " " + text[match.end() :])
        return issue, rest

    parts = text.split()
    if len(parts) >= 2:
        return clean_text(parts[0]), clean_text(" ".join(parts[1:]))

    spaced = re.match(r"^(.*?期)(.*版.*)$", text)
    if spaced:
        return clean_text(spaced.group(1)), clean_text(spaced.group(2))
    return text, ""


def normalize_page_number_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    for dash in ("—", "–", "‑", "﹣", "－", "~", "～"):
        text = text.replace(dash, "-")
    for separator in ("，", "、", "；", ";"):
        text = text.replace(separator, ",")
    return clean_text(text)


def parse_page_numbers(value: str | None) -> list[int]:
    """Parse ``5``, ``12-13``, ``2,3,5-7`` or ``第2—3版`` into page numbers."""
    text = normalize_page_number_text(value)
    if not text:
        return []
    text = text.replace("第", " ")
    text = re.sub(r"版|页", " ", text)

    numbers: set[int] = set()
    range_re = re.compile(r"(\d{1,4})\s*-\s*(\d{1,4})")
    for match in range_re.finditer(text):
        start, end = int(match.group(1)), int(match.group(2))
        if start > end:
            start, end = end, start
        if 1 <= start <= end and end - start <= MAX_PAGES_PER_SCAN:
            numbers.update(range(start, end + 1))
        else:
            numbers.update(n for n in (start, end) if n >= 1)
    remainder = range_re.sub(" ", text)
    numbers.update(int(n) for n in re.findall(r"\d{1,4}", remainder) if int(n) >= 1)
    return sorted(numbers)


# ---------------------------------------------------------------------------
# Link helpers
# ---------------------------------------------------------------------------


def absolute_url(base_url: str, href: str) -> str:
    href = (href or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return ""
    joined = urldefrag(urljoin(base_url, href))[0]
    parsed = urlparse(joined)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    return joined


def _effective_port(parsed) -> int | None:
    if parsed.port:
        return parsed.port
    if parsed.scheme == "https":
        return 443
    if parsed.scheme == "http":
        return 80
    return None


def same_site(url: str, base_url: str) -> bool:
    left, right = urlparse(url), urlparse(base_url)
    if left.scheme not in ("http", "https") or right.scheme not in ("http", "https"):
        return False
    if not left.hostname or not right.hostname:
        return False
    return (
        left.hostname.lower() == right.hostname.lower()
        and _effective_port(left) == _effective_port(right)
    )


def _anchor_text(anchor: Tag) -> str:
    return clean_text(anchor.get_text(" ", strip=True))


def _inside_pagination(anchor: Tag) -> bool:
    for parent in anchor.parents:
        if not isinstance(parent, Tag):
            continue
        marker = (_class_marker(parent) + " " + str(parent.get("id") or "")).lower()
        if any(hint in marker for hint in PAGINATION_MARKERS):
            return True
    return False


def _is_prev_link(anchor: Tag) -> bool:
    rel = " ".join(anchor.get("rel") or []).lower()
    text = _anchor_text(anchor).lower()
    return "prev" in rel or any(word in text for word in PREV_TEXTS)


def extract_pagination_links(soup: BeautifulSoup, page_url: str) -> list[str]:
    candidates: list[Tag] = []
    for tag in soup.find_all(["nav", "div", "ul", "section", "footer"]):
        marker = (_class_marker(tag) + " " + str(tag.get("id") or "")).lower()
        if any(hint in marker for hint in PAGINATION_MARKERS):
            candidates.extend(tag.find_all("a", href=True))

    if not candidates:
        for anchor in soup.find_all("a", href=True):
            rel = " ".join(anchor.get("rel") or []).lower()
            text = _anchor_text(anchor).lower()
            if (
                "next" in rel
                or text == "next"
                or "下一页" in text
                or "下页" in text
                or text in ("›", "»", "→")
            ):
                candidates.append(anchor)

    urls: list[str] = []
    for anchor in candidates:
        if _is_prev_link(anchor):
            continue
        text = _anchor_text(anchor)
        if text.lower() in NAV_TEXTS:
            continue
        url = absolute_url(page_url, str(anchor.get("href", "")))
        if not url or not same_site(url, page_url):
            continue
        if url not in urls:
            urls.append(url)
    return urls


def _looks_like_detail_link(anchor: Tag, url: str) -> bool:
    classes = _class_marker(anchor)
    text = _anchor_text(anchor)
    path = urlparse(url).path.lower()
    rel = " ".join(anchor.get("rel") or []).lower()

    if any(hint in classes for hint in ("scan-detail", "scan-link", "detail-link", "scan-item")):
        return True
    if "detail" in classes:
        return True
    if re.search(r"/scan|/detail|scan\.|detail\.", path):
        return True
    if "详情" in text or "扫描件" in text:
        return True
    if "detail" in rel or "scan" in rel:
        return True
    for parent in anchor.parents:
        if not isinstance(parent, Tag):
            continue
        marker = _class_marker(parent)
        if any(hint in marker for hint in ("scan-list", "scan-table", "detail-list", "scan-grid")):
            return True
    return False


def extract_detail_links(soup: BeautifulSoup, page_url: str) -> list[str]:
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        if _inside_pagination(anchor):
            continue
        url = absolute_url(page_url, str(anchor.get("href", "")))
        if not url or not same_site(url, page_url):
            continue
        if _looks_like_detail_link(anchor, url) and url not in links:
            links.append(url)
    return links


# ---------------------------------------------------------------------------
# Page parsing
# ---------------------------------------------------------------------------


@dataclass
class DirectoryMetadata:
    issue: str = ""
    edition: str = ""
    total_pages_text: str = ""


def parse_directory_page(html: str, page_url: str) -> tuple[DirectoryMetadata, list[str], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    issue = (
        _meta_value(soup, "issue", "newspaper-issue")
        or _class_value(soup, "issue-no", "issue-number", "issue-code")
        or _labeled_value(soup, "期号", "所属期号")
    )
    edition = (
        _meta_value(soup, "edition")
        or _class_value(soup, "edition", "edition-no")
        or _labeled_value(soup, "版次")
    )
    total = (
        _meta_value(soup, "total-pages", "page-count", "total_pages")
        or _class_value(soup, "total-pages", "page-count", "total-pages-count")
        or _labeled_value(soup, "总页数", "页数", "总版面数")
    )

    if not total:
        whole = clean_text(soup.get_text(" ", strip=True))
        match = re.search(r"(?:总页数|共)\s*[：:]?\s*(\d{1,4})\s*页?", whole)
        if match:
            total = match.group(1)
    if not issue:
        whole = clean_text(soup.get_text(" ", strip=True))
        match = re.search(r"期号\s*[：:]\s*([^\s，,；;|]+)", whole)
        if match:
            issue = match.group(1)
    if not edition:
        whole = clean_text(soup.get_text(" ", strip=True))
        match = re.search(r"版次\s*[：:]\s*([^\s，,；;|]+)", whole)
        if match:
            edition = match.group(1)

    return (
        DirectoryMetadata(issue=clean_text(issue), edition=clean_text(edition), total_pages_text=clean_text(total)),
        extract_detail_links(soup, page_url),
        extract_pagination_links(soup, page_url),
    )


@dataclass
class ParsedDetail:
    issue: str
    edition: str
    pages_text: str
    page_numbers: list[int]
    image_url: str


def _extract_image_url(soup: BeautifulSoup, page_url: str) -> str:
    scored: list[tuple[int, str]] = []
    for image in soup.find_all("img"):
        source = image.get("src") or image.get("data-src") or image.get("data-original")
        if not source:
            continue
        source = str(source).strip()
        if source.startswith("data:"):
            continue
        url = absolute_url(page_url, source)
        if not url:
            continue
        score = 0
        marker = _class_marker(image)
        if any(hint in marker for hint in ("scan", "page", "photo", "paper")):
            score += 3
        for parent in image.parents:
            if not isinstance(parent, Tag):
                continue
            parent_marker = _class_marker(parent)
            if any(hint in parent_marker for hint in ("scan", "photo", "figure", "page")):
                score += 1
                break
        scored.append((score, url))
    if scored:
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1]

    for anchor in soup.find_all("a", href=True):
        marker = _class_marker(anchor)
        href = str(anchor.get("href"))
        path = urlparse(href).path.lower()
        if "scan" in marker or "image" in marker or path.endswith(IMAGE_EXTENSIONS):
            url = absolute_url(page_url, href)
            if url:
                return url

    og_image = _meta_value(soup, "og:image", "twitter:image")
    return absolute_url(page_url, og_image) if og_image else ""


def parse_detail_page(html: str, page_url: str) -> ParsedDetail:
    soup = BeautifulSoup(html, "html.parser")
    issue = (
        _meta_value(soup, "issue", "newspaper-issue")
        or _class_value(soup, "issue-no", "issue-number", "issue-code")
        or _labeled_value(soup, "期号", "所属期号")
    )
    edition = (
        _meta_value(soup, "edition")
        or _class_value(soup, "edition", "edition-no")
        or _labeled_value(soup, "版次")
    )
    ownership = (
        _meta_value(soup, "ownership", "issue-edition")
        or _class_value(soup, "ownership", "issue-edition")
        or _labeled_value(soup, "所属期版", "期版", "所属版面")
    )
    if (not issue or not edition) and ownership:
        owner_issue, owner_edition = split_ownership(ownership)
        issue = issue or owner_issue
        edition = edition or owner_edition

    pages_text = (
        _meta_value(soup, "pages", "page-numbers", "page-range")
        or _class_value(soup, "pages", "page-numbers", "page-range")
        or _labeled_value(soup, "页码", "扫描页码", "版面页码", "页码范围")
    )
    if not pages_text:
        whole = clean_text(soup.get_text(" ", strip=True))
        match = re.search(r"(?:页码|页码范围)\s*[：:]\s*([^\s，,；;|]+)", whole)
        if match:
            pages_text = match.group(1)

    image_url = _extract_image_url(soup, page_url)
    return ParsedDetail(
        issue=clean_text(issue),
        edition=clean_text(edition),
        pages_text=clean_text(pages_text),
        page_numbers=parse_page_numbers(pages_text),
        image_url=image_url,
    )


# ---------------------------------------------------------------------------
# Audit model
# ---------------------------------------------------------------------------


@dataclass
class ScanSource:
    code: str
    detail_url: str
    image_url: str
    pages_text: str
    page_numbers: list[int]
    issue: str
    edition: str
    notes: list[str] = field(default_factory=list)


@dataclass
class IgnoredScan:
    detail_url: str
    issue: str
    edition: str
    pages_text: str
    image_url: str
    reason: str


@dataclass
class FetchFailure:
    url: str
    stage: str
    message: str


@dataclass
class PageFinding:
    number: int
    sources: list[ScanSource]

    @property
    def status(self) -> str:
        if not self.sources:
            return "missing"
        if len(self.sources) > 1:
            return "conflict"
        return "covered"


@dataclass
class IssueReport:
    directory_url: str
    issue_no: str
    edition: str
    total_pages: int
    pages: list[PageFinding]
    accepted_scans: list[ScanSource]
    ignored_scans: list[IgnoredScan]
    failures: list[FetchFailure]
    directory_pages: list[str]
    warnings: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.failures:
            return "待核实"
        if self.conflict_pages or self.missing_pages:
            return "不合格"
        return "合格"

    @property
    def status_key(self) -> str:
        return {"合格": "ok", "不合格": "bad", "待核实": "pending"}[self.status]

    @property
    def covered_pages(self) -> int:
        return sum(1 for page in self.pages if page.sources)

    @property
    def detail_count(self) -> int:
        detail_failures = sum(1 for failure in self.failures if failure.stage == "详情")
        return len(self.accepted_scans) + len(self.ignored_scans) + detail_failures

    @property
    def conflict_pages(self) -> list[int]:
        return [page.number for page in self.pages if page.status == "conflict"]

    @property
    def missing_pages(self) -> list[int]:
        return [page.number for page in self.pages if page.status == "missing"]

    @property
    def status_note(self) -> str:
        if self.failures:
            extra = ""
            if self.conflict_pages:
                extra += f" 另发现第 {'、'.join(str(n) for n in self.conflict_pages)} 页重扫冲突，"
            if self.missing_pages:
                extra += f"第 {'、'.join(str(n) for n in self.missing_pages)} 页待补扫，"
            if extra:
                extra = extra.rstrip("，") + "。"
            return (
                f"共有 {len(self.failures)} 处抓取失败，整期不能判为合格，需人工核实后重新验收。"
                + extra
            )
        if self.conflict_pages:
            pages = "、".join(str(number) for number in self.conflict_pages)
            return f"第 {pages} 页被不同扫描重复覆盖，存在重扫冲突。"
        if self.missing_pages:
            pages = "、".join(str(number) for number in self.missing_pages)
            return f"第 {pages} 页没有有效扫描，需补扫。"
        return f"全部 {self.total_pages} 页均由唯一有效扫描覆盖。"


# ---------------------------------------------------------------------------
# Crawl and audit
# ---------------------------------------------------------------------------


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
    )


async def _fetch_html(client: httpx.AsyncClient, url: str) -> tuple[str | None, str, str]:
    """Return (html, error, final_url)."""
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}", url
    if response.status_code >= 400:
        return None, f"HTTP {response.status_code}", str(response.url)
    try:
        return response.text, "", str(response.url)
    except Exception as exc:  # pragma: no cover - defensive decoding fallback
        return None, f"响应无法解码: {exc}", str(response.url)


def _parse_total(value: str) -> int:
    match = re.search(r"\d{1,5}", value or "")
    if not match:
        return 0
    return int(match.group(0))


async def _load_directory(
    client: httpx.AsyncClient, start_url: str
) -> tuple[DirectoryMetadata, list[str], list[str], list[FetchFailure], list[str]]:
    start = urldefrag(start_url)[0]
    queue: deque[str] = deque([start])
    visited: set[str] = set()
    metadata_parts: dict[str, str] = {}
    detail_urls: list[str] = []
    seen_details: set[str] = set()
    directory_pages: list[str] = []
    failures: list[FetchFailure] = []
    warnings: list[str] = []

    while queue and len(visited) < MAX_DIRECTORY_PAGES:
        page_url = queue.popleft()
        if page_url in visited:
            continue
        visited.add(page_url)

        html, error, final_url = await _fetch_html(client, page_url)
        if error:
            if not directory_pages:
                raise DirectoryUnavailable(f"目录页抓取失败：{error}（{page_url}）")
            failures.append(FetchFailure(url=page_url, stage="目录分页", message=error))
            continue

        try:
            metadata, links, pagination = parse_directory_page(html, final_url)
        except Exception as exc:  # pragma: no cover - BeautifulSoup is defensive
            if not directory_pages:
                raise DirectoryUnavailable(f"目录页无法解析：{exc}") from exc
            failures.append(FetchFailure(url=page_url, stage="目录分页", message=f"页面解析失败: {exc}"))
            continue

        directory_pages.append(final_url)
        for key, value in (
            ("issue", metadata.issue),
            ("edition", metadata.edition),
            ("total", metadata.total_pages_text),
        ):
            if value and key not in metadata_parts:
                metadata_parts[key] = value
            elif value and key in metadata_parts:
                old = metadata_parts[key]
                differs = (
                    normalize_issue(old) != normalize_issue(value)
                    if key == "issue"
                    else normalize_edition(old) != normalize_edition(value)
                    if key == "edition"
                    else _parse_total(old) != _parse_total(value)
                )
                if differs:
                    warnings.append(
                        f"目录分页给出的{ {'issue': '期号', 'edition': '版次', 'total': '总页数'}[key] }不一致："
                        f"{old} / {value}（采用首次出现的值）"
                    )

        for link in links:
            if link not in seen_details:
                seen_details.add(link)
                detail_urls.append(link)

        for link in pagination:
            if link.startswith("http") and same_site(link, start) and link not in visited:
                queue.append(link)

    if not directory_pages:
        raise DirectoryUnavailable("没有读到任何目录页。")
    if len(visited) >= MAX_DIRECTORY_PAGES and queue:
        warnings.append(f"目录分页超过 {MAX_DIRECTORY_PAGES} 页，已停止继续读取。")
    if not detail_urls:
        warnings.append("目录中未发现扫描详情链接。")

    metadata = DirectoryMetadata(
        issue=metadata_parts.get("issue", ""),
        edition=metadata_parts.get("edition", ""),
        total_pages_text=metadata_parts.get("total", ""),
    )
    return metadata, detail_urls, directory_pages, failures, warnings


async def _inspect_detail(
    client: httpx.AsyncClient, url: str, semaphore: asyncio.Semaphore
) -> tuple[ParsedDetail | None, FetchFailure | None, str, str]:
    async with semaphore:
        html, error, final_url = await _fetch_html(client, url)
        if error:
            return None, FetchFailure(url=url, stage="详情", message=error), url, ""
        try:
            detail = parse_detail_page(html, final_url)
            return detail, None, url, final_url
        except Exception as exc:  # pragma: no cover - parser defensive fallback
            return None, None, url, f"详情页无法解析：{exc}"


def _ownership_text(issue: str, edition: str) -> str:
    return " ".join(part for part in (issue, edition) if part) or "（未标明）"


async def inspect_issue(directory_url: str, client: httpx.AsyncClient | None = None) -> IssueReport:
    """Read all directory pages and audit the scan coverage of one issue."""
    parsed = urlparse(directory_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise InvalidDirectoryUrl("请输入 http:// 或 https:// 开头的目录 URL。")

    own_client = client is None
    if client is None:
        client = make_client()

    try:
        metadata, detail_urls, directory_pages, dir_failures, warnings = await _load_directory(
            client, directory_url
        )

        total_pages = _parse_total(metadata.total_pages_text)
        if not metadata.issue:
            raise DirectoryUnavailable("目录没有给出期号，无法验收。")
        if not metadata.edition:
            raise DirectoryUnavailable("目录没有给出有效版次，无法验收。")
        if total_pages <= 0:
            raise DirectoryUnavailable("目录没有给出有效的总页数，无法验收。")

        semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
        detail_urls = detail_urls[:MAX_DETAIL_PAGES]
        if len(detail_urls) == MAX_DETAIL_PAGES:
            warnings.append(f"详情数量超过上限，仅检查前 {MAX_DETAIL_PAGES} 个。")
        results = await asyncio.gather(
            *[_inspect_detail(client, url, semaphore) for url in detail_urls]
        )
    finally:
        if own_client:
            await client.aclose()

    accepted: list[ScanSource] = []
    ignored: list[IgnoredScan] = []
    failures: list[FetchFailure] = list(dir_failures)

    for detail, failure, detail_url, parse_error in results:
        if failure is not None:
            failures.append(failure)
            continue
        if detail is None:
            ignored.append(
                IgnoredScan(
                    detail_url=detail_url,
                    issue="",
                    edition="",
                    pages_text="",
                    image_url="",
                    reason=parse_error or "详情页无法解析",
                )
            )
            continue

        if not detail.issue or not detail.edition:
            ignored.append(
                IgnoredScan(
                    detail_url=detail_url,
                    issue=detail.issue,
                    edition=detail.edition,
                    pages_text=detail.pages_text,
                    image_url=detail.image_url,
                    reason="详情未标明所属期版，无法确认归属",
                )
            )
            continue
        if (
            normalize_issue(detail.issue) != normalize_issue(metadata.issue)
            or normalize_edition(detail.edition) != normalize_edition(metadata.edition)
        ):
            ignored.append(
                IgnoredScan(
                    detail_url=detail_url,
                    issue=detail.issue,
                    edition=detail.edition,
                    pages_text=detail.pages_text,
                    image_url=detail.image_url,
                    reason=(
                        "期版不一致：目录为 "
                        f"{_ownership_text(metadata.issue, metadata.edition)}，"
                        f"详情为 {_ownership_text(detail.issue, detail.edition)}"
                    ),
                )
            )
            continue
        if not detail.image_url:
            ignored.append(
                IgnoredScan(
                    detail_url=detail_url,
                    issue=detail.issue,
                    edition=detail.edition,
                    pages_text=detail.pages_text,
                    image_url="",
                    reason="缺少图片链接，不能作为有效扫描",
                )
            )
            continue
        if not detail.page_numbers:
            ignored.append(
                IgnoredScan(
                    detail_url=detail_url,
                    issue=detail.issue,
                    edition=detail.edition,
                    pages_text=detail.pages_text,
                    image_url=detail.image_url,
                    reason="未给出可识别的页码",
                )
            )
            continue

        valid_pages = [number for number in detail.page_numbers if 1 <= number <= total_pages]
        notes: list[str] = []
        if len(valid_pages) != len(detail.page_numbers):
            notes.append(f"部分页码超出总页数（1–{total_pages}）")
        if not valid_pages:
            ignored.append(
                IgnoredScan(
                    detail_url=detail_url,
                    issue=detail.issue,
                    edition=detail.edition,
                    pages_text=detail.pages_text,
                    image_url=detail.image_url,
                    reason=f"页码超出总页数范围（共 {total_pages} 页）",
                )
            )
            continue

        accepted.append(
            ScanSource(
                code=f"S{len(accepted) + 1:02d}",
                detail_url=detail_url,
                image_url=detail.image_url,
                pages_text=detail.pages_text,
                page_numbers=sorted(set(valid_pages)),
                issue=detail.issue,
                edition=detail.edition,
                notes=notes,
            )
        )

    coverage: dict[int, list[ScanSource]] = {number: [] for number in range(1, total_pages + 1)}
    for scan in accepted:
        for number in scan.page_numbers:
            if number in coverage:
                coverage[number].append(scan)

    pages = [PageFinding(number=number, sources=coverage[number]) for number in range(1, total_pages + 1)]
    return IssueReport(
        directory_url=directory_url,
        issue_no=metadata.issue,
        edition=metadata.edition,
        total_pages=total_pages,
        pages=pages,
        accepted_scans=accepted,
        ignored_scans=ignored,
        failures=failures,
        directory_pages=directory_pages,
        warnings=warnings,
    )

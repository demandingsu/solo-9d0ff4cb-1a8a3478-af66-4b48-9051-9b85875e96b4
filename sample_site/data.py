"""Offline demo data for the paginated sample newspaper site."""

from __future__ import annotations

ISSUES: dict[str, dict] = {
    "clean": {
        "slug": "clean",
        "issue": "2024-05-01",
        "edition": "第3版",
        "total_pages": 6,
        "title": "示例 A · 完整期",
        "description": "6 个页面全部由期版一致且带图片的扫描唯一覆盖。",
        "expected": "合格",
        "scans": [
            {"id": "a1", "pages": "1", "image": True},
            {"id": "a2", "pages": "2-3", "image": True},
            {"id": "a3", "pages": "4", "image": True},
            {"id": "a4", "pages": "5", "image": True},
            {"id": "a5", "pages": "6", "image": True},
        ],
    },
    "conflict": {
        "slug": "conflict",
        "issue": "2024-05-02",
        "edition": "第3版",
        "total_pages": 8,
        "title": "示例 B · 重扫与缺页",
        "description": "第 3 页被两张扫描重复覆盖；第 4、5 页分别因期号不符和缺图未被采用。",
        "expected": "不合格",
        "scans": [
            {"id": "b1", "pages": "1", "image": True},
            {"id": "b2", "pages": "2-3", "image": True},
            {"id": "b3", "pages": "3", "image": True},
            {"id": "b4", "pages": "4", "image": True, "issue": "2023-12-31"},
            {"id": "b5", "pages": "5", "image": False},
            {"id": "b6", "pages": "6-7", "image": True},
            {"id": "b7", "pages": "8", "image": True},
        ],
    },
    "pending": {
        "slug": "pending",
        "issue": "2024-05-03",
        "edition": "第3版",
        "total_pages": 12,
        "title": "示例 C · 详情抓取失败",
        "description": "一个详情页返回 404；另有跨页重复覆盖、版次不符和缺页，整期只能待核实。",
        "expected": "待核实",
        "scans": [
            {"id": "c1", "pages": "1", "image": True},
            {"id": "c2", "pages": "2-3", "image": True},
            {"id": "c3", "pages": "4", "image": True},
            {"id": "c4", "pages": "4", "image": True},
            {"id": "c5", "pages": "5", "image": True, "broken": True},
            {"id": "c6", "pages": "6-7", "image": True},
            {"id": "c7", "pages": "8", "image": True},
            {"id": "c8", "pages": "9", "image": True, "edition": "第4版"},
            {"id": "c9", "pages": "10", "image": True},
        ],
    },
}

ISSUE_ORDER = ["clean", "conflict", "pending"]
PAGE_SIZE = 3


def get_issue(slug: str) -> dict | None:
    return ISSUES.get(slug)


def scan_issue(scan: dict, issue: dict) -> str:
    return scan.get("issue", issue["issue"])


def scan_edition(scan: dict, issue: dict) -> str:
    return scan.get("edition", issue["edition"])


def scan_image_path(slug: str, scan: dict) -> str:
    return f"/img/{slug}-{scan['id']}.svg"

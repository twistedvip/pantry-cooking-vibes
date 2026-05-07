"""Pure helpers shared by importers: HTML→text and type coercion."""

from __future__ import annotations

from html import unescape

from bs4 import BeautifulSoup


def _html_to_text(html: str | None) -> str | None:
    if not html:
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return unescape(html).strip() or None
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert(0, "- ")
        li.append("\n")
    for block in soup.find_all(("p", "div", "ol", "ul", "h1", "h2", "h3")):
        block.append("\n")
    text = unescape(soup.get_text()).strip()
    return text or None


def _to_float(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int | float)):
            return float(v)
        return float(str(v))
    except (TypeError, ValueError):
        return None


def _to_int(v: object) -> int | None:
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int | float)):
            return int(v)
        return int(str(v))
    except (TypeError, ValueError):
        return None

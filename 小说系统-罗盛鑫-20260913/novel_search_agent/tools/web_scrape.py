"""【工具4】WebScrape：给一个 url，把网页正文扒下来。

两种取法：

    static   requests + BeautifulSoup，快，够用（静态页、服务端渲染的页面）
    dynamic  Playwright 起真浏览器，能拿到 JS 渲染后的正文（小说阅读页、SPA）
    auto     先 static，正文太短（<200 字，典型的"内容在 JS 里"）再自动上 Playwright

返回 title + 正文。可选把全文存进沙箱，方便后面用 DBQuery 做向量检索。
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from langchain_core.tools import tool

from config import get_logger
from tools.common import fail, ok, resolve_in_sandbox, truncate

log = get_logger("tool.webscrape")

ACTIONS = ("auto", "static", "dynamic")
_SHORT_ENOUGH = 200          # 正文短于这个字数就怀疑"内容在 JS 里"
_NOISE_TAGS = ("script", "style", "noscript", "nav", "footer", "header", "aside", "form", "iframe")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


@tool
def web_scrape(url: str, action: str = "auto", max_chars: int = 6000,
               save_to: str = "", timeout: int = 30) -> str:
    """抓取指定网页的正文。抓不到内容时改用 dynamic 模式再试一次。

    Args:
        url: 完整网址，必须带 http:// 或 https://
        action: auto=先静态后动态；static=只用 requests；dynamic=只用 Playwright 浏览器
        max_chars: 返回正文的最大字数，默认 6000，超出会截断
        save_to: 可选，把完整正文存到沙箱里的这个相对路径，例如 "pages/ch1.txt"
        timeout: 超时秒数，默认 30
    """
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return fail("WebScrape", f"不是合法的 http/https 网址：{url!r}")

    action = (action or "auto").strip().lower()
    if action not in ACTIONS:
        action = "auto"
    timeout = max(5, min(int(timeout or 30), 120))
    max_chars = max(200, min(int(max_chars or 6000), 100000))

    log.info("web_scrape %s action=%s", url, action)

    title, text, used = "", "", ""
    errors = []

    if action in ("auto", "static"):
        try:
            title, text = _static(url, timeout)
            used = "static"
        except Exception as e:  # noqa: BLE001
            errors.append(f"static：{type(e).__name__}: {e}")

    if action == "dynamic" or (action == "auto" and len(text.strip()) < _SHORT_ENOUGH):
        try:
            title, text = _dynamic(url, timeout)
            used = "dynamic"
        except Exception as e:  # noqa: BLE001 浏览器没装、页面超时都落在这
            errors.append(f"dynamic：{type(e).__name__}: {e}")

    if not text.strip():
        hint = "；".join(errors) or "页面正文为空"
        return fail("WebScrape", f"{hint}\n若提示缺少浏览器，执行 `playwright install chromium` 后再试。")

    saved = ""
    if save_to:
        try:
            path = resolve_in_sandbox(save_to)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {title}\n源地址：{url}\n\n{text}", encoding="utf-8")
            saved = f"\n全文已保存到沙箱 {path.name}（{len(text)} 字）"
        except Exception as e:  # noqa: BLE001
            saved = f"\n保存失败：{e}"

    return ok(
        f"抓取方式：{used}\n标题：{title or '(无标题)'}\n链接：{url}\n"
        f"正文共 {len(text)} 字：\n{truncate(text, max_chars, '正文')}{saved}"
    )


# ---------------------------------------------------------------- 两种取法


def _static(url: str, timeout: int) -> tuple[str, str]:
    import requests
    from bs4 import BeautifulSoup

    resp = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = resp.encoding or resp.apparent_encoding
    return _parse(resp.text)


def _dynamic(url: str, timeout: int) -> tuple[str, str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=_UA)
            page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)          # 给前端渲染留一点时间
            return _parse(page.content())
        finally:
            browser.close()


def _parse(html: str) -> tuple[str, str]:
    """去掉噪声标签，取标题和正文。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    for tag in soup(list(_NOISE_TAGS)):
        tag.decompose()

    body = soup.body or soup
    text = body.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)       # 连续空行压成一个
    text = re.sub(r"[ \t]{2,}", " ", text)
    return title, text.strip()


if __name__ == "__main__":
    print(web_scrape.invoke({"url": "https://example.com", "action": "static"}))

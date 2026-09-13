"""微信读书（weread.qq.com）封面源：**只取封面和推荐值，不取正文**。

为什么需要第二个封面源：起点只有网络小说，库里《海底两万里》《机器岛》《雪山飞狐》
这类**出版书/经典**在起点根本搜不到 —— 起点回填命中率因此卡在 35% 左右。
微信读书收录出版书，正好补上这块。

边界（和起点那个源一致）：

- **只拿元数据**：封面图 URL、作者、推荐值、简介。不碰任何正文。
- **礼貌**：每次请求之间睡 1~3 秒；一个浏览器实例复用，不反复启停。
- **robots**：weread.qq.com 的 `/robots.txt` 返回的是应用页面，
  即**没有声明任何禁止规则**。没有禁止不等于鼓励，所以按最低限度来：
  只在缺封面时按书名查一次，不批量扫站、不做目录爬取。

**书名必须核对上才取封面**：搜索结果里有同名/近似名的书（译本、同人、
同名影视书），配错封面比没有封面更糟。匹配规则见 `_title_matches()`。
"""

from __future__ import annotations

import re

import requests.utils
from bs4 import BeautifulSoup

from config import CRAWL_UA, get_logger, log_failed_url
from crawler import polite_sleep
from titlematch import match_score, to_simplified   # 书名核对规则只有这一份，起点源也用它

log = get_logger("weread")

SEARCH_URL = "https://weread.qq.com/web/search/books?keyword={kw}"

# 封面图只认这两个域名，其他都是站点 UI 图标
_COVER_HOSTS = ("cdn.weread.qq.com/weread/cover", "wfqqreader")


def _parse_cards(html: str) -> list[dict]:
    """从渲染后的搜索结果页解出书籍卡片。"""
    soup = BeautifulSoup(html, "html.parser")
    cards = []
    for li in soup.select("li.wr_bookList_item"):
        img = li.find("img")
        src = (img.get("src") or "") if img else ""
        if not any(h in src for h in _COVER_HOSTS):
            continue
        text = li.get_text(" ", strip=True)
        # 卡片文案形如："海底两万里（名家名人译） [法]儒勒·凡尔纳 184 人今日阅读 推荐值 94.1% 简介…"
        title = text.split(" ")[0].strip()
        author = ""
        m = re.search(r"\]\s*([^\s]+)\s", text) or re.search(r"^[\u4e00-\u9fff（）()·\s]+\s([^\s0-9]{2,12})\s", text)
        if m:
            author = m.group(1).strip()
        rec = re.search(r"推荐值\s*([\d.]+)%", text)
        cards.append({
            "title": title,
            "author": author,
            "cover_url": src,
            "recommend": float(rec.group(1)) / 10 if rec else None,   # 94.1% → 9.4 分制
            "raw": text[:400],
        })
    return cards


# 连续这么多次查询失败就中止整轮回填。浏览器一崩，剩下的请求全都白发，
# 2026-09-03 实测空转了 980 本才发现（第 24 条）。
_MAX_CONSECUTIVE_FAILURES = 10


class WereadBrowser:
    """复用一个浏览器实例批量查封面。

    逐条启停 Playwright 的代价是每本 3~5 秒纯启动开销，134 本就是十分钟白等；
    复用一个 page 之后只剩网络往返，快一个量级。

    **崩了要能自愈**：`self._page` 在浏览器进程死掉之后还留着一个野引用，
    只看它非不非空是不够的（第 24 条）。
    """

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._page = None
        self._failures = 0

    def __enter__(self) -> "WereadBrowser":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _ensure(self):
        if self._page:
            return self._page
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self._page = self._browser.new_page(user_agent=CRAWL_UA)
        return self._page

    def close(self) -> None:
        for obj, name in ((self._browser, "browser"), (self._pw, "playwright")):
            try:
                if obj:
                    obj.close() if name == "browser" else obj.stop()
            except Exception:  # noqa: BLE001
                pass
        self._browser = self._page = self._pw = None

    @staticmethod
    def _is_browser_dead(exc: BaseException) -> bool:
        """这个异常是不是"浏览器进程没了"。

        Playwright 对浏览器崩溃/被杀统一报 `Target page, context or browser
        has been closed`，chromium 直接段错误时还会是 `Target crashed`。
        这两种都必须丢掉现有实例重开，否则后面每一次查询都是同一个错。
        """
        msg = str(exc).lower()
        return any(k in msg for k in (
            "has been closed", "target closed", "browser closed",
            "target crashed", "browser has disconnected", "connection closed",
        ))

    def search(self, title: str) -> list[dict]:
        page = self._ensure()
        # 用简体去搜：微信读书索引的是简体书名，繁体书名（紐約時報、狼廳）搜不出来
        url = SEARCH_URL.format(kw=requests.utils.quote(to_simplified(title)))
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(1800)          # 等列表渲染完
            self._failures = 0
            return _parse_cards(page.content())
        except Exception as e:  # noqa: BLE001 网络/超时/页面异常都算这次失败
            self._failures += 1
            if self._is_browser_dead(e):
                # 清空引用，下次 _ensure() 会重新拉起；不清就一直用死的实例
                log.warning("浏览器实例已失效，丢弃后重开（第 %d 次连续失败）：%s",
                            self._failures, type(e).__name__)
                self.close()
            log.warning("微信读书搜索失败（《%s》）：%s", title, e)
            log_failed_url(url, f"微信读书：{type(e).__name__}: {e}")
            if self._failures >= _MAX_CONSECUTIVE_FAILURES:
                raise RuntimeError(
                    f"连续 {self._failures} 次查询失败，中止本轮回填"
                    f"（浏览器多半已经废了，再跑下去全是白发请求）") from e
            return []


def find_cover(title: str, browser: WereadBrowser | None = None) -> dict | None:
    """按书名找封面。返回卡片 dict（含 cover_url / author / recommend），找不到返回 None。"""
    own = browser is None
    b = browser or WereadBrowser()
    try:
        cards = b.search(title)
        # 按可信度排序：精确匹配优先于"带版本后缀"，避免同名网文抢走封面
        scored = sorted(
            ((match_score(title, c["title"]), c) for c in cards),
            key=lambda x: -x[0],
        )
        for score, card in scored:
            if score >= 2:
                return card
        if cards:
            log.debug("《%s》有 %d 条结果但书名对不上，跳过：%s",
                      title, len(cards), "、".join(c["title"][:16] for c in cards[:3]))
        return None
    finally:
        if own:
            b.close()


def backfill_scores(limit: int = 0, sources: tuple = ()) -> tuple[int, int]:
    """给库里没评分的书回微信读书查**推荐值**。返回 (补到的本数, 尝试的本数)。

    为什么拿推荐值当评分：公开榜单页基本不给结构化评分，库里 198 本只有 16 本
    有真实评分，剩下的导出时只能填均值占位。微信读书的推荐值（94.1%）是最容易
    拿到的真实评分，`_parse_cards` 里已经换算成 10 分制。

    **顺带把封面也补了**：查一次拿到的是整张卡片，封面和推荐值都在上面。
    分两个函数各查一遍就是双倍请求量（198 本 → 396 次，还更容易被限流），
    所以这里一次办两件事——缺哪样补哪样，已有的都不覆盖。
    """
    from store import rows_without_scores, update_metadata

    rows = rows_without_scores(limit, sources)
    log.info("有 %d 本书没有评分，开始回微信读书查推荐值", len(rows))

    hit = cover_hit = 0
    with WereadBrowser() as b:
        for i, row in enumerate(rows, 1):
            polite_sleep()
            try:
                card = find_cover(row["title"], b)
            except RuntimeError as e:      # 熔断，见 WereadBrowser.search
                log.error("回填中止：%s；还剩 %d 本没查", e, len(rows) - i + 1)
                break
            if not card:
                continue
            if card.get("recommend"):
                update_metadata(row["id"], score=round(card["recommend"], 1))
                hit += 1
            if card.get("cover_url") and not (row["cover_url"] or "").strip():
                update_metadata(row["id"], cover_url=card["cover_url"],
                                source_site="微信读书（封面回填）")
                cover_hit += 1
            if i % 20 == 0 or i == len(rows):
                log.info("微信读书回填进度 %d/%d：评分 %d，封面 %d",
                         i, len(rows), hit, cover_hit)
    log.info("微信读书回填完成：评分 %d/%d，封面顺带补了 %d 本", hit, len(rows), cover_hit)
    return hit, len(rows)


def backfill_covers(limit: int = 0, sources: tuple = ()) -> tuple[int, int]:
    """给库里没封面的书回微信读书查封面。返回 (补到的本数, 尝试的本数)。"""
    import random
    import time

    from store import rows_without_covers, update_metadata

    rows = rows_without_covers(limit, sources)
    log.info("有 %d 本书没有封面，开始回微信读书查", len(rows))

    hit = 0
    with WereadBrowser() as b:
        for i, row in enumerate(rows, 1):
            polite_sleep()
            try:
                card = find_cover(row["title"], b)
            except RuntimeError as e:      # 熔断，见 WereadBrowser.search
                log.error("回填中止：%s；还剩 %d 本没查", e, len(rows) - i + 1)
                break
            if card and card.get("cover_url"):
                update_metadata(row["id"], cover_url=card["cover_url"],
                                source_site="微信读书（封面回填）")
                hit += 1
            if i % 20 == 0 or i == len(rows):
                log.info("微信读书封面回填进度 %d/%d，成功 %d", i, len(rows), hit)
    log.info("微信读书封面回填完成：%d/%d", hit, len(rows))
    return hit, len(rows)


def main(argv: list) -> None:
    """命令行：scores/backfill [本数] [--source 起点中文网 红袖添香 ...]

    --source 用来圈定只查哪些来源的书。不加就按 id 顺序全查 —— 那会把我们自己采的
    191 本排在最前面，它们已经查过一遍且注定查不到（fixlog 第 23 条）。
    """
    cmd = argv[0] if argv else ""
    rest = argv[1:]

    sources: tuple = ()
    if "--source" in rest:
        i = rest.index("--source")
        sources = tuple(a for a in rest[i + 1:] if not a.isdigit())
        rest = rest[:i]

    n = int(rest[0]) if rest and rest[0].isdigit() else 0

    if cmd == "backfill":
        print("补到封面：", backfill_covers(n, sources))
    elif cmd == "scores":
        print("补到评分：", backfill_scores(n, sources))
    else:
        for t in argv or ["海底两万里", "雪山飞狐", "陆小凤"]:
            c = find_cover(t)
            print(f"{t}: {c['cover_url'][:70] if c else '（没找到）'}")


if __name__ == "__main__":
    import sys

    main(sys.argv[1:])

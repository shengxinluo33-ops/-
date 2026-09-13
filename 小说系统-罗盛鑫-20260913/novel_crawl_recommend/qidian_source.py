"""起点中文网数据源（**只采元数据，不碰正文**）。

为什么只走移动端 `m.qidian.com`：
`www.qidian.com` 和 `book.qidian.com` 直连一律返回 202 —— 那是反爬风控页，
不是正常内容。移动端页面返回 200 且带完整的 og 元信息。**这只是绕开风控，
不是绕开授权**：我们取的仍然是页面上本来就公开的书名、作者、分类、简介、封面
这些出版信息，**不抓任何章节正文**（连载小说受版权保护，批量下载正文是侵权）。

流程：

    m.qidian.com/rank|free|finish   →  榜单页解出 (book_id, 书名)
        ↓  逐本：睡 1~3 秒
    m.qidian.com/book/{id}          →  og:title / og:image / og:novel:author
                                       og:novel:category / og:description
    m.qidian.com/search?kw={书名}    →  按书名反查 book_id（给已有书补封面用）

抓不到的一律记进 logs/failed_urls.log 后跳过，不重试硬闯、不并发。

**关于 403**：一次性跑大批量采集（几百次请求）之后，起点会对来源 IP 返回
403（整站，包括详情页和搜索）。2026-09-02 实测就是这么被限的：
先搜索接口 403，接着榜单页也 403。

碰到 403 的正确做法是**停手等一段时间**（小时级），绝对不要加重试逻辑去硬闯 ——
那只会把封禁时间拉长。自测里这条会记成 SKIP 而不是 FAIL，因为那是环境状态不是代码 bug。
代码层面配了熔断：一旦出现 403，`_blocked` 置位，本进程内剩余的请求不再发出，
批量循环直接收手（`blocked()` 可以查状态）。
按书名回填封面是最费请求的一步（每次两跳：搜索 + 详情），
如果只想要起点榜单那 75 本，别跑 backfill。

两个实测结论（2026-09-02）：

- **rank 页的查询参数是摆设。** `?catId=1..25`、`?gender=male|female`
  翻来覆去请求了 50 次，拿到的还是同样 33 本 —— 分类是在前端过滤的，
  服务端返回的都是同一份榜单。所以能采的总量取决于**有几个不同的榜单页**：
  rank / rank?gender=female / free / finish，合起来 ~75 本。
- **想拿更多书只能靠搜索。** `m.qidian.com/search?kw={书名}` 是真实可用的，
  给库里已有的书回填封面走的就是这条路。
"""

from __future__ import annotations

import re

import requests
from bs4 import BeautifulSoup

from config import CRAWL_TIMEOUT, CRAWL_UA, get_logger, log_failed_url
from crawler import polite_sleep
from titlematch import match_score, to_simplified

log = get_logger("qidian")

_blocked = False        # 起点对本 IP 403 了，本进程内不再发请求


def blocked() -> bool:
    """起点是不是已经把我们限流了。批量循环每轮开头查一次。"""
    return _blocked

RANK_URL = "https://m.qidian.com/rank"
# 起点能白拿的书就这么几个榜单页（rank 的 catId/gender 参数无效，见模块 docstring）
RANK_PAGES = [
    RANK_URL,
    f"{RANK_URL}?gender=female",
    "https://m.qidian.com/free",
    "https://m.qidian.com/finish",
]
BOOK_URL = "https://m.qidian.com/book/{book_id}"
SEARCH_URL = "https://m.qidian.com/search?kw={kw}"
COVER_PREFIX = "https:"          # og:image 是 //bookcover.yuewen.com/... 形式

# 起点自有分类 → 本站分类（起点分得很细，"异世大陆""东方玄幻"都归玄幻）
QI_CATEGORY_MAP = {
    "玄幻": "玄幻", "奇幻": "玄幻", "异世大陆": "玄幻", "东方玄幻": "玄幻",
    "武侠": "武侠", "传统武侠": "武侠", "新派武侠": "武侠", "国术武技": "武侠",
    "仙侠": "玄幻", "修真文明": "玄幻", "现代修仙": "玄幻", "古典仙侠": "玄幻",
    "都市": "都市", "都市生活": "都市", "商战职场": "都市",
    "都市异能": "都市", "异术超能": "都市", "娱乐明星": "都市", "校园": "都市",
    "历史": "历史", "架空历史": "历史", "历史传记": "历史",
    "科幻": "科幻", "星际文明": "科幻", "未来世界": "科幻",
    "悬疑": "悬疑", "侦探推理": "悬疑", "灵异鬼怪": "悬疑",
    "言情": "言情", "浪漫青春": "言情", "现代言情": "言情", "古代言情": "言情",
}


def _get(url: str) -> str | None:
    """抓一个页面。403 时置位 _blocked，让调用方的循环立刻收手。

    上一轮（2026-09-02）就是因为缺这个熔断：403 之后剩下的 134 次查询照发不误，
    日志里记了满屏 HTTP 403，一次都没命中，还把封禁时间拖长了。
    """
    global _blocked
    if _blocked:
        return None
    try:
        resp = requests.get(url, headers={"User-Agent": CRAWL_UA}, timeout=CRAWL_TIMEOUT)
        if resp.status_code == 403:
            _blocked = True
            log.error("起点返回 403 —— 本轮采集到此为止，剩下的书下次再补（不重试）")
            log_failed_url(url, "起点 403 限流，本轮后续请求已中止")
            return None
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}（多半是反爬风控）")
        # 起点移动端一律 UTF-8。用 apparent_encoding 会踩坑：chardet 对部分页面
        # 猜成别的编码，解出来整页都是"è¯·å‹¿é«è€ƒ"这种乱码。
        resp.encoding = "utf-8"
        return resp.text
    except Exception as e:  # noqa: BLE001
        log.warning("抓取失败（%s）：%s", url, e)
        log_failed_url(url, f"起点：{type(e).__name__}: {e}")
        return None


def fetch_rank(limit: int = 12) -> list[dict]:
    """从起点移动端的几个榜单页解出若干本书，顺序就是榜单排名。

    逐个榜单页抓，够 limit 了就停。抓完还不够就是真的没有了 —— 起点的
    分类是靠前端过滤的，服务端就这几页。
    """
    seen: set[str] = set()
    books: list[dict] = []
    for page_url in RANK_PAGES:
        if len(books) >= limit or _blocked:
            break
        polite_sleep()
        html = _get(page_url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select("a[href*='/book/']"):
            m = re.search(r"/book/(\d+)", a.get("href") or "")
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            books.append({"book_id": m.group(1),
                          "title": a.get_text(" ", strip=True)})
            if len(books) >= limit:
                break
        log.info("榜单页 %s 累计解出 %d 本", page_url.split("//")[-1][:40], len(books))

    log.info("起点榜单共解出 %d 本：%s", len(books), "、".join(b["title"][:8] for b in books[:6]))
    return books


def _parse_search_results(html: str) -> list[dict]:
    """解出搜索页里的结果条目：book_id + 书名 + 封面（封面可能为空）。

    两种页面长得不一样，都得认：

    - 普通搜索页：条目是 `a[href*='/book/']`，标题在 `title="…在线阅读"` 上。
    - **标签页**：搜热门词（如"斗破苍穹"）时起点会 302 到 `/soushu/斗破苍穹.html`，
      那里列的是同人作品集，条目链接变成 `/chapter/{bid}/0/`，
      用只认 `/book/` 的正则会一条都解不出来（实测 0 条）。

    两种页面的条目里都带着 `img[data-src]` 真封面（第一个 img 的 `src` 是站点占位图，
    不能要）。能直接拿到封面就**不用再请求详情页**，一次查询从两跳变一跳。
    """
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    seen: set[str] = set()
    for a in soup.select("a[href*='/book/'], a[href*='/chapter/']"):
        m = re.search(r"/(?:book|chapter)/(\d+)", a.get("href") or "")
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))

        h2 = a.find("h2")
        title = (a.get("title") or (h2.get_text(" ", strip=True) if h2 else "")
                 or a.get_text(" ", strip=True) or "").strip()
        # 去掉"…在线阅读""…最新章节"这类站点统一尾巴，否则书名永远对不上
        title = re.sub(r"(在线阅读|最新章节|全文阅读|无弹窗).*$", "", title).strip(" -·")
        if not title:
            continue

        cover = ""
        img = a.find("img")
        if img:
            cover = (img.get("data-src") or "").strip()
            if cover.startswith("//"):
                cover = COVER_PREFIX + cover
            # 只认起点的封面 CDN，别把站点占位图/广告图当成封面
            cover = re.sub(r"/\d{2,4}$", "/600", cover) \
                if "bookcover.yuewen.com" in cover else ""
        items.append({"book_id": m.group(1), "title": title, "cover_url": cover})
    return items


def search_book(title: str) -> dict | None:
    """按书名搜起点，返回**书名核对得上**的那一条（book_id / 书名 / 封面）。

    以前这里是"取页面里第一个 /book/{id}"——两个后果：可能给书配上同名网文的
    封面（配错比没有更糟）；以及每条搜索都白搭一次详情页请求，134 本就是 268 次
    请求，上一轮的 403 就是这么堆出来的。现在先按 `titlematch` 的规则挑最像的一条，
    对不上就直接放弃，省掉第二跳。
    """
    if not title:
        return None
    # 用简体去搜：起点索引的是简体书名，拿「紐約時報」这种繁体去搜一条都出不来
    html = _get(SEARCH_URL.format(kw=requests.utils.quote(to_simplified(title))))
    if not html:
        return None

    items = _parse_search_results(html)
    scored = sorted(((match_score(title, it["title"]), it) for it in items),
                    key=lambda x: -x[0])
    for score, it in scored:
        if score >= 2:
            return it
    log.debug("起点搜到 %d 条但书名都对不上《%s》：%s", len(items), title,
              "、".join(it["title"][:16] for it in items[:3]) or "（空结果）")
    return None


def fetch_by_title(title: str) -> dict | None:
    """按书名找元数据（主要用来补封面）。

    搜索结果里自带封面时就直接返回，不再请求详情页 —— 补封面只要封面，
    多抓一次详情页纯属浪费请求额度。没带封面（老页面）才走详情页。
    """
    hit = search_book(title)
    if not hit:
        return None
    if hit.get("cover_url"):
        return {
            "title": hit["title"],
            "cover_url": hit["cover_url"],
            "source_url": BOOK_URL.format(book_id=hit["book_id"]),
            "source_site": "起点中文网（封面回填）",
        }
    polite_sleep()
    return fetch_book(hit["book_id"])


def fetch_book(book_id: str) -> dict | None:
    """抓一本书的详情页，只取元数据。"""
    html = _get(BOOK_URL.format(book_id=book_id))
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    def meta(*keys: str) -> str:
        for key in keys:
            for attrs in ({"property": key}, {"name": key}):
                tag = soup.find("meta", attrs=attrs)
                if tag and (tag.get("content") or "").strip():
                    return tag["content"].strip()
        return ""

    cover = meta("og:image")
    if cover.startswith("//"):
        cover = COVER_PREFIX + cover
    # 封面 URL 末尾的 /180 是缩略图尺寸，换成 /600 拿到大图
    cover = re.sub(r"/\d{2,4}$", "/600", cover)

    site_category = meta("og:novel:category")
    return {
        "title": meta("og:title", "twitter:title"),
        "author": meta("og:novel:author", "author"),
        "site_category": site_category,
        "category": QI_CATEGORY_MAP.get(site_category, ""),
        "description": meta("og:description", "description"),
        "cover_url": cover,
        "source_url": BOOK_URL.format(book_id=book_id),
        "source_site": "起点中文网",
    }


def crawl_qidian(limit: int = 10, category: str = "") -> list[dict]:
    """采起点榜单。category 为空时用起点自己的分类；不为空则只留映射到该分类的书。"""
    books = fetch_rank(limit=limit * 3)      # 多取一些，够筛出 limit 本
    novels: list[dict] = []
    for rank, b in enumerate(books, 1):
        if len(novels) >= limit or _blocked:
            break
        polite_sleep()
        detail = fetch_book(b["book_id"])
        if not detail or not detail["title"]:
            continue
        if category and detail["category"] and detail["category"] != category:
            continue

        detail["rank_num"] = len(novels) + 1
        detail["category"] = detail["category"] or category or "其他"
        detail["score"] = None                # 起点移动端页面不给结构化评分，不强凑
        novels.append(detail)
        log.info("[%d/%d] 《%s》｜%s｜%s｜起点分类 %s",
                 detail["rank_num"], limit, detail["title"],
                 detail["author"] or "（未知）", detail["category"],
                 detail["site_category"] or "—")
    return novels


def backfill_covers(limit: int = 0, sources: tuple = ()) -> tuple[int, int]:
    """给库里没封面的书回起点查封面。返回 (补到的本数, 尝试过的本数)。

    每次查询包含"搜索 + 详情页"两次请求，之间都有 1~3 秒延时。

    sources 限定只查哪些来源，用法同 `weread_source`（fixlog 第 23 条）：
    库里自己采的 191 本 id 最小，按 id 排永远在最前面，而它们已经查过一轮了。
    """
    from store import rows_without_covers, update_metadata

    rows = rows_without_covers(limit, sources)
    log.info("有 %d 本书没有封面，开始回起点查", len(rows))

    hit = 0
    for i, row in enumerate(rows, 1):
        if _blocked:
            log.warning("已触发限流，%d 本还没查，留给下一轮", len(rows) - i + 1)
            break
        polite_sleep()
        detail = fetch_by_title(row["title"])
        if not detail or not detail.get("cover_url"):
            continue
        update_metadata(row["id"], cover_url=detail["cover_url"],
                        site_category=detail.get("site_category", ""),
                        source_site="起点中文网（封面回填）")
        hit += 1
        if i % 10 == 0 or i == len(rows):
            log.info("封面回填进度 %d/%d，成功 %d", i, len(rows), hit)
    log.info("封面回填完成：%d/%d", hit, len(rows))
    return hit, len(rows)


if __name__ == "__main__":
    import json
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    rest = sys.argv[2:]

    sources: tuple = ()
    if "--source" in rest:
        i = rest.index("--source")
        sources = tuple(a for a in rest[i + 1:] if not a.isdigit())
        rest = rest[:i]

    n = int(rest[0]) if rest and rest[0].isdigit() else 0

    if cmd == "backfill":
        print("补到封面：", backfill_covers(n, sources))
    else:
        n = int(cmd) if cmd.isdigit() else 5
        cat = sys.argv[2] if len(sys.argv) > 2 else ""
        print(json.dumps(crawl_qidian(n, cat), ensure_ascii=False, indent=2))

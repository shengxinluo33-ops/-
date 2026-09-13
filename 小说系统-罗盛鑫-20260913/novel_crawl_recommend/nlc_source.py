"""中国国家图书馆（nlc.cn）来源：**只补一条权威书目链接，不做结构化抓取。**

先说清楚这件事的边界，免得以为这块"爬成功了"：

- 国图的馆藏检索系统 `opac.nlc.cn` 和读者云门户 `read.nlc.cn`
  在当前网络环境**不可达**（TLS 连不上，HTTP 000）；
- 能访问的只有官网 `www.nlc.cn`（资讯门户，不是书目库）；
- 官网不提供按书名的开放检索接口，所以**拿不到单本书的权威书目页**。

因此这个模块做的是：用 Tavily 搜 `site:nlc.cn {书名}`，
**只有在 nlc.cn 域名下真的搜到结果时**才把那条链接记为 `catalog_url`；
搜不到就留空。**不拼 URL、不伪造书目页** —— 猜一个检索参数拼出来的链接
点进去是 404，比留空更糟。

真正的"权威元数据"还是靠起点（见 qidian_source.py）和榜单页解析（crawler.py）。
国图这条链接的作用只是给每本书挂一个可核对的权威出处。
"""

from __future__ import annotations

from urllib.parse import urlparse

from config import get_logger
from crawler import polite_sleep, search_one

log = get_logger("nlc")

NLC_HOST_SUFFIX = "nlc.cn"


def _is_nlc(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == NLC_HOST_SUFFIX or host.endswith("." + NLC_HOST_SUFFIX)


def find_catalog_url(title: str) -> str:
    """搜这本书在 nlc.cn 上的公开页面，搜到就返回 URL，搜不到返回空串。

    注意这个链接的**精度**：国图官网没有按书名的开放检索接口，所以我们拿到的是
    "搜索 site:nlc.cn {书名} 命中的 nlc.cn 页面"，多数情况下是检索入口或相关页面，
    **不是这本书的精确馆藏书目页**。所以前端把它标成"国图相关页"，别误导成
    "这本书的馆藏记录"。想要精确馆藏页，得等 opac.nlc.cn 能连上再做结构化对接。
    """
    if not title:
        return ""

    polite_sleep()
    try:
        results = search_one(f"site:{NLC_HOST_SUFFIX} {title}", max_results=3)
    except RuntimeError as e:      # 没配 TAVILY_API_KEY
        log.debug("跳过国图链接（%s）", e)
        return ""

    for r in results:
        url = (r.get("url") or "").strip()
        if url and _is_nlc(url):
            log.info("《%s》找到国图链接：%s", title, url)
            return url

    log.debug("《%s》没有搜到 nlc.cn 的结果，catalog_url 留空", title)
    return ""


def attach_catalog_urls(novels: list[dict]) -> int:
    """给一批小说补 catalog_url，原地改，返回补到的条数。"""
    hit = 0
    for n in novels:
        url = find_catalog_url(n.get("title", ""))
        if url:
            n["catalog_url"] = url
            hit += 1
    log.info("国图链接补到 %d/%d 条", hit, len(novels))
    return hit


if __name__ == "__main__":
    for t in ["天龙八部", "射雕英雄传", "夜无疆"]:
        print(f"{t}: {find_catalog_url(t) or '（没搜到，留空）'}")

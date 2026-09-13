"""【工具3】WebSearch：用 Tavily 搜网页，返回标题、url、摘要。

为什么用 Tavily 而不是自己爬搜索引擎：Tavily 的 API 就是为 LLM 准备的，
直接给清洗过的正文摘要，省掉"抓搜索结果页再解析"这一层脏活。

没有 TAVILY_API_KEY 时返回一条明确的报错字符串，不抛异常、不拖垮其他工具。
"""

from __future__ import annotations

from langchain_core.tools import tool

from config import env, get_logger
from tools.common import fail, ok, truncate

log = get_logger("tool.websearch")


def _client():
    from tavily import TavilyClient

    key = env("TAVILY_API_KEY")
    if not key:
        raise RuntimeError(
            "缺少 TAVILY_API_KEY。去 https://tavily.com 注册拿 Key，"
            "写进 .env 或 export TAVILY_API_KEY=... 后重试。"
        )
    return TavilyClient(api_key=key)


@tool
def web_search(query: str, max_results: int = 5, search_depth: str = "basic") -> str:
    """联网搜索，返回若干条结果的标题、链接和摘要。适合找资料、查事实、找小说/文档出处。

    Args:
        query: 搜索关键词，中文英文都可以，写得像人话效果更好
        max_results: 返回条数，1 到 10，默认 5
        search_depth: basic=快而省；advanced=慢但召回更全，适合一次要找齐的资料
    """
    query = (query or "").strip()
    if not query:
        return fail("WebSearch", "query 不能为空")
    max_results = max(1, min(int(max_results or 5), 10))
    depth = (search_depth or "basic").strip().lower()
    if depth not in ("basic", "advanced"):
        depth = "basic"

    log.info("web_search q=%r depth=%s k=%d", query, depth, max_results)
    try:
        resp = _client().search(
            query=query, max_results=max_results, search_depth=depth, include_answer=False
        )
    except Exception as e:  # noqa: BLE001 网络、鉴权、额度都可能炸
        log.warning("web_search 失败：%s", e)
        return fail("WebSearch", e)

    items = resp.get("results") or []
    if not items:
        return ok(f"没有搜到关于「{query}」的结果，换个说法再试。")

    lines = [f"共 {len(items)} 条结果："]
    for i, r in enumerate(items, 1):
        title = (r.get("title") or "").strip() or "(无标题)"
        url = r.get("url") or ""
        snippet = (r.get("content") or "").strip().replace("\n", " ")
        lines.append(f"\n[{i}] {title}\n链接：{url}\n摘要：{snippet}")
    return ok(truncate("\n".join(lines), label="搜索结果"))


if __name__ == "__main__":
    print(web_search.invoke({"query": "langgraph 工具调用 教程", "max_results": 3}))

"""自测：真实采集一个分类 → 入库建索引 → 两种查询 → 起 Web 服务打接口。

跑法：
    python selftest.py

判定：PASS / SKIP / FAIL，退出码非 0 表示有 FAIL。

注意：这个自测会真的联网（Tavily 搜索 + 抓页面 + 调 embedding），
武侠那一轮带 1~3 秒礼貌延时，整体约 1 分钟。
"""

from __future__ import annotations

import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import json  # noqa: E402

from config import get_logger  # noqa: E402

log = get_logger("selftest")
PASS, SKIP, FAIL = "PASS", "SKIP", "FAIL"


class SkipCase(Exception):
    """源不可用（被限流 / 网络不通）时抛这个，记为 SKIP 而不是 FAIL。

    代码 bug 才该 FAIL；外部站点 403 是环境状态，不该让自测红。
    """


# ---------------------------------------------------------------- 用例

def test_crawl() -> str:
    """采集武侠类小说，检查九个字段都在。"""
    from recommender import crawl_and_index

    novels = crawl_and_index("武侠", limit=5)
    assert novels, "一本都没采到，检查 TAVILY_API_KEY 和网络"

    need = {"title", "author", "category", "score", "rank_num",
            "description", "source_url"}
    for n in novels:
        missing = need - set(n)
        assert not missing, f"《{n.get('title')}》缺字段：{missing}"
        assert n["title"].strip(), "书名为空"
        assert n["description"].strip(), f"《{n['title']}》简介为空"

    ranks = [n["rank_num"] for n in novels]
    assert ranks == list(range(1, len(novels) + 1)), f"rank_num 不连续：{ranks}"
    return f"采到 {len(novels)} 本：{'、'.join(n['title'] for n in novels[:5])}"


def test_keyword_query() -> str:
    """模式①：分类检索，按评分/排名排序都要能出结果。"""
    from recommender import recommend_by_category, recommend_by_keyword
    from store import count

    assert count() > 0, "数据库是空的，先跑采集"

    rows = recommend_by_category("武侠", sort_by="rank")
    assert rows, "按分类查武侠查不到东西"
    assert all(r["category"] == "武侠" for r in rows), "混进了别的分类"
    ranks = [r["rank_num"] for r in rows if r["rank_num"]]
    assert ranks == sorted(ranks), f"按排名排序没生效：{ranks}"

    first = rows[0]["title"]
    hit = recommend_by_keyword(first[:2])
    assert hit, f"按书名关键词「{first[:2]}」搜不到《{first}》"
    return f"分类检索 {len(rows)} 本，关键词检索命中 {len(hit)} 本"


def test_semantic_query() -> str:
    """模式②：语义检索，相似度要从高到低排。"""
    from recommender import recommend_by_semantics
    from store import index_info

    info = index_info()
    assert info["exists"], "向量索引不存在"
    assert info["count"] > 0, "向量索引是空的"

    rows = recommend_by_semantics("江湖恩怨 武林高手 刀光剑影", top_k=3)
    assert rows, "语义检索没返回任何东西"
    sims = [r["similarity"] for r in rows]
    assert sims == sorted(sims, reverse=True), f"相似度没有降序：{sims}"
    return f"语义检索返回 {len(rows)} 本，相似度 {sims[0]:.3f}～{sims[-1]:.3f}（{info['embedder']}）"


def test_qidian_source() -> str:
    """起点榜单采集：只采元数据，封面/作者/来源站点要在，且不能有正文字段。"""
    from qidian_source import crawl_qidian

    novels = crawl_qidian(limit=3)
    if not novels:
        # 起点被限流时整站返回 403，这是环境状态不是代码问题，按 SKIP 处理
        raise SkipCase("起点站点不可访问（多半是 403 限流），详见 logs/failed_urls.log")

    for n in novels:
        assert n.get("title"), "书名为空"
        assert n.get("cover_url", "").startswith("http"), f"《{n['title']}》没拿到封面"
        assert n.get("source_url", "").startswith("https://m.qidian.com/"), "来源链接不是起点"
        assert n.get("source_site") == "起点中文网", "来源站点标记缺失"
        # 合规底线：只存元数据，绝不能混进章节正文
        assert "content" not in n and "chapter" not in n, "记录里出现了正文字段"

    with_cover = sum(1 for n in novels if n["cover_url"])
    return f"起点采到 {len(novels)} 本，{with_cover} 本有封面，均无正文字段"


def test_nlc_source() -> str:
    """国图链接：尽力而为，搜到才算，搜不到留空（不强求，也不伪造）。"""
    from nlc_source import find_catalog_url

    url = find_catalog_url("天龙八部")
    if not url:
        return "国图没搜到（nlc.cn 结果为空），catalog_url 按设计留空"
    assert "nlc.cn" in url, f"链接不是 nlc.cn 域下的：{url}"
    return f"国图链接可用：{url[:60]}"


def test_web() -> str:
    """起 Flask（5001），打三个接口，确认页面和 JSON 都正常。"""
    import app as web

    port = 5001
    server = threading.Thread(
        target=lambda: web.app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    server.start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(40):                      # 等服务起来，最多等 8 秒
        try:
            urllib.request.urlopen(f"{base}/api/status", timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    else:
        raise AssertionError("Flask 没起来")

    def get(path: str) -> dict:
        with urllib.request.urlopen(f"{base}{path}", timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    page = urllib.request.urlopen(base, timeout=5).read().decode("utf-8")
    assert "小说检索推荐系统" in page, "首页内容不对"

    st = get("/api/status")
    assert st["ok"] and st["novels"] > 0, f"状态接口异常：{st}"

    kw = get("/api/query?q=%E6%AD%A6%E4%BE%A0&mode=keyword&sort=rank")
    assert kw["ok"] and kw["count"] > 0, f"关键词接口异常：{kw}"

    sm = get("/api/query?q=%E6%B1%9F%E6%B9%96&mode=semantic&limit=3")
    assert sm["ok"] and sm["count"] > 0, f"语义接口异常：{sm}"

    return f"首页 + /api/status + 关键词({kw['count']}条) + 语义({sm['count']}条) 均正常"


CASES = [
    ("采集武侠类小说", test_crawl),
    ("关键词/分类查询", test_keyword_query),
    ("语义查询", test_semantic_query),
    ("起点榜单采集", test_qidian_source),
    ("国图权威链接", test_nlc_source),
    ("Web 服务 5001", test_web),
]


# ---------------------------------------------------------------- 主流程

def main() -> int:
    from config import env

    if not env("TAVILY_API_KEY"):
        print("缺少 TAVILY_API_KEY，无法自测采集环节。")
        return 1

    results = []
    for name, fn in CASES:
        try:
            status, detail = PASS, fn()
        except SkipCase as e:
            status, detail = SKIP, str(e)
        except AssertionError as e:
            status, detail = FAIL, str(e)
        except Exception as e:  # noqa: BLE001
            status, detail = FAIL, f"{type(e).__name__}: {e}"
        results.append((status, name, detail))
        print(f"{status} | {name} | {detail}")
        log.info("%s %s %s", status, name, detail)

    failed = [r for r in results if r[0] == FAIL]
    skipped = [r for r in results if r[0] == SKIP]
    print(f"\n合计 {len(results)} 项：通过 {len(results) - len(failed) - len(skipped)}，"
          f"跳过 {len(skipped)}，失败 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

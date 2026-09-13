"""推荐层：两种查询模式（任务书第 3 条）。

模式① 分类/关键词检索
    输入「武侠」→ 从 SQLite 按 category 筛，按评分或榜单排名排序。
    这是"我知道想看哪一类"的用法，快且准。

模式② 语义检索
    输入「女主重生复仇古言」→ 走 FAISS 匹配简介，返回语义最像的几本。
    这是"我说不清分类，但说得出想看什么"的用法。

两种模式都返回 dict 列表，直接喂给前端表格。
"""

from __future__ import annotations

import sqlite3

from config import get_logger
from store import build_index, get_by_ids, keyword_query, semantic_search

log = get_logger("recommender")

# 采集用的预设分类（前端"采分类"按钮的下拉框）。
#
# **这不是库里的分类全集。** 合并进来的那批数据带了 54 个分类
# （轻小说、短篇、仙侠、古代言情、科幻空间…），库的分类要动态查
# `store.category_counts()`，写死在这儿的列表只用于"要采集哪个分类"的提示。
CATEGORIES = ["武侠", "言情", "科幻", "悬疑", "历史", "玄幻", "都市"]


def _row_to_dict(row: sqlite3.Row, similarity: float | None = None) -> dict:
    d = {k: row[k] for k in row.keys()}
    d["score"] = row["score"] if row["score"] is not None else None
    if similarity is not None:
        d["similarity"] = round(similarity, 4)
    return d


def recommend_by_category(category: str, sort_by: str = "score",
                          limit: int = 20, offset: int = 0) -> list[dict]:
    """模式①：按分类筛，sort_by 可选 score（评分降序）或 rank（排名升序）。"""
    rows = keyword_query(category=category, sort_by=sort_by, limit=limit,
                         offset=offset)
    return [_row_to_dict(r) for r in rows]


def recommend_by_keyword(keyword: str, limit: int = 20,
                         offset: int = 0) -> list[dict]:
    """模式①的补充：按书名/作者/简介里的关键词模糊搜。"""
    rows = keyword_query(keyword=keyword, sort_by="score", limit=limit,
                         offset=offset)
    return [_row_to_dict(r) for r in rows]


def recommend_by_category_kw(category: str, keyword: str = "",
                             sort_by: str = "score", limit: int = 20,
                             offset: int = 0) -> list[dict]:
    """分类 + 关键词一起筛。agent 的主路径用这个。

    `keyword` 留空就是纯分类筛（agent 关键词检索 0 条时退到这里）。
    两个条件在 `_match_where` 里是 AND，所以关键词被分类收窄，
    不会像单关键词那样命中一堆无关书。
    """
    rows = keyword_query(category=category, keyword=keyword, sort_by=sort_by,
                         limit=limit, offset=offset)
    return [_row_to_dict(r) for r in rows]


def recommend_all(sort_by: str = "score", limit: int = 20,
                  offset: int = 0) -> list[dict]:
    """不筛选，列出全部——导航栏「全部」那一栏走这个。

    不传 category 也不传 keyword，`_match_where` 就拼不出 WHERE 子句，
    于是返回全表。同一个函数复用，不用为"全部"单独写一条 SQL。
    """
    rows = keyword_query(sort_by=sort_by, limit=limit, offset=offset)
    return [_row_to_dict(r) for r in rows]


def recommend_by_semantics(query: str, top_k: int = 5) -> list[dict]:
    """模式②：语义匹配简介。相似度高的排前面。"""
    try:
        hits = semantic_search(query, top_k=top_k)
    except RuntimeError:
        # 索引没建过，现建一次再试，省得让用户手动跑命令
        build_index()
        hits = semantic_search(query, top_k=top_k)

    rows = {r["id"]: r for r in get_by_ids([i for i, _ in hits])}
    out = []
    for novel_id, sim in hits:
        row = rows.get(novel_id)
        if row:
            out.append(_row_to_dict(row, similarity=sim))
    return out


def crawl_and_index(category: str, limit: int = 8) -> list[dict]:
    """一条龙：采集 → 入库 → 重建向量索引。前端"采集"按钮走这个。"""
    from crawler import crawl_category

    novels = crawl_category(category, limit=limit)
    if novels:
        from store import save_novels

        save_novels(novels)
        build_index()
    return novels


def crawl_qidian_and_index(limit: int = 10, category: str = "",
                           with_nlc: bool = True) -> list[dict]:
    """起点榜单 → 入库 → 补国图链接 → 重建索引。

    只采元数据（书名/作者/分类/简介/封面），**不碰章节正文** —— 起点上的
    连载小说受版权保护，批量下载正文是侵权。详见 qidian_source.py 的说明。
    """
    from qidian_source import crawl_qidian

    novels = crawl_qidian(limit=limit, category=category)
    if not novels:
        return []

    if with_nlc:
        try:
            from nlc_source import attach_catalog_urls

            attach_catalog_urls(novels)
        except Exception as e:  # noqa: BLE001 国图补不上不影响主流程
            log.warning("国图链接补充失败，跳过：%s", e)

    from store import save_novels

    save_novels(novels)
    build_index()
    return novels


if __name__ == "__main__":
    import json
    import sys

    mode = sys.argv[1] if len(sys.argv) > 1 else "keyword"
    q = sys.argv[2] if len(sys.argv) > 2 else "武侠"
    if mode == "semantic":
        print(json.dumps(recommend_by_semantics(q), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(recommend_by_category(q), ensure_ascii=False, indent=2))

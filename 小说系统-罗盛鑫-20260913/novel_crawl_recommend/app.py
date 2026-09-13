"""Web 前端：端口 5001，输入类型或描述，返回榜单表格。

    python app.py               # 启动，浏览器开 http://127.0.0.1:5001
    python app.py --port 8080   # 换端口

默认监听 0.0.0.0（不是 127.0.0.1）：要让 CloudStudio 的端口转发、以及
局域网内别的电脑能连进来，只绑回环地址的话外部一律连不上。

接口：
    GET  /api/query?mode=all|keyword|semantic&q=...&sort=score|rank&limit=20
         mode=all 是导航栏的「全部」：不筛选，q 留空，列出全库（分页）
    GET  /api/agent?q=我想看无脑爽文男主厉害    大白话推荐，见 intent.py
         mode=all 是导航栏的「全部」：不筛选，q 留空，列出全库（分页）
    POST /api/crawl  {"category": "武侠", "limit": 8}   采集并重建索引
    GET  /api/status                                    库里多少条、索引什么模型
"""

from __future__ import annotations

import argparse

from flask import Flask, jsonify, request, send_from_directory

from config import ROOT, get_logger
from intent import recommend_by_agent
from recommender import recommend_all, recommend_by_category, recommend_by_keyword, \
    recommend_by_semantics
from store import category_counts, count, count_matches, index_info

log = get_logger("app")
app = Flask(__name__, static_folder="static")


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/query")
def api_query():
    q = (request.args.get("q") or "").strip()
    mode = (request.args.get("mode") or "keyword").strip()
    sort_by = (request.args.get("sort") or "score").strip()
    try:
        # 每页最多 60 条。库里八千本，一页装不下，靠 page 翻
        limit = max(1, min(int(request.args.get("limit", 20)), 60))
    except ValueError:
        limit = 20
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    offset = (page - 1) * limit

    # mode=all 是导航栏的「全部」：不筛选，所以 q 本来就该是空的
    if not q and mode != "all":
        return jsonify({"ok": False, "error": "请输入小说类型或描述"}), 400
    log.info("查询 mode=%s q=%r sort=%s page=%d", mode, q, sort_by, page)

    try:
        if mode == "all":
            rows = recommend_all(sort_by=sort_by, limit=limit, offset=offset)
            total = count_matches()
        elif mode == "semantic":
            rows = recommend_by_semantics(q, top_k=limit)
            total = len(rows)
        elif _is_known_category(q):
            rows = recommend_by_category(q, sort_by=sort_by, limit=limit,
                                         offset=offset)
            total = count_matches(category=q)
        else:
            rows = recommend_by_keyword(q, limit=limit, offset=offset)
            total = count_matches(keyword=q)
    except Exception as e:  # noqa: BLE001 页面不能因为一次查询炸掉
        log.warning("查询失败：%s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "mode": mode, "count": len(rows), "rows": rows,
                    "total": total, "page": page,
                    "has_more": offset + len(rows) < total})


@app.get("/api/agent")
def api_agent():
    """大白话进，一堆书出：「我想看无脑爽文男主厉害」。

    跟 /api/query 分开是因为它多一次 LLM 调用（实测 0.35~20 秒），
    失败模式也更多（超时、返回非法 JSON、没配 key），降级逻辑单独一套，
    混进 /api/query 会把那边三种检索模式都搞复杂。
    """
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"ok": False, "error": "说说你想看什么"}), 400
    try:
        limit = max(1, min(int(request.args.get("limit", 40)), 60))
    except ValueError:
        limit = 40
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    offset = (page - 1) * limit

    log.info("agent 推荐 q=%r page=%d", q, page)
    try:
        rows, total, intent = recommend_by_agent(q, limit=limit, offset=offset)
    except Exception as e:  # noqa: BLE001 这里炸了也要给前端一个能渲染的响应
        log.warning("agent 推荐失败：%s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "mode": "agent", "count": len(rows),
                    "rows": rows, "total": total, "page": page,
                    "has_more": offset + len(rows) < total,
                    "intent": intent})


@app.get("/api/categories")
def api_categories():
    """库里实际有哪些分类、各多少本——导航栏靠这个动态生成。

    分类不再是写死的 7 个：合并进来的那批数据带了 54 个分类
    （轻小说、短篇、仙侠、古代言情…），写死的话这些全都点不到。
    """
    try:
        return jsonify({"ok": True, "categories": [
            {"name": c, "count": n} for c, n in category_counts()]})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


def _is_known_category(q: str) -> bool:
    """查询词是不是一个分类名（或者分类名的一部分）。

    "言情"要能命中"古代言情""现代言情""玄幻言情"，所以是包含判断不是等值判断。
    反过来"三体"不是任何分类的一部分，就该走关键词检索去书名里搜。
    """
    return any(q in c for c, _ in category_counts())


@app.post("/api/crawl")
def api_crawl():
    payload = request.get_json(silent=True) or {}
    category = (payload.get("category") or "").strip()
    try:
        limit = max(1, min(int(payload.get("limit", 8)), 10))
    except (TypeError, ValueError):
        limit = 8
    if not category:
        return jsonify({"ok": False, "error": "请先选一个分类"}), 400

    log.info("采集分类「%s」，上限 %d 本", category, limit)
    try:
        from recommender import crawl_and_index

        novels = crawl_and_index(category, limit=limit)
    except Exception as e:  # noqa: BLE001
        log.warning("采集失败：%s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "category": category, "count": len(novels),
                    "rows": novels, "total": count()})


@app.post("/api/crawl_qidian")
def api_crawl_qidian():
    """采起点榜单入库（只取元数据）。category 留空=不限分类。"""
    payload = request.get_json(silent=True) or {}
    category = (payload.get("category") or "").strip()
    try:
        limit = max(1, min(int(payload.get("limit", 10)), 20))
    except (TypeError, ValueError):
        limit = 10

    log.info("采集起点榜单，上限 %d 本，分类 %s", limit, category or "不限")
    try:
        from recommender import crawl_qidian_and_index

        novels = crawl_qidian_and_index(limit=limit, category=category)
    except Exception as e:  # noqa: BLE001
        log.warning("起点采集失败：%s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "category": category or "不限", "count": len(novels),
                    "rows": novels, "total": count()})


@app.get("/api/status")
def api_status():
    try:
        cats = category_counts()
        return jsonify({"ok": True, "novels": count(), "index": index_info(),
                        # 库的分类是动态的（54 个），别拿 CATEGORIES 那个采集用的
                        # 预设列表当全集——那是"要采集哪个分类"的候选，不是库里全部
                        "categories": [c for c, _ in cats],
                        "category_counts": [{"name": c, "count": n} for c, n in cats]})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


def main() -> None:
    parser = argparse.ArgumentParser(description="爬虫驱动小说检索推荐系统")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    log.info("启动 Web 服务：http://%s:%d", args.host, args.port)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()

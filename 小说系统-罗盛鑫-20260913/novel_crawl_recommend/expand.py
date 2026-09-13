"""把库扩到 200 本以上，并尽量给每本补上封面。

    python expand.py                      # 每分类 18 本 + 起点 75 本，最后回填封面
    python expand.py --per-category 25    # 每个分类多采点
    python expand.py --no-backfill        # 跳过封面回填

三步走：

1. **公开榜单页**：7 个分类各采一批（默认 18 本）—— 这是数量的主力来源，
   但这些页面不提供封面。
2. **起点榜单**：rank / rank?gender=female / free / finish 四个页全抓（约 75 本），
   这批自带封面。
3. **封面回填**：给第 1 步那些没封面的书，按书名回起点搜索（`/search?kw=`）
   查封面能补多少补多少。

全程 1~3 秒礼貌延时，所以很慢（半小时量级），建议后台跑：

    nohup python expand.py > logs/expand.out 2>&1 &

每步都有进度日志，失败了也只记日志不中断，重跑是幂等的（按 (title, category) 覆盖更新）。
"""

from __future__ import annotations

import argparse
import time

from config import get_logger
from store import build_index, count, index_info
from recommender import CATEGORIES

log = get_logger("expand")


def step_categories(per_category: int) -> None:
    from recommender import crawl_and_index

    for i, cat in enumerate(CATEGORIES, 1):
        t0 = time.time()
        try:
            novels = crawl_and_index(cat, limit=per_category)
            log.info("[%d/%d] 分类「%s」采到 %d 本（%.0fs），库里共 %d 本",
                     i, len(CATEGORIES), cat, len(novels),
                     time.time() - t0, count())
        except Exception as e:  # noqa: BLE001 一个分类挂了不影响其他分类
            log.warning("分类「%s」采集失败：%s", cat, e)
        print(f"CATEGORY {cat}: 库里共 {count()} 本", flush=True)


def step_qidian(limit: int) -> None:
    from recommender import crawl_qidian_and_index

    t0 = time.time()
    try:
        novels = crawl_qidian_and_index(limit=limit)
        log.info("起点采到 %d 本（%.0fs），库里共 %d 本",
                 len(novels), time.time() - t0, count())
    except Exception as e:  # noqa: BLE001
        log.warning("起点采集失败：%s", e)
    print(f"QIDIAN: 库里共 {count()} 本", flush=True)


def step_backfill() -> None:
    from qidian_source import backfill_covers

    t0 = time.time()
    try:
        hit, total = backfill_covers()
        log.info("封面回填 %d/%d（%.0fs）", hit, total, time.time() - t0)
    except Exception as e:  # noqa: BLE001
        log.warning("封面回填失败：%s", e)
    print("BACKFILL DONE", flush=True)


def stats() -> dict:
    from store import connect

    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM novels").fetchone()[0]
        with_cover = conn.execute(
            "SELECT COUNT(*) FROM novels WHERE COALESCE(cover_url,'') <> ''").fetchone()[0]
        by_cat = conn.execute(
            "SELECT category, COUNT(*) c FROM novels GROUP BY category ORDER BY c DESC"
        ).fetchall()
    return {"total": total, "with_cover": with_cover,
            "cover_rate": f"{with_cover / total * 100:.0f}%" if total else "0%",
            "by_category": {r["category"]: r["c"] for r in by_cat}}


def main() -> None:
    ap = argparse.ArgumentParser(description="扩库到 200 本以上并补封面")
    ap.add_argument("--per-category", type=int, default=18, help="每个分类采几本")
    ap.add_argument("--qidian", type=int, default=75, help="起点采几本")
    ap.add_argument("--no-backfill", action="store_true", help="跳过封面回填")
    args = ap.parse_args()

    log.info("开始扩库：每分类 %d 本，起点 %d 本，回填封面 %s",
             args.per_category, args.qidian, not args.no_backfill)

    step_categories(args.per_category)
    step_qidian(args.qidian)
    build_index()
    if not args.no_backfill:
        step_backfill()
        build_index()

    s = stats()
    print("FINAL", s, index_info(), flush=True)


if __name__ == "__main__":
    main()

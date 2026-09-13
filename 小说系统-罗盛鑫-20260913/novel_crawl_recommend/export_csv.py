"""把库里的小说导出成 novels.csv，交给前端伙伴。

格式严格按 `KUST/ch2-rag/notebooks/database.txt`：

    novels.csv | UTF-8 | 英文逗号 | 第一行英文表头
    title,author,category,rating,description,url

**两处脏数据的处理都在导出时做，不改数据库**——DB 里存的是抓到什么就是什么，
CSV 是交付给别人用的，得能直接用：

1. **噪声条目**：媒体名、杂志名、作者名这些不是书的东西（`crawler.is_noise_title`）。
   删库要加 `--clean`，默认只扫描不删，先把清单打出来看。
2. **rating 缺失**：库里 91% 的书没抓到评分，按**同分类均值**填占位值。
   均值只采纳 **>= 5 分**的真实评分——《名剑风流》那个 1.0 是页面上的无关数字
   被正则抓来的，拿它算均值会让整个武侠分类都被拉到 1.0。

用法：

    python export_csv.py                 # 只扫描噪声，不动数据，顺便写 CSV
    python export_csv.py --clean         # 先删噪声再导出（删完自动重建向量索引）
    python export_csv.py --out /tmp/x.csv
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

from config import ROOT, get_logger
from crawler import is_noise_title, noise_reason

log = get_logger("export_csv")

# database.txt 里的字段长度上限，超了就截断，别让前端那边存不进去
MAX_LEN = {"title": 100, "author": 50, "category": 20, "description": 200, "url": 500}

# 低于这个分值的评分不参与均值计算：小说评分掉到 5 分以下基本都是抓错了
MIN_TRUSTED_SCORE = 5.0


def scan_noise(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """扫出会被判成噪声的条目。"""
    rows = conn.execute("SELECT * FROM novels ORDER BY id").fetchall()
    return [r for r in rows if is_noise_title(r["title"])]


def clean_noise(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> None:
    """删掉噪声条目。**删完必须重建向量索引**——FAISS 里存的是 novels.id，
    行没了索引还在，语义检索就会张冠李戴。"""
    ids = [r["id"] for r in rows]
    conn.executemany("DELETE FROM novels WHERE id = ?", [(i,) for i in ids])
    conn.commit()
    log.info("已删除 %d 条噪声记录", len(ids))

    from store import build_index

    n = build_index()
    log.info("向量索引已重建：%d 条", n)


def category_means(conn: sqlite3.Connection) -> tuple[dict[str, float], float]:
    """各分类的真实评分均值，以及全局均值。

    只采纳 >= MIN_TRUSTED_SCORE 的评分。一个分类里没有可信评分时，
    导出会退到全局均值（都市/科幻/其他这三个分类当前一本真实评分都没有，
    只能这么办）。
    """
    rows = conn.execute(
        "SELECT category, score FROM novels "
        "WHERE score IS NOT NULL AND score >= ?", (MIN_TRUSTED_SCORE,)).fetchall()
    if not rows:
        return {}, 0.0

    buckets: dict[str, list[float]] = {}
    for r in rows:
        buckets.setdefault(r["category"], []).append(r["score"])
    means = {c: round(sum(v) / len(v), 1) for c, v in buckets.items()}

    all_scores = [r["score"] for r in rows]
    return means, round(sum(all_scores) / len(all_scores), 1)


def _clip(value: str, key: str) -> str:
    text = (value or "").strip()
    return text[:MAX_LEN[key]]


def export(conn: sqlite3.Connection, out_path: Path) -> dict:
    means, global_mean = category_means(conn)
    # 按 id 排而不是 rank_num：rank_num 是**分类内**的榜单排名，跨分类混在一个
    # 文件里时没有意义（前几名会是各分类的 rank 1 混在一起），而且重采一次就会变，
    # 导致两次导出的文件 diff 出一堆无关的行。按入库顺序排，导出结果可复现。
    rows = conn.execute("SELECT * FROM novels ORDER BY id").fetchall()

    real = filled = 0
    with out_path.open("w", encoding="utf-8", newline="") as f:
        # 简介里有逗号、引号、换行，必须走 csv 模块转义，手写 join 会把列撑坏
        writer = csv.writer(f)
        writer.writerow(["title", "author", "category", "rating", "description", "url"])
        for r in rows:
            if r["score"] is not None:
                rating = round(float(r["score"]), 1)
                real += 1
            else:
                rating = means.get(r["category"], global_mean)
                filled += 1
            writer.writerow([
                _clip(r["title"], "title"),
                _clip(r["author"], "author"),
                _clip(r["category"], "category"),
                f"{rating:.1f}",
                _clip(r["description"], "description"),
                _clip(r["source_url"], "url"),
            ])
    return {"total": len(rows), "real": real, "filled": filled,
            "means": means, "global_mean": global_mean}


def main() -> int:
    ap = argparse.ArgumentParser(description="导出 novels.csv")
    ap.add_argument("--clean", action="store_true",
                    help="先删掉噪声条目再导出（默认只扫描不删）")
    ap.add_argument("--out", default=str(ROOT / "novels.csv"), help="输出路径")
    a = ap.parse_args()

    from store import connect, init_db

    init_db()
    conn = connect()
    conn.row_factory = sqlite3.Row

    noise = scan_noise(conn)
    if noise:
        print(f"\n扫到 {len(noise)} 条噪声记录：")
        for r in noise:
            print(f"  id={r['id']:3d} 《{r['title']}》 分类={r['category']} "
                  f"作者={r['author'] or '—'} 封面={'有' if r['cover_url'] else '无'} "
                  f"← {noise_reason(r['title'])}")
        if a.clean:
            clean_noise(conn, noise)
            print(f"→ 已删除 {len(noise)} 条")
        else:
            print("（没加 --clean，这些都会原样留在 CSV 里）")

    out = Path(a.out)
    stat = export(conn, out)

    print(f"\n已导出 {out}：{stat['total']} 本")
    print(f"  真实评分 {stat['real']} 本，按分类均值填充 {stat['filled']} 本")
    print(f"  各分类均值（只算 >={MIN_TRUSTED_SCORE} 分的真实评分）：")
    for c, m in sorted(stat["means"].items()):
        print(f"    {c:4s} {m}")
    print(f"    无真实评分的分类 → 全局均值 {stat['global_mean']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

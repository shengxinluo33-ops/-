"""把协作库那批同组采集的 CSV 合并进主库（`novels (1).csv`，7970 本起点/红袖/豆瓣/小说阅读网的数据）。

这批是负责数据库的同学爬完打包上传到 GitHub 协作库的，和主库里榜单页那路采集是同一件事的两条落地路径，
所以合并后要整体去重。和 `export_csv.py` 是反方向：那个把库导出成 CSV，这个把 CSV 合进库。
格式同样是 `database.txt` 那套：`title,author,category,rating,description,url`。

**合并语义**（靠 `store.save_novels` 的 `ON CONFLICT` 实现，不去重、不删东西）：

- 按 `(title, category)` 判重，重复采集是**更新**不是堆积。
- **rating = 0.0 一律当缺失（NULL）**。这批数据 7777 本的评分都是 0.0，
  那是"没抓到"而不是"打了 0 分"——真当 0 分存进去，会把现有的 118 本真实评分
  淹没在一片 0.0 里，按评分排序就彻底失效了。
- 已有封面/评分不会被空值盖掉（`COALESCE(NULLIF(excluded...,''), novels...)`），
  所以现有那 165 张封面和 118 个真实评分不会丢。
- 简介取更长的那条。

用法：

    python import_csv.py                       # dry-run，只统计不写库
    python import_csv.py --apply               # 真写库
    python import_csv.py --apply --reindex     # 写完顺带重建向量索引（8000 条，较慢）
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from config import ROOT, get_logger

log = get_logger("import_csv")

DEFAULT_SRC = ROOT / "novels (1).csv"

# rating 里当作"没抓到"的值。0.0 必须在这里——这批数据 97.6% 都是它。
NULL_RATINGS = {"0.0", "0", "0.00", ""}

# 从来源 URL 认站点，写进 source_site 便于溯源。
# 这批数据混了四个站，不标出来以后分不清哪条是哪来的。
SITE_BY_HOST = {
    "m.qidian.com": "起点中文网",
    "www.qidian.com": "起点中文网",
    "book.qidian.com": "起点中文网",
    "www.hongxiu.com": "红袖添香",
    "book.douban.com": "豆瓣读书",
    "www.xxsy.net": "小说阅读网",
}

MAX_LEN = {"title": 100, "author": 50, "category": 20, "url": 500}


def _site_of(url: str) -> str:
    host = url.split("//", 1)[-1].split("/", 1)[0]
    return SITE_BY_HOST.get(host, host)


def _rating(raw: str) -> float | None:
    """0.0 这类占位值当缺失，其余转 float。"""
    raw = (raw or "").strip()
    if raw in NULL_RATINGS:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if 0 < value <= 10 else None


def load_rows(path: Path) -> list[dict]:
    """读 CSV 并转成 novels 表的字段。"""
    # utf-8-sig：带 BOM 的 UTF-8 也能读（Excel 导出的常见情况）
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = {"title", "author", "category", "rating", "description", "url"} - set(
            reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV 缺少列：{sorted(missing)}（实际列名 {reader.fieldnames}）")

        rows = []
        for r in reader:
            title = (r["title"] or "").strip()
            if not title:
                continue
            url = (r["url"] or "").strip()[:MAX_LEN["url"]]
            rows.append({
                "title": title[:MAX_LEN["title"]],
                "author": (r["author"] or "").strip()[:MAX_LEN["author"]],
                "category": (r["category"] or "").strip()[:MAX_LEN["category"]] or "其他",
                "score": _rating(r["rating"]),
                "description": (r["description"] or "").strip()[:200],
                "source_url": url,
                # 这批没有榜单排名，不编一个（编了就是假数据）
                "rank_num": None,
                "cover_url": "",
                "site_category": "",
                "catalog_url": "",
                "source_site": _site_of(url),
            })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="把外部 novels CSV 合并进库")
    ap.add_argument("--src", default=str(DEFAULT_SRC), help="CSV 路径")
    ap.add_argument("--apply", action="store_true", help="真写库（默认 dry-run）")
    ap.add_argument("--reindex", action="store_true", help="写完后重建向量索引")
    a = ap.parse_args()

    src = Path(a.src)
    if not src.is_file():
        print(f"找不到文件：{src}")
        return 1

    rows = load_rows(src)
    scored = sum(1 for r in rows if r["score"] is not None)

    print(f"\n读到 {len(rows)} 条（来自 {src.name}）")
    print(f"  有真实评分 {scored} 条，{len(rows) - scored} 条的 rating 是占位值（按缺失处理）")
    print("  分类分布（前 12）：",
          dict(Counter(r["category"] for r in rows).most_common(12)))
    print("  来源站点：", dict(Counter(r["source_site"] for r in rows).most_common()))

    from store import build_index, count, save_novels

    before = count()
    if not a.apply:
        print(f"\n（dry-run，没写库。当前库 {before} 本，合并后约 "
              f"{before + len(rows)} 本，实际以 (title, category) 去重后的结果为准。"
              f"确认后加 --apply）")
        return 0

    save_novels(rows)
    after = count()
    print(f"\n已合并：{before} 本 → {after} 本（新增/更新 {after - before} 条）")

    if a.reindex:
        n = build_index()
        print(f"向量索引已重建：{n} 条")
    else:
        print("向量索引未重建——新书还没有向量，语义检索查不到它们。")
        print("重建命令：python store.py   （约 8000 条 embedding，较慢）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

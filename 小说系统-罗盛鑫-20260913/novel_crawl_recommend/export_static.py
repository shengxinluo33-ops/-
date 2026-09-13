"""导出静态只读站的数据快照。

    python3 export_static.py

产出两个文件（static_site/data/ 下）：
    meta.json    分类统计 + 生成时间，小文件，先加载，侧栏靠它渲染
    novels.json  全部小说（精简字段），约 2.6MB，gzip 后 1.9MB

**这是快照不是镜像**：导出之后工作区里再采新书，静态站不会跟着变，
要重新跑一次这个脚本再发布。工作区的 Flask 系统完全不受影响。

为什么不用一个文件：meta 只有几 KB，先回来就能把分类栏和库存数渲染出来，
不必等 2.6MB 的小说数据下载完——首屏不用白等着。
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from config import ROOT, get_logger

log = get_logger("export_static")

OUT = ROOT / "static_site" / "data"
DB = ROOT / "data" / "novel.db"

# 导出的字段。catalog_url（国图链接）和 site_category（起点分类）详情页要用，
# 留着；id 是详情抽屉里定位用的，也留着。
FIELDS = ("id", "title", "author", "category", "score", "rank_num",
          "description", "cover_url", "source_url", "source_site",
          "site_category", "catalog_url", "create_time")


def load_rows() -> list[dict]:
    if not DB.exists():
        raise SystemExit(f"找不到数据库：{DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM novels").fetchall()
    finally:
        conn.close()
    return [{k: r[k] for k in FIELDS} for r in rows]


def main() -> None:
    rows = load_rows()
    if not rows:
        raise SystemExit("库里一条都没有，先跑爬虫或导入 CSV")

    # 分类按本数降序，和工作区 /api/categories 的顺序保持一致
    counts = Counter(r["category"] or "其他" for r in rows)
    categories = [{"name": c, "count": n}
                  for c, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    OUT.mkdir(parents=True, exist_ok=True)

    meta = {
        "novels": len(rows),
        "categories": categories,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": "data/novel.db",
    }
    (OUT / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    # 小说是数组而不是对象包一层：前端 JSON.parse 完直接用，少一层解包
    (OUT / "novels.json").write_text(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")

    size = (OUT / "novels.json").stat().st_size / 1048576
    log.info("导出 %d 本、%d 个分类 → %s", len(rows), len(categories), OUT)
    print(f"已导出 {len(rows)} 本，{len(categories)} 个分类")
    print(f"  {OUT / 'meta.json'}")
    print(f"  {OUT / 'novels.json'}（{size:.2f} MB，gzip 后约 1.9 MB）")
    print("下一步：把 static_site/ 整个目录发布到静态托管")


if __name__ == "__main__":
    main()

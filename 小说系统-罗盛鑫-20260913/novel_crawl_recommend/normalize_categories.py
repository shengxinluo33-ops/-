"""分类归一：同类异名的合并成一个，不是小说分类的归到"其他"。

合并进来的那批数据带了 54 个分类，里面两类脏：

1. **同类异名**：悬疑 / 悬疑灵异 / 悬疑侦探 / 悬疑推理 / 推理其实是同一个东西，
   古代言情 / 现代言情 / 玄幻言情 / 言情也是。54 个分类里有一半是这么来的。
2. **根本不是小说分类**：计算机网络、管理、经济、法律、医学、宗教、投资理财、
   期刊杂志……这些是豆瓣的**图书**分类，混在小说库里很怪。

用法：

    python normalize_categories.py            # dry-run：打印映射表
    python normalize_categories.py --apply    # 真改库

**改完通常不需要重建向量索引**：只动 category 字段，行的 id 和顺序都没变，
FAISS 里存的是 novels.id，简介向量也没变，索引自然还有效。
唯一的例外是**归一后有同名的书撞了 `(title, category)` 唯一键**
（比如《江湖》在"小说"和"文化"里各有一条，归并到"其他"就重了）——
脚本会先合并这些冲突行，删了行才需要重建索引。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from config import get_logger

log = get_logger("normalize_categories")

# 归一规则：**按顺序匹配，先匹配到的生效**。顺序是有意义的——
# "玄幻言情" 同时含"玄幻"和"言情"，它在红袖是言情频道（玄幻背景的言情），
# 所以言情的规则要排在玄幻前面。
CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("悬疑", ("悬疑", "推理")),      # 推理也归悬疑：推理小说本来就是悬疑大类
    ("言情", ("言情",)),
    ("科幻", ("科幻",)),
    ("仙侠", ("仙侠",)),
    ("游戏", ("游戏",)),
    ("青春", ("青春",)),
    ("体育", ("体育",)),
    ("现实", ("现实",)),
]

# 不是小说分类（来自豆瓣的图书分类，以及"小说"这种等于没分类的宽泛标签）。
# 这些归到"其他"，不硬塞进某个小说品类——塞进去就是在编数据。
NON_FICTION = {
    "计算机网络", "管理", "经济", "法律", "工业技术", "医学", "宗教",
    "投资理财", "科普读物", "文化", "文学", "成功励志", "两性关系",
    "期刊杂志", "网络", "公版免费书", "童书", "小说",
}

OTHER = "其他"


def normalize_category(cat: str) -> tuple[str, str]:
    """返回 (新分类, 原因)。原因用来在 dry-run 里解释"为什么动它"。"""
    for target, keywords in CATEGORY_RULES:
        hit = [k for k in keywords if k in cat]
        if hit:
            return target, f"含「{hit[0]}」"
    if cat in NON_FICTION:
        return OTHER, "不是小说分类"
    return cat, ""


def plan(conn: sqlite3.Connection) -> list[dict]:
    """算出每一行要改成什么，返回改动计划（不写库）。"""
    rows = conn.execute("SELECT id, title, category FROM novels").fetchall()
    out = []
    for r in rows:
        new, reason = normalize_category(r["category"])
        if new != r["category"]:
            out.append({"id": r["id"], "title": r["title"],
                        "old": r["category"], "new": new, "reason": reason})
    return out


def _richness(r: sqlite3.Row) -> int:
    """信息量打分：决定冲突时保留哪条。封面和评分比简介长度值钱。"""
    return (len(r["description"] or "")
            + (100 if (r["cover_url"] or "").strip() else 0)
            + (50 if r["score"] is not None else 0)
            + (30 if (r["author"] or "").strip() else 0))


def find_conflicts(conn: sqlite3.Connection) -> dict[tuple[str, str], list[sqlite3.Row]]:
    """归一后哪些 (title, category) 会撞 UNIQUE。

    **这个检测不能漏掉 NON_FICTION 那条规则**：第一版就漏了，只算了关键词组内
    的冲突，结果"小说""文化""文学"归并到"其他"时和原有的"其他"撞了，
    `UPDATE` 直接 IntegrityError 崩在半路。
    """
    rows = conn.execute("SELECT * FROM novels").fetchall()
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in rows:
        new, _ = normalize_category(r["category"])
        groups.setdefault((r["title"], new), []).append(r)
    return {k: v for k, v in groups.items() if len(v) > 1}


def merge_conflicts(conn: sqlite3.Connection,
                    conflicts: dict[tuple[str, str], list[sqlite3.Row]],
                    apply: bool) -> int:
    """冲突组里保留信息最全的一条，其余的补完缺字段后删掉。返回删除条数。"""
    cols = ("author", "cover_url", "site_category", "catalog_url", "source_url")
    removed = 0
    for (title, cat), group in sorted(conflicts.items()):
        keeper, dups = sorted(group, key=_richness, reverse=True)[0], \
            sorted(group, key=_richness, reverse=True)[1:]
        print(f"  冲突《{title}》→ {cat}：保留 id={keeper['id']}（旧分类"
              f"{keeper['category']}），删 {len(dups)} 条 "
              f"（{[d['category'] for d in dups]}）")
        for d in dups:
            # 保留行缺的字段，从要删的那条补过来，别白白丢信息
            for col in cols:
                if not (keeper[col] or "").strip() and (d[col] or "").strip():
                    if apply:
                        conn.execute(f"UPDATE novels SET {col} = ? WHERE id = ?",
                                     (d[col], keeper["id"]))
            if keeper["score"] is None and d["score"] is not None:
                if apply:
                    conn.execute("UPDATE novels SET score = ? WHERE id = ?",
                                 (d["score"], keeper["id"]))
            if apply:
                conn.execute("DELETE FROM novels WHERE id = ?", (d["id"],))
            removed += 1
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description="分类归一")
    ap.add_argument("--apply", action="store_true", help="真改库（默认 dry-run）")
    a = ap.parse_args()

    from store import connect, init_db

    init_db()
    conn = connect()
    conn.row_factory = sqlite3.Row

    # 改动前后的分类分布，打印出来对比
    before = conn.execute(
        "SELECT category, COUNT(*) n FROM novels GROUP BY category ORDER BY n DESC"
    ).fetchall()

    changes = plan(conn)
    if not changes:
        print("没有需要归一的分类。")
        return 0

    # 按"旧分类 → 新分类"聚合，看清楚每一组的规模
    grouped: dict[tuple[str, str], int] = {}
    for c in changes:
        grouped[(c["old"], c["new"])] = grouped.get((c["old"], c["new"]), 0) + 1

    print(f"\n[分类归一] {len(changes)} 本会改分类，涉及 {len(grouped)} 组：\n")
    for (old, new), n in sorted(grouped.items(), key=lambda x: -x[1]):
        reason = normalize_category(old)[1]
        print(f"  {old:10s} → {new:6s}  {n:5d} 本   （{reason}）")

    # 冲突必须在 UPDATE 之前解决，否则 UNIQUE 约束会让 UPDATE 崩在半路
    conflicts = find_conflicts(conn)
    if conflicts:
        print(f"\n[唯一键冲突] 归一后有 {len(conflicts)} 组书名会撞车，需要先合并：\n")
        merge_conflicts(conn, conflicts, apply=False)

    if not a.apply:
        print(f"\n（dry-run，没动库。加 --apply 执行）")
        return 0

    removed = 0
    if conflicts:
        removed = merge_conflicts(conn, conflicts, apply=True)
        conn.commit()
        print(f"  已合并，删掉 {removed} 条重复")

    # 冲突里的行已经被删了，重新算一遍计划，别去 UPDATE 不存在的行
    changes = [c for c in plan(conn)
               if c["id"] not in
               {d["id"] for g in conflicts.values() for d in
                sorted(g, key=_richness, reverse=True)[1:]}]
    conn.executemany(
        "UPDATE novels SET category = ? WHERE id = ?",
        [(c["new"], c["id"]) for c in changes])
    conn.commit()

    after = conn.execute(
        "SELECT category, COUNT(*) n FROM novels GROUP BY category ORDER BY n DESC"
    ).fetchall()
    print(f"\n已归一：分类 {len(before)} 个 → {len(after)} 个")
    print("\n归一后的分类分布：")
    for r in after:
        print(f"  {r['category']:10s} {r['n']}")

    if removed:
        # 删了行，FAISS 里就多出孤儿向量，必须重建
        from store import build_index

        n = build_index()
        print(f"\n删掉 {removed} 条冲突记录，向量索引已重建：{n} 条")
    else:
        print("\n向量索引不需要重建——只改了 category，行的 id 和顺序没变，"
              "FAISS 里存的是 novels.id。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

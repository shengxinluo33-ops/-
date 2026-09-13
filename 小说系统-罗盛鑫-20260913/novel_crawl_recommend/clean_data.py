"""一次性数据维护：清洗简介 → 合并重复条目 → 重建向量索引。

三件事放在一个脚本里，是因为**后两步都要重建 FAISS 索引**（简介变了向量就得重算，
条目删了索引里就多一条孤儿）。分开跑等于多烧一遍 embedding 额度，合起来只重建一次。

用法：

    python clean_data.py            # dry-run：只打印会改什么，不动库
    python clean_data.py --apply    # 真改库
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys

from config import get_logger
from crawler import strip_inline_noise
from titlematch import normalize

log = get_logger("clean_data")

# 兜底句和 crawler.crawl_category 里的写法一致，但**阈值不一样**：采集时用 40 字，
# 这里用 12 字。
#
# 采集时简介抓不到，给一句完整的话比留半截噪声强；但清洗已有数据时，
# 很多真实简介本来就短——《捞尸人》"人知鬼恐怖，鬼晓人心毒。这是一本传统灵异小说。"
# 才 23 字、《青山》30 字、《将夜》11 字，全都是有语义的完整句子。
# 拿 40 字卡它们等于**把真数据换成模板句**，比原来的噪声还糟。
# 所以这里只有真的洗到没内容了（<12 字）才兜底。
MIN_DESC_LEN = 12

# 百科/维基目录残留：一行里"2.2 慕容復 + 2.3 喬峰 + 2.4 虛竹"这样的编号串。
# 出现 3 次以上基本可以确定这段是目录不是简介（正常散文里不会这么编号）。
_CATALOG_NOISE_RE = re.compile(r"(?:\d+\.){1,3}\d*(?=\s|$)")
_CATALOG_MIN_HITS = 3


# 页面 chrome（导航条/商店信息）残留：正常简介不会把站点的导航词堆成一串，
# 也不会把书名重复三遍（那是面包屑），出现这些说明抓的是页面外壳不是正文。
# 实测《异常测定》"阅读记录 五二首页 最近更新 言情书单 言情作者…"
# 和《从地球到月球》"écrit par … sur Apple Books … 7,49 €" 都是这类，
# 清洗掉模板词之后长度仍然够，光靠字数判断抓不出来。
_NAV_WORDS = ("首页", "最近更新", "书单", "作者大全", "阅读记录", "男频", "女频",
              "其他小说", "耽美小说", "排行榜", "全本", "完本", "免费阅读")
_NAV_MIN_HITS = 3
_STORE_WORDS = ("apple books", "écrit par", "écritpar", "written by",
                "kindle", "audible", "€", "£", "¥")


def is_catalog_noise(text: str) -> bool:
    """这段文本是页面目录结构，不是简介。"""
    return len(_CATALOG_NOISE_RE.findall(text)) >= _CATALOG_MIN_HITS


def is_page_chrome(text: str) -> bool:
    """这段是页面外壳（导航条/商店信息），不是简介正文。

    实测过一条更狠的规则——"书名在简介里出现 3 次以上就是面包屑"——**不能用**：
    《饲犬》《已知的世界》这些正常简介里书名本来就会重复出现（"《饲犬》by鸣鸾"、
    "《饲犬》鸣銮- 言情小说"），一条都没落下全被误判。宁可漏判也不要误杀真简介。
    """
    if sum(1 for w in _NAV_WORDS if w in text) >= _NAV_MIN_HITS:
        return True
    low = text.lower()
    return any(w in low for w in _STORE_WORDS)


def fallback_desc(title: str, author: str, category: str) -> str:
    who = f"{author}创作的" if author else ""
    return f"《{title}》是{who}{category}类小说。"


# 人工确认的同一本书：中外文异名，归一化后对不上，只能靠清单。
# 自动检测（normalize 后相同）抓不到这些——"狼廳" 归一成 "狼厅"、
# "Ｗolf Hall" 归一成 "wolfhall"，两个字符串毫无关系。
KNOWN_DUPES: list[tuple[str, ...]] = [
    ("狼廳", "Ｗolf Hall"),
    ("The Known World", "已知的世界"),
    ("My Brilliant Friend", "那不勒斯故事"),
    ("Corrections", "修正"),
]


def clean_descriptions(conn: sqlite3.Connection, apply: bool) -> int:
    """清洗简介。返回改动条数。"""
    rows = conn.execute("SELECT id,title,author,category,description FROM novels").fetchall()
    changed = 0
    samples: list[str] = []

    for r in rows:
        old = r["description"] or ""
        # 已有数据是单行，只能用片段级清洗（clean_text 会整行丢弃，
        # 单行简介里带个"广告"就全没了）
        new = strip_inline_noise(old)[:300]

        # 清洗完还是垃圾（目录 / 页面外壳 / 太短），就用兜底句——
        # 留着目录和导航栏对语义检索是纯噪声，不如一句完整的话
        reason = ""
        if len(new) < MIN_DESC_LEN:
            reason = "清洗后过短"
        elif is_catalog_noise(new):
            reason = "百科目录残留"
        elif is_page_chrome(new):
            reason = "页面导航/商店信息残留"
        if reason:
            new = fallback_desc(r["title"], r["author"] or "", r["category"])

        if new == old:
            continue
        changed += 1
        if apply:
            conn.execute("UPDATE novels SET description = ? WHERE id = ?", (new, r["id"]))
        if reason:
            # 兜底是破坏性最大的一步（整段话换掉），全部列出来给人核
            print(f"  [兜底·{reason}] 《{r['title']}》 {len(old)}字→{len(new)}字")
            print(f"      旧：{old[:70]}")
        elif len(samples) < 6:
            samples.append(f"  《{r['title']}》 {len(old)}字→{len(new)}字 [清洗噪声]\n"
                           f"      旧：{old[:70]}\n      新：{new[:70]}")

    print(f"\n[简介清洗] {len(rows)} 本里有 {changed} 本会变（其中兜底替换的已全部列出）")
    for s in samples:
        print(s)
    return changed


def find_duplicate_groups(conn: sqlite3.Connection) -> list[list[sqlite3.Row]]:
    """找出同一本书的重复条目：归一化书名相同的，加人工清单里的中外文异名。"""
    rows = conn.execute("SELECT * FROM novels ORDER BY id").fetchall()

    groups: list[list[sqlite3.Row]] = []
    by_norm: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_norm.setdefault(normalize(r["title"]), []).append(r)
    groups.extend(g for g in by_norm.values() if len(g) > 1)

    # 人工清单：按书名精确匹配，能配上就凑一组
    by_title = {r["title"]: r for r in rows}
    for names in KNOWN_DUPES:
        group = [by_title[n] for n in names if n in by_title]
        if len(group) > 1:
            groups.append(group)

    return groups


def _richness(r: sqlite3.Row) -> int:
    """信息量打分：决定重复条目里保留哪条。封面和评分比简介长度值钱。"""
    return (len(r["description"] or "")
            + (100 if (r["cover_url"] or "").strip() else 0)
            + (50 if r["score"] is not None else 0)
            + (30 if (r["author"] or "").strip() else 0))


def merge_duplicates(conn: sqlite3.Connection, apply: bool) -> int:
    """合并重复条目：保留信息最全的那条，其余的补完缺字段后删掉。返回删除条数。"""
    groups = find_duplicate_groups(conn)
    if not groups:
        print("\n[重复条目] 没有发现重复")
        return 0

    print(f"\n[重复条目] 发现 {len(groups)} 组：")
    removed = 0
    for g in groups:
        g_sorted = sorted(g, key=_richness, reverse=True)
        keep, dups = g_sorted[0], g_sorted[1:]
        titles = " / ".join(f"《{r['title']}》" for r in g_sorted)
        print(f"  {titles}")
        print(f"    保留 id={keep['id']}（简介{len(keep['description'] or '')}字"
              f" 封面{'有' if keep['cover_url'] else '无'}"
              f" 评分{keep['score'] or '无'}），删掉 {len(dups)} 条")

        for d in dups:
            # 保留条缺的字段，从被删的那条上补过来，别白白丢信息
            for col in ("author", "cover_url", "site_category", "catalog_url"):
                if not (keep[col] or "").strip() and (d[col] or "").strip():
                    if apply:
                        conn.execute(f"UPDATE novels SET {col} = ? WHERE id = ?",
                                     (d[col], keep["id"]))
                    print(f"      id={keep['id']} 补上 {col}（来自 id={d['id']}）")
            if keep["score"] is None and d["score"] is not None:
                if apply:
                    conn.execute("UPDATE novels SET score = ? WHERE id = ?",
                                 (d["score"], keep["id"]))
                print(f"      id={keep['id']} 补上 score={d['score']}（来自 id={d['id']}）")
            if apply:
                conn.execute("DELETE FROM novels WHERE id = ?", (d["id"],))
            removed += 1
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description="清洗简介 + 合并重复条目")
    ap.add_argument("--apply", action="store_true", help="真改库（默认 dry-run）")
    a = ap.parse_args()

    from store import build_index, connect, init_db

    init_db()
    conn = connect()
    conn.row_factory = sqlite3.Row

    changed = clean_descriptions(conn, a.apply)
    removed = merge_duplicates(conn, a.apply)

    if not a.apply:
        print(f"\n（dry-run，没动库。会改 {changed} 条简介、删 {removed} 条重复。"
              f"确认后加 --apply）")
        return 0

    conn.commit()
    n = build_index()
    print(f"\n已改 {changed} 条简介、删掉 {removed} 条重复条目，向量索引重建：{n} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())

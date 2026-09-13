"""存储层：SQLite 存元数据，FAISS 存简介向量。

表结构：任务书要的九个字段，加后补的四个（封面、来源站分类、来源站点、国图链接）：

    id, title, author, category, score, rank_num, description, source_url, create_time
    cover_url, site_category, source_site, catalog_url

向量库用 FAISS 的 IndexFlatIP（向量归一化后内积即余弦），meta 里存
**每条向量对应的 novels.id**，这样语义检索命中的是数据库里的行，
不会出现"向量下标漂移导致张冠李戴"的问题。

embedding 有两档（和 novel_search_agent 里那一套一致）：
配了 EMBEDDING_API_KEY 走在线模型（BAAI/bge-m3），没配就降级成本地哈希向量。
**换模型必须重建索引** —— 两档都是 1024 维，光比维度看不出来，所以这里
连同模型名一起比（这个坑在 novel_search_agent 里踩过）。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime

from config import (
    DATA_DIR,
    DB_PATH,
    DEFAULT_EMBEDDING_BASE_URL,
    DEFAULT_EMBEDDING_MODEL,
    INDEX_PATH,
    META_PATH,
    env,
    get_logger,
)

log = get_logger("store")

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS novels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    author      TEXT    DEFAULT '',
    category    TEXT    NOT NULL,
    score       REAL,
    rank_num    INTEGER,
    description TEXT    DEFAULT '',
    source_url  TEXT    DEFAULT '',
    create_time TEXT    DEFAULT (datetime('now','localtime')),
    UNIQUE(title, category)
);
"""

# 后加的字段（起点封面 / 起点自有分类 / 来源站点 / 国图权威链接）。
# SQLite 没有 ALTER TABLE IF NOT EXISTS，只能查 pragma 判断，老库升级时要跑一遍。
EXTRA_COLUMNS = {
    "cover_url": "TEXT DEFAULT ''",
    "site_category": "TEXT DEFAULT ''",
    "source_site": "TEXT DEFAULT ''",
    "catalog_url": "TEXT DEFAULT ''",
}

_TOKEN_RE = re.compile(r"[一-龥]|[A-Za-z]+|\d+")


# ---------------------------------------------------------------- SQLite

def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """建表，并给老库补后加的字段（幂等，重复调用无害）。"""
    with connect() as conn:
        conn.execute(CREATE_SQL)
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(novels)")}
        for col, ddl in EXTRA_COLUMNS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE novels ADD COLUMN {col} {ddl}")
                log.info("表结构升级：新增列 %s", col)


def save_novels(rows: list[dict]) -> int:
    """写入/更新小说元数据。按 (title, category) 去重，重复采集会更新而非堆积。"""
    init_db()
    sql = """
    INSERT INTO novels (title, author, category, score, rank_num, description,
                        source_url, cover_url, site_category, source_site, catalog_url)
    VALUES (:title, :author, :category, :score, :rank_num, :description, :source_url,
            :cover_url, :site_category, :source_site, :catalog_url)
    ON CONFLICT(title, category) DO UPDATE SET
        author      = excluded.author,
        score       = COALESCE(excluded.score, novels.score),
        rank_num    = excluded.rank_num,
        description = CASE WHEN length(excluded.description) > length(novels.description)
                           THEN excluded.description ELSE novels.description END,
        source_url  = excluded.source_url,
        cover_url   = COALESCE(NULLIF(excluded.cover_url, ''), novels.cover_url),
        site_category = COALESCE(NULLIF(excluded.site_category, ''), novels.site_category),
        source_site = COALESCE(NULLIF(excluded.source_site, ''), novels.source_site),
        catalog_url = COALESCE(NULLIF(excluded.catalog_url, ''), novels.catalog_url),
        create_time = datetime('now','localtime')
    """
    payload = [{
        "title": r["title"], "author": r.get("author", ""), "category": r["category"],
        "score": r.get("score"), "rank_num": r.get("rank_num"),
        "description": r.get("description", ""), "source_url": r.get("source_url", ""),
        "cover_url": r.get("cover_url", ""), "site_category": r.get("site_category", ""),
        "source_site": r.get("source_site", ""), "catalog_url": r.get("catalog_url", ""),
    } for r in rows]
    with connect() as conn:
        conn.executemany(sql, payload)
    return len(payload)


# 检索时当分隔符处理的字符。用户输入「无脑 爽文」「无脑、爽文」「无脑·爽文」
# 都该当成「无脑」+「爽文」两片去匹配——原来的实现直接拼进 LIKE，
# 变成要求原文里正好有个空格，实测「无脑爽文」只能搜到 1 本。
_SEP_RE = re.compile(
    r"[\s\u3000、，。！？；：·・…—\-_~～()（）《》〈〉【】\[\]{}「」『』“”‘’'\"|/\\]+")
_MIN_PART_LEN = 2          # 短于 2 字的碎片不参与匹配（"的""了"这类）


def split_keyword(keyword: str) -> list[str]:
    """把带分隔符的关键词切成片段：「无脑 爽文」→ ["无脑", "爽文"]。

    切不出来（本来就没有分隔符）就返回原词归一化后的单个片段。
    """
    parts = [p for p in _SEP_RE.split(keyword) if len(p) >= _MIN_PART_LEN]
    if parts:
        return parts
    return [keyword] if len(keyword) >= _MIN_PART_LEN else []


def _match_where(category: str, keyword: str) -> tuple[str, list]:
    """拼 WHERE 子句。**注意分类走的是 LIKE，不是等值**：

    合并进来的那批数据有 54 个分类，"言情"要能同时命中"古代言情""现代言情"
    "玄幻言情"，"科幻"要能命中"科幻"和"科幻空间"。等值匹配会把这些全漏掉。

    关键词的处理分两种（实测出来的，见 fixlog 第 26 条）：

    - **用户显式加了分隔符**（"无脑 爽文"）→ 按片段 OR 匹配。
      不这么干的话 `LIKE '%无脑 爽文%'` 要求原文里正好有个空格，
      实测「无脑爽文」只能搜到 1 本，而「爽文」单独有 131 本。
    - **没加分隔符**（"无脑爽文"）→ 当整体匹配。不擅自切词，
      否则「张三丰」会被切成「张三」「三丰」误伤一片。

    返回 (sql 片段, 参数)。
    """
    where, args = [], []
    if category:
        where.append("category LIKE ?")
        args.append(f"%{category}%")
    if keyword:
        parts = split_keyword(keyword)
        if len(parts) > 1:
            conds = []
            for p in parts:
                conds.append("(title LIKE ? OR author LIKE ? OR description LIKE ?)")
                args.extend([f"%{p}%"] * 3)
            where.append("(" + " OR ".join(conds) + ")")
        elif parts:
            where.append("(title LIKE ? OR author LIKE ? OR description LIKE ?)")
            args.extend([f"%{parts[0]}%"] * 3)
    clause = "WHERE " + " AND ".join(where) if where else ""
    return clause, args


def keyword_query(category: str = "", keyword: str = "",
                  sort_by: str = "score", limit: int = 20,
                  offset: int = 0) -> list[sqlite3.Row]:
    """模式①：分类/关键词检索。排序可选 score（评分）或 rank_num（榜单排名）。

    `offset` 是后来加的——库涨到八千本之后一页装不下，必须能翻页。
    """
    init_db()
    order = "rank_num ASC" if sort_by == "rank" else "score DESC, rank_num ASC"
    clause, args = _match_where(category, keyword)
    sql = (f"SELECT * FROM novels {clause} ORDER BY {order} "
           f"LIMIT ? OFFSET ?")
    args.extend([int(limit), int(offset)])
    with connect() as conn:
        return conn.execute(sql, args).fetchall()


def count_matches(category: str = "", keyword: str = "") -> int:
    """同一个筛选条件下一共有多少本——分页控件要知道总页数。

    参数和 `keyword_query` 里的筛选条件必须用**同一个** `_match_where` 拼，
    否则分页总数和结果条数会对不上（这种 bug 翻到第二页才会暴露）。
    """
    init_db()
    clause, args = _match_where(category, keyword)
    with connect() as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM novels {clause}", args).fetchone()[0]


def category_counts() -> list[tuple[str, int]]:
    """各分类有多少本，按数量降序。用于动态生成导航栏。

    分类不再是写死的 7 个：合并进来的那批数据带了 54 个分类
    （轻小说、短篇、仙侠、古代言情…），写死就全看不见了。
    """
    init_db()
    with connect() as conn:
        return conn.execute(
            "SELECT category, COUNT(*) n FROM novels "
            "GROUP BY category ORDER BY n DESC, category").fetchall()


def rows_without_covers(limit: int = 0, sources: tuple = ()) -> list[sqlite3.Row]:
    """捞还没有封面的书，用来回填封面。

    sources 用来限定来源（见 fixlog 第 23 条）：我们自己采的那 191 本
    id 最小，按 id 排会排在最前面，但它们上一轮已经查过微信读书了 —— 起点和
    微信读书都没有（冷门网文 + 外文原著），再查一遍还是 0 命中。
    真正有戏的是外部 CSV 那批，用 sources=('起点中文网','红袖添香',...) 圈出来。
    """
    init_db()
    sql = "SELECT * FROM novels WHERE COALESCE(cover_url,'') = ''"
    if sources:
        ph = ",".join("?" * len(sources))
        sql += f" AND source_site IN ({ph})"
    sql += " ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with connect() as conn:
        return conn.execute(sql, sources).fetchall()


def rows_without_scores(limit: int = 0, sources: tuple = ()) -> list[sqlite3.Row]:
    """捞还没有评分的书。公开榜单页基本不给结构化评分，所以这批是绝大多数。

    sources 的用途同 rows_without_covers：跳开已经查过一遍、注定查不到的自己采的那批。
    """
    init_db()
    sql = "SELECT * FROM novels WHERE score IS NULL"
    if sources:
        ph = ",".join("?" * len(sources))
        sql += f" AND source_site IN ({ph})"
    sql += " ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with connect() as conn:
        return conn.execute(sql, sources).fetchall()


def update_metadata(novel_id: int, **fields) -> None:
    """更新一本书的部分字段（回填封面/国图链接/评分时用）。

    只改传进来的字段，且**不覆盖已有值**——回填是为了补空缺，
    不是为了把起点数据盖到榜单页数据上。

    score 也走这里，同样是"只填空"：库里已有的 16 个真实评分不会
    被微信读书的推荐值盖掉。
    """
    allowed = {"cover_url", "site_category", "source_site", "catalog_url", "score"}
    pairs = [(k, v) for k, v in fields.items() if k in allowed and v]
    if not pairs:
        return
    sets = ", ".join(f"{k} = ?" for k, _ in pairs)
    with connect() as conn:
        conn.execute(f"UPDATE novels SET {sets} WHERE id = ?",
                     [v for _, v in pairs] + [novel_id])


def get_by_ids(ids: list[int]) -> list[sqlite3.Row]:
    if not ids:
        return []
    init_db()
    placeholders = ",".join("?" * len(ids))
    with connect() as conn:
        return conn.execute(
            f"SELECT * FROM novels WHERE id IN ({placeholders})", ids).fetchall()


def count() -> int:
    init_db()
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM novels").fetchone()[0]


# ---------------------------------------------------------------- 向量化

class HashingEmbeddings:
    """没配 Key 时的本地降级向量：词哈希 + L2 归一化，1024 维。

    只抓字面重合，不抓语义。能跑通流程，别当效果基线。
    """

    dim = 1024

    def embed(self, texts: list[str]) -> list[list[float]]:
        import numpy as np

        vecs = np.zeros((len(texts), self.dim), dtype="float32")
        for i, t in enumerate(texts):
            for tok in _TOKEN_RE.findall(t.lower()):
                h = int.from_bytes(
                    hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest(), "big")
                vecs[i, h % self.dim] += 1.0
                vecs[i, (h >> 13) % self.dim] += 0.5
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (vecs / norms).tolist()

    @property
    def name(self) -> str:
        return "HashingEmbeddings(本地降级)"


class OnlineEmbeddings:
    """走 OpenAI 兼容接口的在线 embedding（硅基流动 BAAI/bge-m3）。"""

    def __init__(self) -> None:
        from langchain_openai import OpenAIEmbeddings

        base_url = env("EMBEDDING_BASE_URL") or DEFAULT_EMBEDDING_BASE_URL
        if not base_url:
            base_url = env("BASE_URL")
            log.warning("EMBEDDING_BASE_URL 未设置，退回聊天模型的地址 %s。"
                        "两家通常不是一个渠道，报 model_not_found 就先查这里。", base_url)
        self._impl = OpenAIEmbeddings(
            model=env("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
            api_key=env("EMBEDDING_API_KEY"),
            base_url=base_url,
            check_embedding_ctx_length=False,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._impl.embed_documents(texts)

    @property
    def name(self) -> str:
        return env("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)


def get_embedder():
    return OnlineEmbeddings() if env("EMBEDDING_API_KEY") else HashingEmbeddings()


# ---------------------------------------------------------------- FAISS

def _load_meta() -> dict:
    if META_PATH.is_file():
        return json.loads(META_PATH.read_text(encoding="utf-8"))
    return {"ids": [], "dim": None, "embedder": ""}


def _save_meta(meta: dict) -> None:
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


# 一次发给 embedding 服务的条数。库涨到八千本之后不能一次全发——
# 8000 条 × 200 字是上百万字符，一次性请求会直接超服务的 token 上限。
# 实测 128 条 / 0.3 秒，八千本分 63 批约两分钟。
EMBED_BATCH = 128


def _embed_batched(embedder, texts: list[str]) -> list[list[float]]:
    """分批 embed。批间没有额外延时——瓶颈是服务端算力不是礼貌，
    而且硅基流动对 embedding 没有像起点那样凶的限流。"""
    vecs: list[list[float]] = []
    total = len(texts)
    for start in range(0, total, EMBED_BATCH):
        vecs.extend(embedder.embed(texts[start:start + EMBED_BATCH]))
        done = min(start + EMBED_BATCH, total)
        if done % (EMBED_BATCH * 10) < EMBED_BATCH or done == total:
            log.info("向量化进度 %d/%d", done, total)
    return vecs


def build_index(rows: list[sqlite3.Row] | None = None) -> int:
    """把小说简介灌进向量库。默认灌全表。

    重建条件：**维度变了，或者向量模型变了**。只看维度不够——本地哈希和
    bge-m3 都是 1024 维，维度一样但向量空间不同，混着检索出来的分数没有意义。
    """
    import faiss
    import numpy as np

    init_db()
    if rows is None:
        with connect() as conn:
            rows = conn.execute("SELECT * FROM novels ORDER BY id").fetchall()
    if not rows:
        log.warning("表里没有数据，先跑 crawl 采集。")
        return 0

    embedder = get_embedder()
    texts = [f"{r['title']}。{r['description'] or ''}" for r in rows]
    vecs = np.asarray(_embed_batched(embedder, texts), dtype="float32")
    dim = vecs.shape[1]

    meta = _load_meta()
    if meta["ids"] and (meta["dim"] != dim or meta["embedder"] != embedder.name):
        log.warning("向量模型 %s(%s维) -> %s(%s维)，重建索引",
                    meta["embedder"], meta["dim"], embedder.name, dim)
        INDEX_PATH.unlink(missing_ok=True)
        meta = {"ids": [], "dim": dim, "embedder": embedder.name}

    index = faiss.IndexFlatIP(dim)      # 向量已归一化，内积即余弦相似度
    index.add(vecs)
    faiss.write_index(index, str(INDEX_PATH))
    _save_meta({"ids": [r["id"] for r in rows], "dim": dim, "embedder": embedder.name})
    log.info("向量库建好：%d 条，%d 维，模型 %s", len(rows), dim, embedder.name)
    return len(rows)


def semantic_search(text: str, top_k: int = 5) -> list[tuple[int, float]]:
    """模式②：语义检索，返回 [(novel_id, 相似度), ...]。"""
    import faiss
    import numpy as np

    if not INDEX_PATH.is_file():
        raise RuntimeError("向量索引还没建，先跑 build_index()。")

    embedder = get_embedder()
    index = faiss.read_index(str(INDEX_PATH))
    meta = _load_meta()

    q = np.asarray(embedder.embed([text]), dtype="float32")
    if q.shape[1] != index.d:
        raise RuntimeError(
            f"维度不匹配：索引 {index.d} 维，当前模型 {q.shape[1]} 维。"
            f"索引是用 {meta['embedder']} 建的，请配同一个模型后重建索引。")

    scores, positions = index.search(q, min(top_k, index.ntotal))
    hits = []
    for score, pos in zip(scores[0], positions[0]):
        if pos < 0 or pos >= len(meta["ids"]):
            continue
        hits.append((meta["ids"][pos], float(score)))
    return hits


def index_info() -> dict:
    meta = _load_meta()
    return {
        "exists": INDEX_PATH.is_file(),
        "count": len(meta["ids"]),
        "dim": meta["dim"],
        "embedder": meta["embedder"],
        "db_path": str(DB_PATH),
        "data_dir": str(DATA_DIR),
    }


if __name__ == "__main__":
    init_db()
    print("入库条数：", count())
    print("向量库：", index_info())
    print("建索引：", build_index(), "条")
    print("更新时间：", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

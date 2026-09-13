"""【工具5】DBQuery：SQLite 做结构化存储，FAISS 做语义检索。

一个工具两件套，因为它俩在 Agent 眼里是同一件事：把资料存下来，再按需要捞出来。

    SQLite  tables / schema / insert / query / execute
    FAISS   vector_add / vector_search / vector_info

关于向量化（这是本工具唯一需要解释一下的设计）：

    优先用 EMBEDDING_API_KEY 对应的在线 embedding 模型（默认 nemotron-3-embed-1b）。
    没配 Key 时自动降级成本地的 HashingEmbeddings —— 它不做语义理解，本质是
    加权的词袋哈希，只能抓住字面重合。目的是让自测和离线演示跑得起来，
    不是生产可用的语义检索。想要真正的语义召回，请配 EMBEDDING_API_KEY。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from langchain_core.tools import tool

from config import DATA_DIR, DEFAULT_EMBEDDING_MODEL, env, get_logger
from tools.common import fail, ok, truncate

log = get_logger("tool.dbquery")

DB_PATH = DATA_DIR / "agent.db"
INDEX_PATH = DATA_DIR / "faiss.index"
META_PATH = DATA_DIR / "faiss_meta.json"

ACTIONS = ("tables", "schema", "insert", "query", "execute",
           "vector_add", "vector_search", "vector_info")

_ROW_LIMIT = 50
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


@tool
def db_query(action: str, sql: str = "", text: str = "",
             table: str = "", top_k: int = 5) -> str:
    """SQLite 数据库与 FAISS 向量库的统一入口。

    Args:
        action: tables=列出所有表和结构；schema=执行 CREATE TABLE 建表；
            insert=插入数据；query=执行 SELECT 并返回结果；execute=执行任意 SQL；
            vector_add=把文本切成块写进 FAISS 向量库；
            vector_search=语义检索最相似的片段；vector_info=看向量库当前状态
        sql: tables/schema/insert/query/execute 用的 SQL 语句
        text: vector_add 时要入库的原文，多段用单独一行 "---" 分隔；
            vector_search 时要检索的问句
        table: insert 后顺手要查一下的表名，可留空
        top_k: vector_search 返回几条，默认 5
    """
    action = (action or "").strip().lower()
    if action not in ACTIONS:
        return fail("DBQuery", f"未知动作 {action!r}，可选：{'、'.join(ACTIONS)}")

    log.debug("db_query action=%s", action)
    try:
        if action in ("tables", "schema", "insert", "query", "execute"):
            return _sqlite(action, sql, table)
        return _faiss(action, text, top_k)
    except Exception as e:  # noqa: BLE001 SQL 写错、索引损坏都当数据交回模型
        log.warning("db_query %s 失败：%s", action, e)
        return fail(f"DBQuery {action}", e)


# ---------------------------------------------------------------- SQLite


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _sqlite(action: str, sql: str, table: str) -> str:
    with _connect() as conn:
        if action == "tables":
            rows = conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            if not rows:
                return ok(f"数据库 {DB_PATH.name} 里还没有任何表，用 action=schema 建一张。")
            out = [f"共 {len(rows)} 张表："]
            for r in rows:
                out.append(f"\n· {r['name']}\n{r['sql'] or '(无建表语句)'}")
            return ok(truncate("\n".join(out), label="表结构"))

        if not sql.strip():
            return fail(f"DBQuery {action}", "sql 不能为空")

        cur = conn.execute(sql)

        if action == "query" or (action == "execute" and cur.description is not None):
            rows = cur.fetchmany(_ROW_LIMIT + 1)
            truncated = len(rows) > _ROW_LIMIT
            rows = rows[:_ROW_LIMIT]
            if not rows:
                return ok("查询成功，但没有命中任何行。")
            cols = list(rows[0].keys())
            lines = [" | ".join(cols), " | ".join("---" for _ in cols)]
            for r in rows:
                lines.append(" | ".join("" if v is None else str(v) for v in r))
            head = f"{len(rows)} 行" + (f"（只显示前 {_ROW_LIMIT} 行）" if truncated else "")
            return ok(f"{head}：\n" + truncate("\n".join(lines), label="查询结果"))

        # schema / insert / execute(无结果集)
        conn.commit()
        affected = cur.rowcount
        msg = f"{action} 执行成功"
        if affected is not None and affected >= 0:
            msg += f"，影响 {affected} 行"

        if table:
            rows = conn.execute(f"SELECT * FROM {table} LIMIT 5").fetchall()
            if rows:
                cols = list(rows[0].keys())
                preview = "\n".join(
                    " | ".join("" if v is None else str(v) for v in r) for r in rows
                )
                msg += f"\n{table} 现有内容（最多 5 行）：\n" + " | ".join(cols) + "\n" + preview
        return ok(msg)


# ---------------------------------------------------------------- 向量化


class HashingEmbeddings:
    """零依赖的本地向量化（降级方案，不是语义模型）。

    做法：把文本切成词/字，用 blake2b 把每个词稳稳地映射到向量的一维上
    （不能用内置 hash()，它对字符串每次进程都会变），叠加后 L2 归一化。

    抓得住字面重合，抓不住同义改写。只在没有 EMBEDDING_API_KEY 时启用。
    """

    dim = 1024

    def __init__(self) -> None:
        self._warned = False

    def embed(self, texts: list[str]) -> list[list[float]]:
        import numpy as np

        vecs = np.zeros((len(texts), self.dim), dtype="float32")
        for i, t in enumerate(texts):
            for tok in _TOKEN_RE.findall(t.lower()):
                digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
                h = int.from_bytes(digest, "big")
                vecs[i, h % self.dim] += 1.0
                # 相邻字组成的二元组，让"报到""学校"这类词有独立位置
                vecs[i, (h >> 13) % self.dim] += 0.5
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (vecs / norms).tolist()

    @property
    def name(self) -> str:
        return "HashingEmbeddings(本地降级)"


class OnlineEmbeddings:
    """走 OpenAI 兼容接口的在线 embedding。"""

    def __init__(self) -> None:
        from langchain_openai import OpenAIEmbeddings

        base_url = env("EMBEDDING_BASE_URL")
        if not base_url:
            # 这个兜底害过一次：留空时会拿聊天模型的地址去调 embedding，
            # 换来的只有一句 503 model_not_found，很难想到是地址错了。
            base_url = env("BASE_URL", "https://api.agnes-ai.cn/v1")
            log.warning("EMBEDDING_BASE_URL 未设置，退回聊天模型的地址 %s。"
                        "两个渠道通常不是一家，报 model_not_found 就先查这里。", base_url)
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
    """有 Key 用在线模型，没 Key 用本地哈希。返回对象统一有 embed() 和 dim。"""
    if env("EMBEDDING_API_KEY"):
        return OnlineEmbeddings()
    return HashingEmbeddings()


# ---------------------------------------------------------------- FAISS


def _load_meta() -> dict:
    if META_PATH.is_file():
        return json.loads(META_PATH.read_text(encoding="utf-8"))
    return {"texts": [], "dim": None, "embedder": ""}


def _save_meta(meta: dict) -> None:
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _faiss(action: str, text: str, top_k: int) -> str:
    import faiss  # 放在函数里 import，避免没装 faiss 时整个工具都导入不了
    import numpy as np

    if action == "vector_info":
        meta = _load_meta()
        if not meta["texts"]:
            return ok(f"向量库还是空的（{INDEX_PATH.name} 不存在），先用 action=vector_add 灌文本。")
        return ok(f"{len(meta['texts'])} 个片段，维度 {meta['dim']}，向量模型 {meta['embedder']}")

    if not (text or "").strip():
        return fail(f"DBQuery {action}", "text 不能为空")

    embedder = get_embedder()

    if action == "vector_add":
        chunks = [c.strip() for c in re.split(r"\n-{3,}\n|\n---\n", text) if c.strip()]
        if not chunks:
            return fail("DBQuery vector_add", "text 里没有可用文本")
        vecs = np.asarray(embedder.embed(chunks), dtype="float32")
        dim = vecs.shape[1]

        meta = _load_meta()
        # 换了模型就得重建。光看维度不够：本地哈希向量和 BAAI/bge-m3 都是 1024 维，
        # 维度一样但向量空间完全不同，混在一个索引里检索出来的分数没有意义。
        # 所以维度变了、或者模型名变了，都整个重建。
        if meta["texts"] and (meta["dim"] != dim or meta["embedder"] != embedder.name):
            log.warning("向量模型 %s（%s 维）-> %s（%s 维），重建索引",
                        meta["embedder"], meta["dim"], embedder.name, dim)
            meta = {"texts": [], "dim": dim, "embedder": embedder.name}
            INDEX_PATH.unlink(missing_ok=True)

        if INDEX_PATH.is_file():
            index = faiss.read_index(str(INDEX_PATH))
            index.add(vecs)
            meta["texts"].extend(chunks)
        else:
            index = faiss.IndexFlatIP(dim)        # 向量已归一化，内积即余弦
            index.add(vecs)
            meta = {"texts": list(chunks), "dim": dim, "embedder": embedder.name}

        faiss.write_index(index, str(INDEX_PATH))
        _save_meta(meta)
        return ok(f"已写入 {len(chunks)} 个片段，向量库现在共 {len(meta['texts'])} 个"
                  f"（维度 {dim}，模型：{embedder.name}）")

    if action == "vector_search":
        meta = _load_meta()
        if not meta["texts"] or not INDEX_PATH.is_file():
            return fail("DBQuery vector_search", "向量库是空的，先 vector_add")
        index = faiss.read_index(str(INDEX_PATH))
        q = np.asarray(embedder.embed([text]), dtype="float32")
        if q.shape[1] != index.d:
            return fail("DBQuery vector_search",
                        f"维度不匹配：索引 {index.d} 维，当前模型 {q.shape[1]} 维。"
                        f"索引是用 {meta['embedder']} 建的，请配同一个模型后重建。")
        k = max(1, min(int(top_k or 5), index.ntotal))
        scores, ids = index.search(q, k)

        lines = [f"命中 {k} 条（模型：{embedder.name}）："]
        for rank, (idx, score) in enumerate(zip(ids[0], scores[0]), 1):
            if idx < 0:
                continue
            lines.append(f"\n[{rank}] 相似度 {float(score):.4f}\n{truncate(meta['texts'][idx], 800, '片段')}")
        return ok(truncate("\n".join(lines), label="检索结果"))

    return fail("DBQuery", f"动作 {action} 未处理")


if __name__ == "__main__":
    print(db_query.invoke({"action": "schema",
                           "sql": "CREATE TABLE IF NOT EXISTS novel(id INTEGER PRIMARY KEY, title TEXT)"}))
    print(db_query.invoke({"action": "insert",
                           "sql": "INSERT INTO novel(title) VALUES('测试书名')", "table": "novel"}))
    print(db_query.invoke({"action": "vector_add",
                           "text": "报到需要录取通知书和身份证。\n---\n宿舍在东区 5 号楼。"}))
    print(db_query.invoke({"action": "vector_search", "text": "报到要带什么", "top_k": 2}))

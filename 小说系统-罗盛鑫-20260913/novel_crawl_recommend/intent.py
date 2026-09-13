"""把用户的大白话翻译成检索条件——本项目的"小型 agent"。

    python intent.py "我想看无脑爽文男主厉害"

**它不是一个会自己规划的 Agent**，只做一件事：把一句自然语言翻成
`{category, keywords}`，然后交给现有的检索函数。不做 ReAct 循环、
不挂工具、不让模型逐本写推荐语。理由见 README「为什么 agent 只做翻译」：

- 实测这个模型一次结构化输出 **0.35~20 秒**，多步循环会让页面等到没法用；
- 让它给 20 本书各写一句推荐理由要几秒到十几秒，而检索本身只要 0.3 秒。

**检索走哪条路，取决于模型给没给关键词**（这是实测出来的，不是拍脑袋）：

| 模型返回 | 走哪条路 | 为什么 |
|---|---|---|
| 有分类 + 有关键词 | `分类 AND 关键词` | 「无脑爽文男主厉害」→ 玄幻+无敌 → 30 本，靠谱 |
| 有分类 + 没关键词 | 语义检索 | 「睡前轻松小甜饼」只有分类时等于整个言情 978 本，没筛；语义能给出《吾家阿囡》 |
| 没分类 + 有关键词 | 关键词检索 | — |
| 两个都没有，但没说无关 | 语义检索 | 模型保守起来会什么都不给，这时候不该跟着放弃 |
| 模型明确说 `relevant: false` | **不检索** | 「今天天气怎么样」硬检索只会返回"其他"分类那 125 本噪音 |

**关键词会依次降级**（`_pick_keyword`）：模型给的是一组候选，按"最具体→最泛"
排列，系统从第一个开始试，命中不足 `MIN_HITS`(5) 就退到下一个。实测
「无脑爽文」整词只命中 1 本，而「爽文」有 131 本——不降级的话用户会以为
系统坏了。

**带分隔符的关键词按片段 OR 匹配**（规则在 `store.split_keyword`）：
「无脑 爽文」→ 匹配含"无脑"**或**"爽文"的 → 146 本。
原来直接拼进 `LIKE '%无脑 爽文%'`，等于要求原文里正好有个空格，搜不到东西。
没加分隔符的（「张三丰」）仍当整体匹配，不擅自切词，否则会误伤。

**四级降级，任何一层都不白屏**：没配 key / 超时 / 返回非法 JSON → 规则兜底；
检索 0 条 → 退纯分类 → 再退语义；前端 fetch 失败 → 自己改调语义检索。
"""

from __future__ import annotations

import json
import re
import sys

from config import env, get_logger
# 关键词切分复用 store 的实现：切分规则必须和检索侧完全一致，
# 否则会出现"这里切出来了、那里匹配不上"的怪事。
from store import split_keyword

log = get_logger("intent")

# 实测这个模型一次输出最慢到过 20 秒，必须卡死。超时就走规则兜底，
# 宁可推荐得笨一点也不让页面转圈。
INTENT_TIMEOUT = float(env("INTENT_TIMEOUT", "8"))
# 关键词太长说明模型在复述原话（"想看男主无敌的爽文"），太短是噪音（"的"）。
KEYWORD_MIN_LEN = 2
KEYWORD_MAX_LEN = 8

SYSTEM_PROMPT = """你是小说推荐助手，负责把用户的大白话翻译成检索条件。

只输出一个 JSON 对象，不要解释、不要 markdown 代码围栏，四个键：

- category：只能从下面给定的分类列表里选一个，选不出就给空字符串 ""
- keywords：数组，**1-3 个候选检索词**，按"最具体 → 最泛"排列，
  每个 2-8 字，必须是**小说简介里真会出现的词**。系统会从第一个开始试，
  命中太少就自动退到下一个（更泛的）候选。
- intent_echo：一句话，20 字以内，说明你理解成用户想要什么
- relevant：布尔值。用户在找小说（哪怕说得很模糊、只说了个感觉）就是 true；
  只有完全跟小说无关（问天气、算数学题、乱码）才是 false。

**候选词怎么给**（这条最容易出错，务必照做）：

读者行话是**可用的检索词**，不要因为它是行话就放弃。这些词在简介里真的会写：
  「无脑爽文」→ ["无脑爽文", "爽文"]      库里"爽文"有 131 本
  「推荐个甜文」→ ["甜文"]                "甜文"也是简介会写的词
  「男主无敌」  → ["无敌"]                172 本

反过来，这些才是该避免的：
  整句话：「男主很厉害」    不是检索词，是句子
  纯评价：「好看」「经典」  简介里不会这么写自己

拿不准就**给泛的那个词**，别给空数组。空的 keywords 会让系统完全放弃关键词
检索，比推荐得宽一点糟糕得多。"""

USER_TEMPLATE = """可选分类：{cats}

用户说：{query}

三个例子（照着这个风格来）：
「我想看无脑爽文男主厉害」→ {{"category":"玄幻","keywords":["无敌","爽文"],"intent_echo":"想看男主无敌的爽文","relevant":true}}
「适合睡前看的轻松小甜饼」→ {{"category":"言情","keywords":["小甜饼","轻松"],"intent_echo":"轻松甜蜜的睡前读物","relevant":true}}
「今天天气怎么样」        → {{"category":"","keywords":[],"intent_echo":"这和小说无关，说说想看什么类型吧","relevant":false}}"""


# 候选词命中低于这个数就退到下一个（更泛的）候选。
# 「无脑爽文」整词只命中 1 本，但「爽文」有 131 本 —— 不降级的话等于白搜。
MIN_HITS = 5


def _extract_json(text: str) -> dict | None:
    """从模型的回复里抠出 JSON 对象。两道：先整体解析，再正则兜。

    实测这个模型在输入是乱码（"asdkjh"）时返回的东西不是 JSON，
    所以两道都得有。两道都失败返回 None，交给上层降级。
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else None
    except ValueError:
        pass
    m = re.search(r"\{.*\}", text, re.S)      # 模型偶尔会加一句"好的，这是结果："
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return d if isinstance(d, dict) else None
    except ValueError:
        return None


def parse_intent_llm(query: str, cats: list[str]) -> dict | None:
    """调一次聊天模型，返回 {"category","keywords","intent_echo"}。

    任何一步出问题都返回 None（超时、HTTP 错、返回非法 JSON），
    **不抛异常**——调用方靠 None 判断要不要降级。
    """
    api_key = env("API_KEY")
    if not api_key:
        return None

    try:
        from openai import OpenAI
    except ImportError:                        # 没装 openai 包，直接降级
        log.warning("没装 openai 包，意图解析降级为规则匹配")
        return None

    from config import DEFAULT_BASE_URL, DEFAULT_MODEL_ID

    try:
        client = OpenAI(api_key=api_key,
                        base_url=env("BASE_URL", DEFAULT_BASE_URL),
                        timeout=INTENT_TIMEOUT, max_retries=1)
        resp = client.chat.completions.create(
            model=env("MODEL_ID", DEFAULT_MODEL_ID),
            temperature=0,
            max_tokens=150,
            # response_format 实测这个接口支持，但**不依赖它**——
            # 万一换模型不支持，还有 _extract_json 的正则兜底。
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_TEMPLATE.format(
                    cats="、".join(cats), query=query)},
            ],
        )
        text = resp.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001 超时/限流/网络错，一律降级
        log.warning("LLM 意图解析失败（%s）：%s", type(e).__name__, e)
        return None

    d = _extract_json(text)
    if d is None:
        log.warning("LLM 返回的不是 JSON：%r", text[:120])
        return None
    return d


def parse_intent_rule(query: str, cats: list[str]) -> dict:
    """不调模型的兜底：分类做包含匹配，关键词从原话里剥停用词后切片段。

    能接住「我想看玄幻小说」「无脑 爽文」这类，接不住太隐晦的说法。
    """
    hit = ""
    for c in cats:
        if c and c in query:
            hit = c
            break

    text = query
    for w in _STOP_WORDS:
        text = text.replace(w, " ")
    kws = split_keyword(text)[:3]      # 复用 store 的切分，规则跟检索侧一致

    echo = f"按「{hit}」分类检索" if hit else (
        f"按「{'、'.join(kws)}」检索" if kws else "没识别出条件，用语义检索试试")
    return {"category": hit, "keywords": kws, "intent_echo": echo,
            "relevant": bool(hit or kws)}


# 规则兜底时先剥掉这些词，剩下的才是像检索词的片段
_STOP_WORDS = ("我想看", "我想要", "我想", "想看", "想要", "看看", "有没有", "有没有人",
               "推荐", "来一本", "来本", "给我", "帮我", "找个", "找一下", "一下",
               "什么", "类型", "小说", "书", "的", "了", "吗", "呢", "吧", "啊",
               "有", "是", "要", "看", "个", "一本")


def parse_intent(query: str, cats: list[str]) -> dict:
    """统一入口：先试模型，失败就退回规则。

    返回 dict，一定带 `source` 字段（"llm" / "rule"），前端据此决定
    要不要提示"AI 不可用"。
    """
    d = parse_intent_llm(query, cats)
    if d:
        d["source"] = "llm"
        return d
    d = parse_intent_rule(query, cats)
    d["source"] = "rule"
    return d


def _clean_keywords(raw) -> list[str]:
    """把模型给的候选词洗干净：去分隔符、限长、去重、最多留 3 个。

    模型偶尔会返回字符串而不是数组，也偶尔会带空格（"无脑 爽文"）——
    `split_keyword` 会把它切成片段，后面的降级逻辑才有东西可退。
    """
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        for part in split_keyword(str(item).strip()):
            if KEYWORD_MIN_LEN <= len(part) <= KEYWORD_MAX_LEN and part not in out:
                out.append(part)
    return out[:3]


def _pick_keyword(cands: list[str], cat: str) -> tuple[str, int]:
    """依次试候选词，取第一个命中数够的；都不够就取命中最多的那个。

    候选是按"最具体 → 最泛"排的，所以第一个够用的就是最精准的选择。
    实测「无脑爽文」整词只命中 1 本、而「爽文」有 131 本，不这么做的话
    用户搜"无脑爽文"会以为系统坏了。
    """
    from store import count_matches

    best, best_n = "", -1
    for kw in cands:
        n = count_matches(category=cat, keyword=kw)
        if n > best_n:
            best, best_n = kw, n
        if n >= MIN_HITS:
            log.info("候选词 %r 命中 %d 本，够用", kw, n)
            return kw, n
    if best:
        log.info("候选词都不够 %d 本，用命中最多的 %r（%d 本）", MIN_HITS, best, best_n)
    return best, max(best_n, 0)


def recommend_by_agent(query: str, limit: int = 40,
                       offset: int = 0) -> tuple[list[dict], int, dict]:
    """一句话进，一堆书出。返回 (rows, total, intent)。

    intent 里除了模型给的三个字段，还有：
    - `source`：  "llm" / "rule"，模型有没有接住
    - `strategy`："kw" / "semantic" / "skip"，实际走了哪条检索路
    """
    from recommender import recommend_by_category_kw, recommend_by_keyword, \
        recommend_by_semantics
    from store import category_counts, count_matches

    cats = [c for c, _ in category_counts()]
    plan = parse_intent(query, cats)

    cat = str(plan.get("category") or "").strip()
    if cat and cat not in cats:               # 模型偶尔会自造分类
        log.info("模型给了库里没有的分类 %r，忽略", cat)
        cat = ""
    kws = _clean_keywords(plan.get("keywords"))

    intent = {"echo": str(plan.get("intent_echo") or "").strip(),
              "category": cat, "keywords": kws,
              "source": plan.get("source", "rule"), "strategy": ""}
    log.info("意图解析：source=%s cat=%r kw=%s echo=%r",
             intent["source"], cat, kws, intent["echo"])

    rows: list[dict] = []
    total = 0

    # 只有模型明确说"与小说无关"才放弃检索。以前是"两个都空就 skip"，
    # 结果模型一保守（把"爽文"判成行话不给词）就整轮空手而归。
    irrelevant = plan.get("relevant") is False
    if not cat and not kws:
        if irrelevant:
            intent["strategy"] = "skip"
            return [], 0, intent
        log.info("模型没给条件但也没说无关，兜底语义检索")
        intent["strategy"] = "semantic"
        rows = recommend_by_semantics(query, top_k=limit)
        return rows, len(rows), intent

    if cat and kws:
        # 主路径：分类 AND 关键词。AND 天然收窄，不会像单关键词那样泛。
        intent["strategy"] = "kw"
        kw, total = _pick_keyword(kws, cat)
        if total:
            rows = recommend_by_category_kw(cat, kw, sort_by="score",
                                            limit=limit, offset=offset)
        if not rows and cat:
            # 关键词太窄（比如"硬科幻"只命中 4 本还被分页切掉）→ 退纯分类
            log.info("分类+关键词没结果，退到纯分类「%s」", cat)
            total = count_matches(category=cat)
            rows = recommend_by_category_kw(cat, "", sort_by="score",
                                            limit=limit, offset=offset)
    elif kws:
        intent["strategy"] = "kw"
        kw, total = _pick_keyword(kws, "")
        if total:
            rows = recommend_by_keyword(kw, limit=limit, offset=offset)
    else:
        # 只有分类、没有关键词：说明用户说的是氛围（"睡前轻松小甜饼"），
        # 这时候分类检索等于把整个分类倒出来，语义检索明显更准（见模块 docstring）。
        intent["strategy"] = "semantic"
        rows = recommend_by_semantics(query, top_k=limit)
        total = len(rows)

    if not rows:
        # 最后一招：不管分类直接语义检索，总比空列表强
        log.info("分类/关键词都没结果，兜底语义检索")
        intent["strategy"] = "semantic"
        rows = recommend_by_semantics(query, top_k=limit)
        total = len(rows)

    return rows, total, intent


def main() -> None:
    from store import category_counts

    args = [a for a in sys.argv[1:] if a.strip()]
    if not args:
        args = ["我想看无脑爽文男主厉害", "主角是医生", "适合睡前看的轻松小甜饼"]
    cats = [c for c, _ in category_counts()]
    print(f"库里 {len(cats)} 个分类：{'、'.join(cats)}\n")
    for q in args:
        rows, total, intent = recommend_by_agent(q, limit=5)
        print(f"「{q}」")
        print(f"  source={intent['source']}  strategy={intent['strategy']}  "
              f"cat={intent['category'] or '(空)'}  kw={intent['keywords']}")
        print(f"  回显：{intent['echo'] or '（无）'}   命中 {total} 本")
        for r in rows[:5]:
            print(f"    · [{r['category']}] 《{r['title']}》")
        print()


if __name__ == "__main__":
    main()

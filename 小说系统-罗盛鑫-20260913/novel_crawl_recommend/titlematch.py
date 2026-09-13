"""书名核对：搜索结果里到底哪一条才是我们要找的那本书。

起点和微信读书两个封面源都要用，规则必须**只有一份**——
两边各写一套迟早会不一致，而"两边判断不一样"比"判断错"更难查。

核心立场：**宁可漏，不可错配。** 配错封面比没有封面更糟——
用户看到有封面就默认是这本书的封面，而缺封面时占位图一眼就看得出是占位的。
"""

from __future__ import annotations

import re
import unicodedata

import zhconv


def to_simplified(title: str) -> str:
    """繁体 → 简体，顺带把全角字符收成半角（NFKC）。比对前的第一步。

    库里的书名来自好几个榜单页，繁简根本不统一：**「紐約時報」和「纽约时报」
    是同一本书**，不归一化就永远对不上，封面也就永远补不到（《狼廳》《荒野偵探》
    《地下鐵道》同理）。全角那条是为了「Ｗolf Hall」——首字母是全角 Ｗ，
    zhconv 不管这个，得靠 NFKC。
    """
    return zhconv.convert(unicodedata.normalize("NFKC", title or ""), "zh-cn")


def normalize(title: str) -> str:
    """书名归一化：繁转简、去括号内容、标点、空白，用于比对。"""
    t = to_simplified(title)
    t = re.sub(r"[（(][^）)]*[）)]", "", t)
    t = re.sub(r"[\s　·・\-—_:：,，。.!！?？《》\[\]【】'\"]+", "", t)
    return t.lower()


# 允许出现在书名后面的版本/篇幅后缀，多出来的只有这些才认作同一本书
SUFFIX_RE = re.compile(
    r"^(全集|全集\d*册?|全\d*册|上下?册|合订本|精装版|典藏版|修订版|插图版|"
    r"纪念版|珍藏版|新版|再版|足本|无删减版?|完整版|合集\d*|"
    r"名家名人译|译文版|人民文学出版社|上海译文出版社|"
    r"传奇|三部曲|四部曲|系列|前传|后传|外传|前篇|后篇)$"
)


def match_score(query: str, candidate: str) -> int:
    """候选书名和查询词是不是同一本书。返回可信度，0 表示不认。

    分三档：

      3  归一后完全相等："海底两万里" = "海底两万里（名家名人译）"
      2  只多了版本/篇幅后缀："逆水寒（全集）"、"机器岛（凡尔纳作品精选）"
      0  其他一律不认

    **为什么不要"包含即匹配"**：实测踩过 —— 查「逆水寒」，微信读书返回的第一条是
    "逆水寒：开局掉落琼华白羽！"，那是本同名网文，和温瑞安毫无关系。
    包含规则会直接给它配上那个封面。

    **"作者：书名"这种写法要认**："古龙：陆小凤传奇（全7册）" 就是《陆小凤传奇》。
    但反过来"书名：副标题"（"逆水寒：开局掉落琼华白羽"）是另一本书，不认。

    **查询自己带冒号时先比整条**："法医秦明：天谴者" 是系列分册，
    归一后整条相等就直接认（3 分），不能再拿去拆冒号——拆完递归比"天谴者"，
    六本分册一本都认不出来。
    """
    q = normalize(query)
    if not q:
        return 0
    c = normalize(candidate)
    if not c:
        return 0

    # **整条归一后相等，先认下来再去拆冒号。** 少了这一步，
    # 「法医秦明：天谴者」这种**查询自己带冒号**的系列分册会掉进下面的拆分逻辑
    # （head=法医秦明，tail=天谴者），递归去比「天谴者」，永远对不上。
    # 拆冒号那条规则是为了拦「逆水寒：开局掉落…」，不该把系列分册一起拦掉。
    if q == c:
        return 3

    # NFKC 已经把全角冒号收成半角，所以这里只认 ":" 就够
    head, sep, tail = to_simplified(candidate or "").partition(":")
    if sep and tail:
        # 前缀得像个人名/系列名：够短、不含数字
        if len(normalize(head)) <= 6 and not re.search(r"\d", head):
            return match_score(query, tail)
        return 0

    if c.startswith(q):
        suffix = c[len(q):]
        if not suffix or SUFFIX_RE.match(suffix):
            return 2
    return 0

# -*- coding: utf-8 -*-
"""双 Agent 协作主控：DeepSeek 写爬虫配置 -> Kimi 评审 -> 改进 -> 落地执行。"""
import json
import re

import agents
import generic_scraper


def extract_json(text):
    """从模型输出里稳健地提取 JSON（容忍 Markdown 代码块包裹）。"""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        return m.group(1)
    m = re.search(r"\{.*\}", text, re.S)
    return m.group(0) if m else text


def run(goal, max_rounds=3, out="scraper_config.json"):
    dev_hist = [{"role": "user", "content": goal}]
    final_cfg = None

    for i in range(1, max_rounds + 1):
        # 1) DeepSeek 产出配置
        raw = agents.ask_developer(dev_hist)
        dev_hist.append({"role": "assistant", "content": raw})
        print(f"\n===== 第 {i} 轮 · DeepSeek 产出 =====\n{raw}\n")

        # 2) 尝试解析 JSON；失败则直接把它当成修改意见
        cfg = None
        try:
            cfg = json.loads(extract_json(raw))
            final_cfg = cfg
        except Exception as e:
            review = f"配置 JSON 解析失败：{e}。请重新输出纯 JSON（不要 Markdown 代码块）。"
        else:
            # 3) Kimi 评审
            review = agents.ask_reviewer([{"role": "user", "content": raw}])
        print(f"===== 第 {i} 轮 · Kimi 评审 =====\n{review}\n")

        # 4) 收敛判断（注意「不通过」也含「通过」三个字，必须排除）
        if cfg is not None and "通过" in review and "不通过" not in review:
            break

        # 5) 把评审意见喂回 DeepSeek
        dev_hist.append({"role": "user", "content": f"评审意见（请据此修改）：\n{review}"})

    if final_cfg:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(final_cfg, f, ensure_ascii=False, indent=2)
        print(f"\n配置已保存 -> {out}")
    return final_cfg


if __name__ == "__main__":
    GOAL = (
        "生成一个爬取【某大学计算机专硕近三年复试分数线】的爬虫配置 JSON。\n"
        "JSON 必须包含：\n"
        " - url：目标网页地址\n"
        " - container：每条数据记录的 CSS 选择器\n"
        " - fields：字段名到 CSS 选择器的映射，至少包含「年份、专业、复试分数线」\n"
        " - output：输出 csv 文件名\n"
        "只输出 JSON 本体。"
    )
    cfg = run(GOAL)
    if cfg:
        try:
            generic_scraper.run("scraper_config.json")
        except Exception as e:
            print(f"\n抓取失败：{e}")
            print("提示：模型生成的 URL 是虚构/猜测的，替换成真实网页地址后重试。")

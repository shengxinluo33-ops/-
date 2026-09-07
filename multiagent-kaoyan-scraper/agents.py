# -*- coding: utf-8 -*-
"""两个 Agent 的能力封装：DeepSeek 负责产出，Kimi 负责评审。

两家都是 OpenAI 兼容接口，所以统一用 openai SDK，只换 base_url / key / model。
"""
import time

from openai import OpenAI
import config

developer = OpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL)
reviewer = OpenAI(api_key=config.KIMI_API_KEY, base_url=config.KIMI_BASE_URL)

DEVELOPER_SYSTEM = (
    "你是爬虫开发工程师，根据任务生成爬虫配置（JSON）。"
    "只输出 JSON 本体，不要用 Markdown 代码块包裹，不要加任何解释。"
)

REVIEWER_SYSTEM = (
    "你是资深爬虫评审，审查下面这份爬虫配置 JSON。"
    "检查：字段是否合理、CSS 选择器是否可能失效、是否缺少必要字段、URL 是否合理。"
    "若没问题，第一句回复「通过」；有问题则逐条列出具体修改意见。"
)


def _chat(client, model, messages, retries=3):
    """带限流重试的调用：Kimi 免费/低档 key 的 RPM 很低，撞到 429 就退避重试。"""
    last = None
    for attempt in range(retries):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages
            )
            return resp.choices[0].message.content
        except Exception as e:
            last = e
            # 只有限流（429）才重试；认证/模型错误直接抛
            if "429" not in str(e) and "rate" not in str(e).lower():
                raise
            wait = 60
            print(f"  [限流] 等待 {wait}s 后重试...")
            time.sleep(wait)
    raise last


def ask_developer(messages):
    return _chat(
        developer,
        config.DEEPSEEK_MODEL,
        [{"role": "system", "content": DEVELOPER_SYSTEM}] + messages,
    )


def ask_reviewer(messages):
    return _chat(
        reviewer,
        config.KIMI_MODEL,
        [{"role": "system", "content": REVIEWER_SYSTEM}] + messages,
    )

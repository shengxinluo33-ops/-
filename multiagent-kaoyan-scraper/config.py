# -*- coding: utf-8 -*-
"""配置：从环境变量读取 API Key，绝不硬编码到代码里。"""
import os

# ---- DeepSeek（开发者 / 写代码角色） ----
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"

# ---- Kimi / Moonshot（评审角色） ----
KIMI_API_KEY = os.environ.get("KIMI_API_KEY", "")
KIMI_BASE_URL = "https://api.moonshot.cn/v1"   # 国内版域名（.cn，不是 .ai）
KIMI_MODEL = "kimi-k3"

# -*- coding: utf-8 -*-
"""抓取 2025 考研国家线并输出干净 CSV。

国家线表格含合并单元格（两层表头 + rowspan），通用框架的简单 CSS 选择器
处理不了，这里用 pandas.read_html 自动展开 rowspan/colspan。
"""
import io

import requests
import pandas as pd

URL = "https://mtoutiao.xdf.cn/kaoyan/202510/14968606.html"
OUT = "2025考研国家线.csv"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

r = requests.get(URL, headers=HEADERS, timeout=15)
df = pd.read_html(io.BytesIO(r.content), flavor="lxml", header=[0, 1])[0]

# 列顺序固定：0学科门类 1学科专业 2~4 A类三列 5~7 B类三列 8少数民族 9备注
df = df.iloc[:, :9]
df.columns = [
    "学科门类", "学科专业",
    "A类总分", "A类单科(100)", "A类单科(>100)",
    "B类总分", "B类单科(100)", "B类单科(>100)",
    "少数民族骨干总分",
]
# 学科门类这一列因 rowspan 合并，被合并的行是空值，向下填充
df["学科门类"] = df["学科门类"].ffill()

df.to_csv(OUT, index=False, encoding="utf-8-sig")
print(df.to_string(index=False))
print(f"\n已保存 -> {OUT}（共 {len(df)} 行）")

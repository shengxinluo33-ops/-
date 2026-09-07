# -*- coding: utf-8 -*-
"""通用可配置爬虫框架：填 URL + CSS 选择器即可爬数据存 CSV。

配置文件（JSON）格式：
{
    "url": "目标网页地址",
    "container": "每条数据记录的 CSS 选择器（如 table tr）",
    "fields": {"字段名": "该字段相对 container 的 CSS 选择器（如 td:nth-child(1)）"},
    "output": "输出 CSV 文件名"
}
"""
import csv
import json

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def fetch(url, timeout=15):
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def parse(html, cfg):
    soup = BeautifulSoup(html, "lxml")
    items = soup.select(cfg["container"])
    records = []
    for item in items:
        rec = {}
        for name, sel in cfg["fields"].items():
            node = item.select_one(sel)
            rec[name] = node.get_text(strip=True) if node else ""
        if any(rec.values()):            # 跳过全空行
            records.append(rec)
    return records


def save(records, out_csv):
    if not records:
        print("未解析到数据，请检查 container / fields 选择器。")
        return
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)
    print(f"已保存 {len(records)} 条数据 -> {out_csv}")


def run(config_path):
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    print(f"抓取: {cfg['url']}")
    html = fetch(cfg["url"])
    records = parse(html, cfg)
    save(records, cfg.get("output", "output.csv"))
    return records


if __name__ == "__main__":
    import sys
    run(sys.argv[1] if len(sys.argv) > 1 else "config.json")

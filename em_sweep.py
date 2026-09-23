import json, urllib.parse, subprocess, re, sys, time
from em_search import em_search

KWS = [
 "中际旭创 目标价",
 "中际旭创 摩根士丹利",
 "中际旭创 摩根大通",
 "中际旭创 野村",
 "中际旭创 花旗",
 "中际旭创 美银",
 "中际旭创 瑞银",
 "中际旭创 招银国际",
 "中际旭创 交银国际",
 "中际旭创 大和",
 "中际旭创 麦格理",
 "中际旭创 巴克莱",
 "中际旭创 里昂",
 "中际旭创 汇丰",
 "中际旭创 中金",
 "中际旭创 海通国际",
 "中际旭创 高盛 研报",
 "中际旭创 评级",
 "中际旭创 光模块 关税",
 "中际旭创 光模块 禁令",
 "中际旭创 CPO",
 "光模块 LightCounting",
 "光模块 Dell'Oro",
 "光模块 1.6T 出货 预测",
 "Innolight",
]

seen = {}
for kw in KWS:
    print("=====", kw)
    try:
        res = em_search(kw, 25)
    except Exception as e:
        print("  ERR", e); continue
    if isinstance(res, str):
        print("  ERR", res); continue
    for x in res:
        u = x["url"]
        key = u
        if key in seen: continue
        seen[key] = 1
        print(f"  {x['date']} | {x['media']} | {x['title']}\n     {u}\n     {x['content'][:220]}")
    time.sleep(0.4)

import asyncio, sys
from app.tools import web_search as W

QUERIES = [
 '摩根士丹利 中际旭创 目标价 650元 上调 超配',
 '野村 中际旭创 目标价 1375元 上调 买入',
 '摩根大通 中际旭创 目标价 366元 研报',
 '花旗 中际旭创 目标价 569元 买入 三季报',
 '美银证券 中际旭创 目标价 780元 上调',
 '高盛 中际旭创 目标价 1187元 791元 上调 4月',
 '高盛 中际旭创 目标价 2581元 1187元 上调 7月17日',
 '中际旭创 大摩 摩根士丹利 AI光模块 TAM 1020亿美元',
 '中际旭创 高盛 光模块 出货预期 上修 2026年9月',
 '中际旭创 CPO 替代 风险 争议 2026',
 '中际旭创 H股 上市 980港元 2026',
 '中际旭创 招银国际 目标价 1400元 2026年8月',
]

def is_direct(u):
    return not any(k in u for k in ('so.com/link','baidu.com/link','search/jump','news.so.com','ai.so.com','so.toutiao.com'))

async def main():
    for q in QUERIES:
        print('====', q)
        try:
            r = await W.web_search(q, 8)
        except Exception as e:
            print('  ERR', e); continue
        for x in r:
            u = x.get('url','')
            tag = 'D' if is_direct(u) else '-'
            print(f'  [{tag}] {x.get("title","")}\n       {u}\n       {(x.get("snippet") or "")[:230]}')

asyncio.run(main())

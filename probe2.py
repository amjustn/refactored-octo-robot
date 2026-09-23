import asyncio, sys
from app.tools import web_search as W

QUERIES = [
 'site:finance.sina.com.cn 中际旭创 高盛 目标价',
 'site:cls.cn 中际旭创 高盛 目标价',
 'site:caifuhao.eastmoney.com 中际旭创 高盛 目标价 2645',
 'site:wallstreetcn.com 中际旭创 光模块',
 '中际旭创 高盛 目标价 2645元 财联社',
 '中际旭创 摩根士丹利 目标价 650元 上调',
 '中际旭创 野村 目标价 1325元 上调',
 '中际旭创 花旗 目标价 上调 2026年',
 '中际旭创 美银 目标价 780元',
 '中际旭创 摩根大通 目标价 增持 2026年',
 'Reuters optical transceivers China ban data centers Innolight',
 'LightCounting optical transceiver market 2026 forecast 1.6T',
 "Dell'Oro Group optical transceiver market 2026 800G",
 'TrendForce 800G 1.6T optical transceiver 2026',
 '中际旭创 H股 招银国际 买入 目标价 2026',
]

def is_direct(u):
    return not ('so.com/link' in u or 'baidu.com/link' in u or 'search/jump' in u or 'news.so.com' in u)

async def main():
    for q in QUERIES:
        print('====', q)
        try:
            r = await W.web_search(q, 8)
        except Exception as e:
            print('  ERR', e); continue
        for x in r:
            u = x.get('url',''); t = x.get('title','')
            tag = 'DIRECT' if is_direct(u) else 'redir'
            print(f'  [{tag}] {t} || {u}')
            print('       ', (x.get('snippet') or '')[:200])

asyncio.run(main())

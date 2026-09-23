import asyncio, httpx, sys
from app.tools import web_search as W

async def main():
    qs = ['Reuters Trump administration draft ban Chinese optical transceivers data centers',
          'Innolight top pick 2026 optical transceiver JPMorgan Morgan Stanley',
          '中际旭创 高盛 目标价 1187元 2026年4月',
          '中际旭创 高盛 目标价 2581元 上调',
          '中际旭创 招银国际 目标价 买入 2026年8月']
    async with httpx.AsyncClient(follow_redirects=False, timeout=15) as c:
        for q in qs:
            print('====', q)
            try:
                r = await W.web_search(q, 8)
            except Exception as e:
                print('ERR', e); continue
            for x in r:
                u = x.get('url','')
                t = x.get('title','')
                sn = (x.get('snippet') or '')[:200]
                final = u
                if 'link?url=' in u or 'search/jump' in u:
                    try:
                        resp = await c.get(u, headers={'User-Agent':'Mozilla/5.0'})
                        loc = resp.headers.get('location','')
                        if loc: final = loc
                    except Exception:
                        pass
                print(t, '||', final)
                print('   ', sn)

asyncio.run(main())

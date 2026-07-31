#!/usr/bin/env python3
"""Debug HTTP API responses."""
import sys
sys.path.insert(0, "/home/q/ai_berkshire_web")

import asyncio
import httpx

async def debug_stooq():
    print("=== Stooq debug ===")
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        # Try different URL formats
        urls = [
            "https://stooq.com/q/l/?s=aapl.us&f=sd2t2ohlcvn&h&e=csv",
            "https://stooq.com/q/l/?s=aapl&f=sd2t2ohlcvn&h&e=csv",
            "https://stooq.com/q/?s=aapl.us",
            "https://stooq.com/q/l/?s=aapl.us&f=sd2t2ohlcv&h&e=csv",
        ]
        for url in urls:
            try:
                resp = await client.get(url)
                print(f"  URL: {url}")
                print(f"  Status: {resp.status_code}")
                print(f"  Body: {resp.text[:200]}")
                print()
            except Exception as e:
                print(f"  URL: {url} -> ERROR: {e}")
                print()

async def debug_tencent_indices():
    print("=== Tencent indices debug ===")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get("http://qt.gtimg.cn/q=sh000001,sz399001,sz399006")
        print(f"  Status: {resp.status_code}")
        text = resp.text
        print(f"  Raw text (first 500): {text[:500]}")
        print()
        # Try splitting differently
        entries = text.strip().split(';')
        for i, entry in enumerate(entries):
            entry = entry.strip()
            if not entry:
                continue
            print(f"  Entry {i}: {entry[:200]}")

async def main():
    await debug_stooq()
    await debug_tencent_indices()

asyncio.run(main())

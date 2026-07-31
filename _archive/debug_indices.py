#!/usr/bin/env python3
"""Debug fetch_market_indices specifically."""
import sys
sys.path.insert(0, "/home/q/ai_berkshire_web")

import asyncio


async def debug_indices():
    print("=== Direct test: fetch_indices_tencent() ===")
    from app.tools.market_data_http import fetch_indices_tencent, fetch_indices_tencent_global, close_client
    
    data = await fetch_indices_tencent()
    print(f"  Result: {data}")
    
    data2 = await fetch_indices_tencent_global()
    print(f"  Global: {data2}")
    
    await close_client()

    print("\n=== Test: fetch_market_indices() with fresh cache ===")
    import os
    cache_path = "/home/q/ai_berkshire_web/data/cache.db"
    if os.path.exists(cache_path):
        os.remove(cache_path)
        print(f"  Removed cache: {cache_path}")
    
    from app.tools.market_data import fetch_market_indices, _cache
    result = await fetch_market_indices()
    print(f"  Keys: {list(result.keys())}")
    real_keys = [k for k in result.keys() if not k.startswith("_") and k != "timestamp"]
    print(f"  Real keys: {real_keys}")
    for k in real_keys:
        v = result[k]
        if isinstance(v, dict):
            print(f"  {k}: {v.get('name', v.get('symbol', '?'))} = {v.get('price', 0)}")
    
    if not real_keys:
        print("  ❌ No real index data!")
        print(f"  Full result: {result}")
    
    from app.tools.market_data_http import close_client as cc
    await cc()


asyncio.run(debug_indices())

#!/usr/bin/env python3
"""Smoke test for HTTP fallback module — all tests in one event loop."""
import sys
sys.path.insert(0, "/home/q/ai_berkshire_web")

import asyncio


async def run_all_tests():
    print("=== Testing HTTP fallback module imports ===")
    try:
        from app.tools.market_data_http import (
            fetch_a_share_price_tencent,
            fetch_a_share_price_sina,
            fetch_news_eastmoney,
            fetch_news_sina,
            fetch_indices_tencent,
            fetch_indices_yahoo,
            fetch_us_hk_price_yahoo,
            get_client,
            close_client,
        )
        print("  All HTTP fallback imports: OK")
    except Exception as e:
        print(f"  Import FAILED: {e}")
        return

    print("\n=== Testing bootstrap module imports ===")
    try:
        from app.core.bootstrap import check_dependencies, health_check_data_sources
        print("  Bootstrap imports: OK")
    except Exception as e:
        print(f"  Bootstrap import FAILED: {e}")

    print("\n=== Testing market_data.py imports (with HTTP fallback) ===")
    try:
        from app.tools.market_data import get_stock_price, fetch_company_news, fetch_market_indices
        print("  market_data imports: OK")
    except Exception as e:
        print(f"  market_data import FAILED: {e}")

    print("\n=== Testing main.py imports ===")
    try:
        from app.main import app
        print(f"  main.py imports: OK (app title={app.title})")
        routes = [r.path for r in app.routes]
        if "/api/health/data-sources" in routes:
            print("  /api/health/data-sources route: OK")
        else:
            print("  /api/health/data-sources route: MISSING!")
    except Exception as e:
        print(f"  main.py import FAILED: {e}")

    print("\n=== Testing dependency check ===")
    try:
        deps = check_dependencies()
        for name, info in deps.items():
            print(f"  {name}: {info['status']}" + (f" v{info.get('version', '')}" if info['status'] == 'ok' else ""))
    except Exception as e:
        print(f"  Dependency check FAILED: {e}")

    print("\n=== Testing Tencent HTTP price (live) ===")
    try:
        data = await fetch_a_share_price_tencent("600519.SH")
        if data:
            print(f"  Tencent HTTP: OK - {data.get('name', '?')} price={data.get('price', 0)}")
        else:
            print("  Tencent HTTP: returned None (market may be closed)")
    except Exception as e:
        print(f"  Tencent HTTP: FAILED - {e}")

    print("\n=== Testing Yahoo Finance HTTP price (live) ===")
    try:
        data = await fetch_us_hk_price_yahoo("AAPL")
        if data:
            print(f"  Yahoo HTTP: OK - {data.get('name', '?')} price={data.get('price', 0)}")
        else:
            print("  Yahoo HTTP: returned None")
    except Exception as e:
        print(f"  Yahoo HTTP: FAILED - {e}")

    print("\n=== Testing Tencent indices (live) ===")
    try:
        data = await fetch_indices_tencent()
        if data:
            for k, v in data.items():
                print(f"  {k}: {v.get('name', '?')} price={v.get('price', 0)}")
        else:
            print("  Tencent indices: returned None")
    except Exception as e:
        print(f"  Tencent indices: FAILED - {e}")

    print("\n=== Testing Yahoo Finance indices (live) ===")
    try:
        data = await fetch_indices_yahoo()
        if data:
            for k, v in data.items():
                print(f"  {k}: price={v.get('price', 0)} change={v.get('change_pct', 0)}%")
        else:
            print("  Yahoo indices: returned None")
    except Exception as e:
        print(f"  Yahoo indices: FAILED - {e}")

    print("\n=== Testing full get_stock_price with fallback (live) ===")
    try:
        # This should work even without akshare installed, via HTTP fallback
        data = await get_stock_price("600519.SH")
        if data and "error" not in data:
            print(f"  get_stock_price(600519.SH): OK - {data.get('name', '?')} price={data.get('price', 0)} source={data.get('_source', 'primary')}")
        elif data and "error" in data:
            print(f"  get_stock_price(600519.SH): ERROR - {data.get('error', '?')}")
        else:
            print("  get_stock_price(600519.SH): returned None")
    except Exception as e:
        print(f"  get_stock_price FAILED: {e}")

    try:
        data = await get_stock_price("AAPL")
        if data and "error" not in data:
            print(f"  get_stock_price(AAPL): OK - {data.get('name', '?')} price={data.get('price', 0)} source={data.get('_source', 'primary')}")
        elif data and "error" in data:
            print(f"  get_stock_price(AAPL): ERROR - {data.get('error', '?')}")
        else:
            print("  get_stock_price(AAPL): returned None")
    except Exception as e:
        print(f"  get_stock_price(AAPL) FAILED: {e}")

    print("\n=== Testing fetch_market_indices with fallback (live) ===")
    try:
        data = await fetch_market_indices()
        keys = [k for k in data.keys() if not k.startswith("_")]
        print(f"  fetch_market_indices: OK - keys={keys}")
        if data.get("_source"):
            print(f"  source={data['_source']}")
    except Exception as e:
        print(f"  fetch_market_indices FAILED: {e}")

    # Cleanup
    await close_client()

    print("\n=== All tests complete! ===")


asyncio.run(run_all_tests())

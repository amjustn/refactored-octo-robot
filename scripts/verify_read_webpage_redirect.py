#!/usr/bin/env python3
"""read_webpage 跳转壳页跟进 —— 用 2026-09-13 端到端留痕里的真实失败 URL 复测。

现场：模型对搜索引擎结果里的 so.com/link?m=... 调 read_webpage，返回 200 但只有
JS 跳转壳页 → "Could not extract text content from page"（3 次里 2 次），
第 3 次撞上熔断器 circuit open，连东财公告正文都读不到。

用法: venv/bin/python scripts/verify_read_webpage_redirect.py
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
FAILED: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [FAIL] {name} {detail}")


# 端到端留痕里真实失败的 3 个 URL
SO_LINK_1 = "https://www.so.com/link?m=wJA4lyOHje8wu1S2so%2Ft%2BXYgKBkuFXz74RZxVXjvttuQ0iwWg9zj0sbAA9pZtuAwOhPDHim8qmqm25lOotJprUKR1VOOoPPOcO14KsQ3vZy%2FZrN8HoxK%2BNZO8JbgnFD6Qv4HGFk8LygZNR3WUZKfsZBIAt8Utjxo71KsI2sZS%2FQToa2oA3OoromjngUs7pM%2FFn0F%2F9sjs%2FDubStVyB6rX6zzKvJ7Bud0ig9u3U1sic1KL988cCt9saXTpQb%2BUqlEY7wYnGD5hBBDQc4kV4cGPh4R5ypt1aL9OApt0Qvr79jpw%2FPUKECLhZ4yP7dXzF7QWSbhNKJQeKPxGEV7hKVitH7xXnqGlf8kH6uueV88ZLmjwPgwln3Ywd74Fd%2FI%2FcMCGdGEQhg%3D%3D"
SO_LINK_2 = "https://www.so.com/link?m=bEDHOJzw%2BjLrt9cr4jAM7QCmE%2BwA3Yf0eU7LYov4024zE3u1Xxg%2FB%2FeRW4gZdQmBTuJvFhgZWWLyhYlD0vJ7dsRqeEs9Ln%2Fx52FHYsEoSR18vBGi7BrBtXB%2BtIgUqNkaPf0OMTDyhIrzX8u8DEnLybO3cpFYZRUQ2p6U4l7v7dFgGA1PM3zix%2B8EJEBic%2BoLAo%2BiFTeiqq%2FP9aLEdUbLyDH3XkHLzvMxdMiB0Utw3zQRE1yK79DcZJcAIkMkMT98qmOkRyGLQiqvmlhLn5n2GFi%2FvrtkY%2B6KgT9mGKlJaMsBkTjg7kWBx%2F83GVk20djmqOTFk35UM1sY4m4mlZKE5WHSMXvZ0oubf4IF%2FSb9yxTxH3IBDZbVGq5IHruh5iFZapKcGSg%3D%3D"
EM_NOTICE = "https://data.eastmoney.com/notices/detail/688008/AN202609101829231605.html"
TENCENT = "https://www.cninfo.com.cn/new/disclosure/detail?stockCode=688008&announcementId=1225530486&orgId=9900039002&announcementTime=2026-08-29"


def part_a_static() -> None:
    from app.tools.web_search import _extract_page_text, _extract_redirect_target

    print("\n=== A. 离线：跳转目标解析与正文提取 ===")
    html_js = """<html><head><title>跳转中</title></head><body>
    <script>window.location.replace("https://example.com/real/article.html");</script>
    </body></html>"""
    check("A1 JS 跳转壳页能取出目标",
          _extract_redirect_target(html_js, "https://www.so.com/link?m=x") == "https://example.com/real/article.html")

    html_meta = """<html><head><meta http-equiv="refresh" content="0;url=https://finance.sina.com.cn/a.html"></head><body>go</body></html>"""
    check("A2 meta refresh 能取出目标",
          _extract_redirect_target(html_meta, "https://www.so.com/link?m=x") == "https://finance.sina.com.cn/a.html")

    html_loc = """<script>location.href="/news/12345.html"</script>"""
    check("A3 相对跳转目标按 base 补全",
          _extract_redirect_target(html_loc, "https://www.sogou.com/link?url=x") == "https://www.sogou.com/news/12345.html")

    check("A4 普通正文页不误判为跳转", _extract_redirect_target("<html><body><p>正文内容</p></body></html>", "https://a.com/x") == "")
    check("A5 空 HTML 不抛异常", _extract_redirect_target("", "https://a.com/x") == "")

    t, txt = _extract_page_text("<html><head><title>标题A</title></head><body><script>x=1</script><p>正文一二三四五六七八九十</p></body></html>")
    check("A6 正文提取：标题与文本都在", t == "标题A" and "正文一二三四五六七八九十" in txt, f"{t!r}/{txt[:20]!r}")
    check("A7 script/style 内容被剔除", "x=1" not in txt)


async def part_b_live() -> None:
    from app.tools.web_search import read_webpage

    print("\n=== B. 联网：真实失败 URL 复测 ===")
    for label, url, name in (
        ("B1 so.com 跳转链接#1", SO_LINK_1, "第一条 360 跳转链接"),
        ("B2 so.com 跳转链接#2", SO_LINK_2, "第二条 360 跳转链接"),
    ):
        try:
            r = await read_webpage(url, max_chars=1500)
            ok = bool(r.get("content"))
            check(f"{label} 能取到正文（跟进跳转后）", ok,
                  f"error={r.get('error')} url={r.get('url','')[:70]}")
            if ok:
                print(f"        -> 最终 URL: {r.get('url','')[:90]}")
                print(f"        -> 标题: {r.get('title','')[:60]} | {r.get('word_count')} 字符")
        except Exception as e:
            check(f"{label} 能取到正文（跟进跳转后）", False, f"{type(e).__name__}: {e}")

    try:
        r = await read_webpage(EM_NOTICE, max_chars=1200)
        check("B3 东财公告页可读（熔断不再被解析失败堵死）", bool(r.get("content")),
              f"error={r.get('error')}")
        if r.get("content"):
            print(f"        -> 标题: {r.get('title','')[:70]} | {r.get('word_count')} 字符")
            print(f"        -> 片段: {r.get('content','')[:120]}")
    except Exception as e:
        check("B3 东财公告页可读（熔断不再被解析失败堵死）", False, f"{type(e).__name__}: {e}")

    try:
        r = await read_webpage(TENCENT, max_chars=800)
        check("B4 巨潮公告链接可读（当初读不到的正是它）", bool(r.get("content")) or bool(r.get("title")),
              f"error={r.get('error')}")
        if r.get("title"):
            print(f"        -> 标题: {r.get('title','')[:80]}")
    except Exception as e:
        check("B4 巨潮公告链接可读（当初读不到的正是它）", False, f"{type(e).__name__}: {e}")


def part_c_breaker() -> None:
    from app.harness.tools import _is_business_error

    print("\n=== C. 熔断器分类（解析失败不应开闸） ===")
    check("C1 'Could not extract text content' 判为业务性失败",
          _is_business_error("Could not extract text content from page"))
    check("C2 超限响应体判为业务性失败", _is_business_error("响应体超过 2MB 上限，已拒绝"))
    check("C3 URL 被拒判为业务性失败", _is_business_error("URL 被拒绝: 内网地址"))
    check("C4 真实网络错误仍算故障（会开闸）",
          not _is_business_error("timeout after 20s") and not _is_business_error("连接失败: Connection reset"))


def main() -> int:
    part_a_static()
    asyncio.run(part_b_live())
    part_c_breaker()
    print("\n" + "=" * 56)
    print(f"PASS {PASS} / FAIL {FAIL}")
    if FAILED:
        print("失败项：" + "; ".join(FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

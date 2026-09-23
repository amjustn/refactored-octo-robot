import json, urllib.parse, subprocess, re, sys

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"

def curl(url, headers=None):
    cmd = ["curl", "-s", "-L", "--max-time", "30", "-A", UA]
    for h in (headers or []):
        cmd += ["-H", h]
    cmd.append(url)
    return subprocess.run(cmd, capture_output=True, text=True, errors="ignore").stdout

def reports(code="300308", begin="2026-01-01", end="2026-09-21", size=100, qtype=0):
    p = {
        "cb": "cb", "pageSize": size, "pageNo": 1, "beginTime": begin, "endTime": end,
        "code": code, "industryCode": "*", "rating": "", "ratingChange": "",
        "fields": "", "qType": qtype,
    }
    url = "https://reportapi.eastmoney.com/report/list?" + urllib.parse.urlencode(p)
    txt = curl(url, ["Referer: https://data.eastmoney.com/report/"])
    m = re.search(r'cb\((.*)\)\s*$', txt.strip(), re.S)
    if not m:
        return txt[:400]
    d = json.loads(m.group(1))
    out = []
    for it in d.get("data", []):
        out.append({
            "date": (it.get("publishDate") or "")[:10],
            "org": it.get("orgSName") or it.get("orgName"),
            "title": it.get("title"),
            "rating": it.get("emRatingName") or it.get("sRatingName"),
            "url": "https://data.eastmoney.com/report/zw_stock.jshtml?infocode=" + (it.get("infoCode") or ""),
            "pdf": it.get("attachPdf"),
        })
    return out

if __name__ == "__main__":
    args = sys.argv[1:]
    begin = args[0] if args else "2026-01-01"
    end = args[1] if len(args) > 1 else "2026-09-21"
    r = reports(begin=begin, end=end)
    if isinstance(r, str):
        print("ERR", r)
    else:
        print("count", len(r))
        for x in r:
            print(f"{x['date']} | {x['org']} | {x['rating']} | {x['title']}\n   {x['url']}\n   {x['pdf']}")

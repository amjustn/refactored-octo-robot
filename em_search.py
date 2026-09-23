import json, urllib.parse, subprocess, re, sys

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

def curl(url, extra=None):
    cmd = ["curl", "-s", "-L", "--max-time", "30", "-A", UA]
    if extra: cmd += extra
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, errors="ignore")
    return r.stdout

def em_search(kw, count=20, typ="cmsArticleWebOld"):
    param = {
        "uid": "", "keyword": kw, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default",
                                       "pageIndex": 1, "pageSize": count, "preTag": "", "postTag": ""}}
    }
    url = "https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param=" + urllib.parse.quote(json.dumps(param, ensure_ascii=False))
    txt = curl(url, ["-H", "Referer: https://so.eastmoney.com/"])
    m = re.search(r'^cb\((.*)\)\s*$', txt.strip(), re.S)
    if not m:
        return txt[:300]
    d = json.loads(m.group(1))
    out = []
    for it in d.get("result", {}).get("cmsArticleWebOld", []):
        out.append({
            "title": re.sub('<[^>]+>', '', it.get("title", "")),
            "date": it.get("date", ""),
            "url": it.get("url", ""),
            "media": it.get("mediaName", ""),
            "content": re.sub('<[^>]+>', '', it.get("content", ""))[:300],
        })
    return out

if __name__ == "__main__":
    for kw in sys.argv[1:]:
        print("=====", kw)
        res = em_search(kw)
        if isinstance(res, str):
            print("  ERR", res); continue
        for x in res:
            print(f"  {x['date']} | {x['media']} | {x['title']}\n     {x['url']}\n     {x['content']}")

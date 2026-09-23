import subprocess, re, html, sys

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"

def txt(url):
    r = subprocess.run(["curl","-s","-L","--max-time","30","-A",UA,url], capture_output=True, text=True, errors="ignore")
    t = r.stdout
    t = re.sub(r'(?is)<(script|style).*?</\1>',' ',t)
    t = re.sub(r'(?s)<[^>]+>',' ',t)
    t = html.unescape(t)
    t = re.sub(r'\s+',' ',t)
    i = t.find('研报正文')
    return t[i:i+4000] if i>0 else t[:4000]

for code in sys.argv[1:]:
    print("="*20, code)
    print(txt("https://data.eastmoney.com/report/zw_stock.jshtml?infocode="+code))
    print()

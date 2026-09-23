import httpx

H = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120'}

URLS = [
 ("GS A2645/H3267 财联社", "http://finance.eastmoney.com/a/202609073866642193.html", "3267"),
 ("GS A 2645 券商中国", "http://finance.eastmoney.com/a/202609073866816617.html", "2645"),
 ("Citi H 1524 财联社", "http://finance.eastmoney.com/a/202609073866625662.html", "1524"),
 ("GS H 3267 hk.eastmoney", "http://hk.eastmoney.com/a/202609073866214095.html", "3267"),
 ("目标价逼近3000元", "http://finance.eastmoney.com/a/202609073866798878.html", "3000"),
 ("光模块采购预测上修", "http://finance.eastmoney.com/a/202609163875895763.html", "交银"),
 ("GS 海外研选0907", "http://finance.eastmoney.com/a/202609073866770650.html", "中际旭创"),
 ("GS 上修TAM 界面", "http://stock.eastmoney.com/a/202609163875985888.html", "TAM"),
 ("GS 15家企业 走访", "http://stock.eastmoney.com/a/202609173877107946.html", "15家"),
 ("大摩高盛增持H股", "http://finance.eastmoney.com/a/202608213849352681.html", "摩根士丹利"),
 ("大摩降至6.55%", "http://finance.eastmoney.com/a/202609013861541771.html", "6.55"),
 ("大摩9/11降至4.32%", "http://finance.eastmoney.com/a/202609163876105771.html", "4.32"),
 ("大摩升至5.95%", "http://finance.eastmoney.com/a/202609143873529123.html", "5.95"),
 ("JPM 14.01%", "http://finance.eastmoney.com/a/202609103870635385.html", "14.01"),
 ("FCC禁令虚惊一场", "http://finance.eastmoney.com/a/202609113872235424.html", "FCC"),
 ("风口上的光模块 分歧", "http://finance.eastmoney.com/a/202609173877468435.html", "分歧"),
 ("交银国际 2026-09-02", "https://data.eastmoney.com/report/zw_stock.jshtml?infocode=AP202609021828904437", "1488"),
 ("交银国际 2026-07-22", "https://data.eastmoney.com/report/zw_stock.jshtml?infocode=AP202607221827251189", "1600"),
 ("招银国际 2026-04-17", "https://data.eastmoney.com/report/zw_stock.jshtml?infocode=AP202604171821281745", "1,080"),
 ("招银国际 2026-04-01", "https://data.eastmoney.com/report/zw_stock.jshtml?infocode=AP202604011820937277", "707"),
 ("招银国际 2026-02-05", "https://data.eastmoney.com/report/zw_stock.jshtml?infocode=AP202602051819789964", "707"),
 ("招银国际 2026-08-24", "https://data.eastmoney.com/report/zw_stock.jshtml?infocode=AP202608241828350956", "招银国际"),
 ("招银国际 2025-12-15", "https://data.eastmoney.com/report/zw_stock.jshtml?infocode=AP202512151800802998", "707"),
 ("LC Mar2026 QMU", "https://www.lightcounting.com/newsletter/en/march-2026-quarterly-market-update-380", "23.8"),
 ("LC Jan2026 AI clusters", "https://www.lightcounting.com/newsletter/en/january-2026-optics-for-ai-clusters-366", "26 billion"),
 ("LC Sep2026 QMU", "https://www.lightcounting.com/newsletter/en/september-2026-quarterly-market-update-371", "NPO"),
 ("LC Mar2026 Ethernet Optics", "https://www.lightcounting.com/newsletter/en/march-2026-ethernet-optics-382", "100 billion"),
 ("DellOro DC capex 2Q26", "https://www.delloro.com/news/data-center-capex-grew-92-percent-in-2q-2026-driven-by-surging-ai-demand-and-memory-costs/", "92"),
 ("GS weixin 2645", "https://mp.weixin.qq.com/s?src=11&timestamp=1789992852&ver=6980&signature=aMLtIECZZjzf23Q1ybMQo2TeuT7r3Iifc5aPfiyThyk9woFy4*bw4SuP4QH3ygg3j*-ElGxVGTwyjNGwFYyvTK8U3NWhfnKI4Xx3rhwGH*ExjIMe*-PGNGhzKUPB9oQx&new=1", "2645"),
 ("GS timeline weixin", "https://mp.weixin.qq.com/s?src=11&timestamp=1789993380&ver=6980&signature=0pHxzBCKwvYBiVAfSMR5biTuTAxkO7*5pKMj1jR5fY8FL6zoG1Sgg35bIKicj1qE0Wkq3QCZXsqwsrDLtGv7SSoCXW409*DUBU-ftasML8v2Is4dvdR8sf7FL6H723PG&new=1", "2581"),
 ("360kuai GS 2581", "https://www.360kuai.com/pc/987c701bce357b658", "2581"),
 ("360kuai Citi 1524", "https://www.360kuai.com/pc/9e078665a767fc588", "1524"),
 ("360kuai GS H 3267", "https://www.360kuai.com/pc/93d063a7c6a96b26e", "3267"),
 ("360kuai JPM 增持", "https://www.360kuai.com/pc/922d2ae31b47b19aa", "中际旭创"),
 ("光模块拥挤度低", "http://stock.eastmoney.com/a/202609173877107946.html", "拥挤度"),
]

for name, u, kw in URLS:
    try:
        r = httpx.get(u, timeout=30, follow_redirects=True, headers=H)
        ok = kw in r.text
        print(f"{r.status_code} kw={'YES' if ok else 'NO '} len={len(r.text):>7} | {name}")
    except Exception as e:
        print(f"ERR  {name} | {e}")

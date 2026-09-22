"""Sector Knowledge Base — A股板块知识库（申万一级 31 板块 × 4 视角）。

每个板块蒸馏 4 个不同立场/角度的知名机构或人物风格对该板块的
观察观点与研究方法：
  1. 买方趋势派  —— 知名公募/私募基金经理的赛道投资方法
  2. 深度价值派  —— 保守价值机构的安全边际定价方法
  3. 卖方研究派  —— 券商首席分析师的产业跟踪框架
  4. 风险逆向派  —— 空头/风控视角的证伪清单

与 skill_knowledge.py 同一模式：
  - 默认引导内容随代码发布（default_bootstrap），首次运行即落盘
  - LLM 每 7 天自动刷新（update_all_sector_knowledge）
  - 刷新失败保留旧数据，绝不静默清空
  - 注入格式化走 get_sector_knowledge_for_prompt()

板块识别（动态调用）在 app/tools/sector_router.py；本模块只管
注册表、存储、蒸馏与格式化。

知识文件：data/knowledge/sectors/{sector_key}.json

注意：representative 字段是「方法论风格」标注（公开已知的投资风格），
不是、也不得被当作该人物/机构的真实语录或持仓建议。
"""
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from ..core.config import BASE_DIR
from ..core.llm import chat_complete

logger = logging.getLogger("ai_berkshire.sector_knowledge")

SECTOR_KNOWLEDGE_DIR = BASE_DIR / "data" / "knowledge" / "sectors"
SECTOR_KB_REFRESH_INTERVAL = 7 * 86400  # 7 days

PERSPECTIVE_STANCES = ["买方趋势派", "深度价值派", "卖方研究派", "风险逆向派"]

# ==================== 板块注册表（申万一级 31 个） ====================
# key -> {name: 显示名, sw: 申万一级行业名, aliases: 路由别名（越长越具体越靠前匹配）}
SECTORS = {
    "nonglin": {"name": "农林牧渔", "sw": "农林牧渔",
                "aliases": ["农林牧渔", "猪周期", "生猪", "养殖", "牧原", "种业", "饲料", "白羽鸡", "农业"]},
    "huagong": {"name": "基础化工", "sw": "基础化工",
                "aliases": ["基础化工", "化工", "万华", "MDI", "钛白粉", "农药", "化肥", "磷化工", "氟化工", "纯碱"]},
    "gangtie": {"name": "钢铁", "sw": "钢铁",
                "aliases": ["钢铁", "螺纹钢", "特钢", "不锈钢", "宝钢"]},
    "youse": {"name": "有色金属", "sw": "有色金属",
              "aliases": ["有色金属", "有色", "铜", "铝", "黄金", "稀土", "锂矿", "小金属", "紫金"]},
    "dianzi": {"name": "电子（半导体/消费电子）", "sw": "电子",
               "aliases": ["半导体", "集成电路", "芯片", "晶圆", "封测", "光刻", "存储", "消费电子", "电子", "PCB", "EDA", "中芯国际"]},
    "jiadian": {"name": "家用电器", "sw": "家用电器",
                "aliases": ["家用电器", "家电", "白电", "厨电", "小家电", "美的", "格力", "海尔"]},
    "shipin": {"name": "食品饮料（白酒）", "sw": "食品饮料",
               "aliases": ["食品饮料", "白酒", "茅台", "五粮液", "啤酒", "乳制品", "调味品", "休闲食品"]},
    "fangzhi": {"name": "纺织服饰", "sw": "纺织服饰",
                "aliases": ["纺织服饰", "运动服饰", "服装", "纺织", "家纺", "安踏", "李宁"]},
    "qinggong": {"name": "轻工制造", "sw": "轻工制造",
                 "aliases": ["轻工制造", "轻工", "造纸", "包装", "家具", "家居", "文具"]},
    "yiyao": {"name": "医药生物", "sw": "医药生物",
              "aliases": ["医药生物", "创新药", "医药", "中药", "医疗器械", "CXO", "疫苗", "医疗服务", "生物药"]},
    "gongyong": {"name": "公用事业", "sw": "公用事业",
                 "aliases": ["公用事业", "电力", "火电", "水电", "核电", "绿电", "燃气", "水务", "长江电力"]},
    "jiaotong": {"name": "交通运输", "sw": "交通运输",
                 "aliases": ["交通运输", "航空", "机场", "航运", "快递", "物流", "港口", "铁路", "海运", "集运"]},
    "fangdichan": {"name": "房地产", "sw": "房地产",
                   "aliases": ["房地产", "地产", "房企", "万科", "保利", "物业"]},
    "shangmao": {"name": "商贸零售", "sw": "商贸零售",
                 "aliases": ["商贸零售", "零售", "百货", "超市", "免税", "跨境电商", "电商"]},
    "shehui": {"name": "社会服务", "sw": "社会服务",
               "aliases": ["社会服务", "酒店", "餐饮", "旅游", "教育", "人力资源", "免税零售"]},
    "zonghe": {"name": "综合", "sw": "综合", "aliases": ["综合类", "综合"]},
    "jiancai": {"name": "建筑材料", "sw": "建筑材料",
                "aliases": ["建筑材料", "建材", "水泥", "玻璃", "消费建材", "防水", "涂料", "海螺"]},
    "jianzhu": {"name": "建筑装饰", "sw": "建筑装饰",
                "aliases": ["建筑装饰", "建筑", "基建", "中字头", "装饰", "园林"]},
    "dianlishebei": {"name": "电力设备（新能源）", "sw": "电力设备",
                     "aliases": ["电力设备", "新能源", "锂电", "光伏", "储能", "风电", "电池", "宁德时代", "硅料", "组件", "固态电池"]},
    "guofang": {"name": "国防军工", "sw": "国防军工",
                "aliases": ["国防军工", "军工", "国防", "航空装备", "航天", "船舶", "兵器", "雷达", "导弹"]},
    "jisuanji": {"name": "计算机（AI/软件）", "sw": "计算机",
                 "aliases": ["计算机", "人工智能", "软件", "信创", "云计算", "大模型", "数据要素", "AI应用", "SaaS"]},
    "chuanmei": {"name": "传媒", "sw": "传媒",
                 "aliases": ["传媒", "游戏", "影视", "出版", "广告", "短剧"]},
    "tongxin": {"name": "通信（算力/光模块）", "sw": "通信",
                "aliases": ["通信", "光模块", "CPO", "算力", "运营商", "5G", "6G", "光通信", "数据中心", "IDC", "液冷"]},
    "yinhang": {"name": "银行", "sw": "银行",
                "aliases": ["银行", "城商行", "农商行", "招商银行", "工商银行"]},
    "feiyin": {"name": "非银金融", "sw": "非银金融",
               "aliases": ["非银金融", "非银", "券商", "证券", "保险", "期货", "信托", "中国平安", "东方财富"]},
    "qiche": {"name": "汽车", "sw": "汽车",
              "aliases": ["汽车", "新能源车", "智能汽车", "自动驾驶", "智驾", "比亚迪", "汽车零部件", "整车"]},
    "jixie": {"name": "机械设备（机器人）", "sw": "机械设备",
              "aliases": ["机械设备", "机械", "人形机器人", "机器人", "工程机械", "机床", "减速器", "丝杠", "自动化"]},
    "meitan": {"name": "煤炭", "sw": "煤炭",
               "aliases": ["煤炭", "动力煤", "焦煤", "中国神华"]},
    "shiyou": {"name": "石油石化", "sw": "石油石化",
               "aliases": ["石油石化", "石油", "石化", "炼化", "油服", "中海油", "中石油"]},
    "huanbao": {"name": "环保", "sw": "环保",
                "aliases": ["环保", "固废", "垃圾焚烧", "环卫", "节能"]},
    "meirong": {"name": "美容护理", "sw": "美容护理",
                "aliases": ["美容护理", "医美", "化妆品", "护肤", "彩妆", "珀莱雅", "爱美客"]},
}

# ==================== 默认板块知识（default_bootstrap） ====================
# 每个板块 4 个视角：stance / representative（方法论风格标注，非真实语录）/
# method（观察该板块的观点与方法）/ signals（关键跟踪信号）

def _p(rep: str, method: str, signals: str) -> dict:
    return {"representative": rep, "method": method, "signals": signals}


DEFAULT_SECTOR_KNOWLEDGE = {
    "dianzi": [
        _p("科技赛道基金经理风格（高仓位押注国产替代主线的公开风格）",
           "以「国产替代 + 周期反转」双主线布局，沿设计-制造-封测-设备材料环节找稼动率与库存周期的拐点，龙头优先。",
           "晶圆厂稼动率、存储现货价、半导体设备招标、北美半导体出货额、SIA 月度销售额"),
        _p("保守价值机构风格",
           "警惕高估值与资本开支过剩，只在周期底部、PB 接近历史低分位时介入，优先现金流稳定的模拟/功率龙头。",
           "PB 历史分位、研发费用资本化率、存货周转天数、商誉占净资产比"),
        _p("券商电子首席产业跟踪框架",
           "拆全球半导体销售额同比与费城半导体指数映射，跟踪下游（手机/PC/汽车/AI 服务器）拉货节奏与渠道库存。",
           "SIA 月度销售额、手机/PC 出货量、AI 服务器 BOM 成本、渠道库存周数"),
        _p("空头/风控视角",
           "盯三个杀伤点：出口管制升级、技术迭代掉队、估值透支；主题拥挤时坚决回避。",
           "实体清单与出口管制变化、先进制程进度、解禁与减持、板块成交占比过热"),
    ],
    "shipin": [
        _p("消费长持型私募风格（林园/但斌公开的「垄断+成瘾性+长期持有」方法论）",
           "只买有定价权的品牌龙头，把渠道库存与批价当作先于报表的核心变量，下跌中靠分红与提价权穿越周期。",
           "飞天茅台批价、渠道库存月数、经销商打款进度、动销与开瓶率"),
        _p("保守价值机构风格",
           "白酒是现金流机器，按 DCF + 股息率定价，等待系统性杀跌带来的错杀机会，不追估值扩张。",
           "PE/PB 历史分位、股息率、自由现金流、合同负债（预收款）"),
        _p("券商食品饮料首席框架",
           "拆「价-量-库存」三角，分价格带（高端/次高端/区域酒）跟踪宴席与商务需求，批价体系是景气先行指标。",
           "批价体系、宴席场次、次高端增速、经销商利润率"),
        _p("空头/风控视角",
           "盯人口结构与消费习惯变迁（年轻群体低度化）、政策变量（消费税/宴请新规）、库存周期反噬。",
           "批价破位下行、库存超 3 个月、消费税政策动向、餐饮收入增速"),
    ],
    "yiyao": [
        _p("医药主题基金经理风格（长坡厚雪+创新主线的公开风格）",
           "创新药出海（BD/license-out）+ 器械国产替代双主线，看管线厚度、临床数据质量与商业化兑现。",
           "BD 交易金额与首付款、IND/NDA 数量、医保谈判结果、FDA/NMPA 审批"),
        _p("保守价值机构风格",
           "集采出清后的仿制药/中药龙头按现金流定价，关注股息与回购，回避纯管线故事。",
           "集采中标价降幅、PE 分位、经营性现金流、账上净现金"),
        _p("券商医药首席框架",
           "分赛道（创新药/CXO/器械/中药/药店）拆政策周期与需求周期，医保基金收支是总闸门。",
           "医保基金支出增速、集采批次与规则、医疗设备招标、CXO 在手订单"),
        _p("空头/风控视角",
           "政策（集采/医保控费/医疗反腐）是最大变量，临床失败与商誉减值是个股雷区。",
           "医疗反腐动态、集采扩围品种、研发管线终止公告、应收账款增速"),
    ],
    "dianlishebei": [
        _p("新能源赛道基金经理风格",
           "渗透率逻辑 + 技术迭代（固态电池/HJT/钙钛矿），买供需格局最优环节的全球龙头。",
           "新能源车渗透率、动力电池装机量、光伏组件价格、碳酸锂价格"),
        _p("保守价值机构风格",
           "产能过剩行业里只找成本曲线最左端、资产负债表最稳的公司，等行业出清信号再加仓。",
           "行业产能利用率、单位盈利（单 Wh/单 W 净利）、资本开支增速、龙头市占率"),
        _p("券商电新首席框架",
           "拆「量-价-利」：装机/出货量 × 价格 × 单位盈利，跟踪主产业链周度价格与排产数据。",
           "硅料/硅片/电池片/组件价格、碳酸锂价、月度排产、出口数据"),
        _p("空头/风控视角",
           "产能内卷与价格战是常态，警惕技术路线颠覆与海外贸易壁垒（关税/本地化要求）。",
           "行业亏损面、美欧关税与本土化政策、技术路线切换信号、在建产能规模"),
    ],
    "yinhang": [
        _p("高股息策略机构风格（保险/红利基金配置方法）",
           "把银行当「类债券 + 期权」，核心看股息率相对 10 年国债的溢价与资产质量稳定性。",
           "股息率 vs 10Y 国债收益率、不良率、拨备覆盖率、净息差"),
        _p("保守价值机构风格",
           "PB-ROE 框架，买低估且资产质量真实的银行，回避地产与城投敞口过高的。",
           "PB 分位、ROE、关注类贷款占比、地产对公敞口"),
        _p("券商银行首席框架",
           "拆净息差（生息资产收益率 - 付息负债成本）+ 规模增速 + 中收 + 资产质量四因子。",
           "LPR 与存款利率调整、社融信贷结构、不良生成率、理财规模"),
        _p("空头/风控视角",
           "盯地产链风险传导、地方债务化解对息差的挤压、净息差跌破警戒线后的盈利下修。",
           "净息差环比、城投/地产不良生成、按揭提前还款、基金持仓分位"),
    ],
    "feiyin": [
        _p("券商板块趋势资金风格",
           "券商是「牛市旗手」高贝塔，行情启动期买弹性；保险看负债端改善 + 投资端弹性。",
           "日均成交额、两融余额、新开户数、NBV 增速"),
        _p("保守价值机构风格",
           "保险按 P/EV、券商按 PB 定价，买在情绪冰点与估值底部分位。",
           "P/EV、PB 分位、内含价值回报率、代理人数量企稳信号"),
        _p("券商非银首席框架",
           "券商拆经纪/投行/资管/自营四业务弹性；保险拆负债（新单/续期）+ 资产（利差）两端。",
           "成交额、IPO/再融资规模、10 年国债收益率、750 天移动平均国债曲线"),
        _p("空头/风控视角",
           "成交额萎缩时券商盈利下修快于预期；保险利差损是长期隐忧。",
           "成交额趋势、长端利率下行幅度、佣金费率政策、偿付能力充足率"),
    ],
    "fangdichan": [
        _p("政策博弈型资金风格",
           "买「政策底 → 市场底」的左侧反转，选幸存龙头与轻资产物管，严格止损。",
           "限购限贷放松节奏、百强房企销售、土地出让金、二手房挂牌量"),
        _p("保守价值机构风格",
           "NAV 折价 + 幸存者逻辑，只看净负债率低、土储集中一二线的央国企。",
           "NAV 折价率、净负债率、现金短债比、融资成本"),
        _p("券商地产首席框架",
           "拆「销售-拿地-开工-竣工」链条与政策工具箱（收储/城中村/白名单），高频成交定方向。",
           "30 城成交面积、百城土地溢价率、新开工/竣工、房贷利率"),
        _p("空头/风控视角",
           "人口与城镇化长周期向下，警惕债务违约蔓延与房价负反馈循环。",
           "房企美元债价格、违约名单扩散、新房价格环比、法拍房数量"),
    ],
    "qiche": [
        _p("智能汽车趋势资金风格",
           "智能化（智驾/座舱）是新一轮产品周期，买有爆款车型 + 智驾标签的车企与增量零部件。",
           "月度交付量、智驾渗透率、新车型周期、排产与订单"),
        _p("保守价值机构风格",
           "价格战行业里只买成本领先 + 规模效应的赢家，警惕增收不增利。",
           "单车利润、毛利率、产能利用率、经销商库存系数"),
        _p("券商汽车首席框架",
           "拆「销量 × ASP × 成本」，跟踪乘联会周度数据、出口与原材料成本。",
           "乘联会零售/批发、出口量、碳酸锂与钢铝价格、终端折扣"),
        _p("空头/风控视角",
           "价格战侵蚀全行业利润，智驾叙事可能证伪，警惕高估值新势力现金流断裂。",
           "降价频次、行业利润率、新势力现金储备、库存预警指数"),
    ],
    "jisuanji": [
        _p("TMT 基金经理风格",
           "买 AI 产业趋势的应用落地（Agent/办公/行业软件），核心看订单、ARR 与续费率。",
           "AI 应用用户数、SaaS 续费率、信创招标、大模型 API 调用量"),
        _p("保守价值机构风格",
           "计算机板块现金流差、估值贵是常态，只买有真实利润与回购的龙头。",
           "经营性现金流、PE 分位、应收账款/收入、回购金额"),
        _p("券商计算机首席框架",
           "拆「政策（信创/数据要素）+ 技术（AI）+ 下游 IT 预算」三驱动，订单领先收入。",
           "政府/央企 IT 招标、行业信息化支出、人员扩张、季度收入增速"),
        _p("空头/风控视角",
           "主题炒作期估值与业绩脱节，警惕商誉减值与减持潮。",
           "主题成交占比、商誉/净资产、重要股东减持、订单转化不及预期"),
    ],
    "tongxin": [
        _p("算力趋势资金风格",
           "AI 算力基础设施是最确定的「卖水人」，光模块/液冷/IDC 跟着北美云厂商资本开支走。",
           "北美四大云厂商 capex、800G/1.6T 光模块出货、英伟达指引、数据中心上架率"),
        _p("保守价值机构风格",
           "运营商是高股息压舱石；设备商警惕在周期顶部追高。",
           "运营商股息率、ARPU、资本开支/收入、PE 分位"),
        _p("券商通信首席框架",
           "拆运营商（2C/2H/2B）+ 设备（传输/数通/无线）两条线，重视海外映射与订单验证。",
           "5G 基站建设、千兆宽带渗透、云业务收入、光模块订单"),
        _p("空头/风控视角",
           "算力链高位拥挤，警惕 capex 指引下修、客户集中度与出口管制。",
           "云厂商 capex 环比、前五大客户占比、出口管制清单、板块换手率"),
    ],
    "jixie": [
        _p("机器人主题资金风格",
           "人形机器人是下一个大产业趋势，买核心零部件（丝杠/减速器/电机）卡位公司。",
           "特斯拉 Optimus 进展、国产机器人订单、核心零部件送样/定点"),
        _p("保守价值机构风格",
           "传统工程机械看更新周期 + 出海，买低估值高分红的周期龙头。",
           "挖机销量（国内/出口）、更新需求占比、PB 分位、股息率"),
        _p("券商机械首席框架",
           "拆「国内更新 + 出口 + 新兴（机器人/半导体设备）」三需求，开工数据定景气。",
           "小松开机小时数、挖机开工率、出口数据、制造业 PMI"),
        _p("空头/风控视角",
           "机器人主题估值远超当前收入体量，警惕订单证伪与零部件价格战。",
           "主题成交拥挤度、零部件降价、下游资本开支、实际出货量"),
    ],
    "guofang": [
        _p("军工主题资金风格",
           "订单驱动 + 资产证券化，买主机厂与核心配套，事件催化（阅兵/演习）定节奏。",
           "军工集团订单、合同负债、关联交易预计额、重大演习/阅兵"),
        _p("保守价值机构风格",
           "军工估值长期偏贵、现金流差，只在订单真空期后的底部区域布局。",
           "PE 分位、合同负债环比、经营性现金流、预收款"),
        _p("券商军工首席框架",
           "拆五年规划装备采购周期，分航空/航天/船舶/信息化赛道跟踪交付节奏。",
           "军费增速、装备采购计划、主机厂交付、元器件订单"),
        _p("空头/风控视角",
           "信息不透明 + 订单波动大，警惕业绩低于预期后的估值消化。",
           "年报/Q1 订单验证、装备降价压力、应收账款、板块 beta"),
    ],
    "youse": [
        _p("商品周期资金风格",
           "商品牛市里买资源量最大、弹性最高的标的（铜/金/锂），顺美元与利率周期。",
           "LME 铜价、金价、锂价、美元指数、全球交易所库存"),
        _p("保守价值机构风格",
           "买成本曲线左侧、储量寿命长的矿业龙头，按重置成本/储量估值。",
           "单位现金成本、储量/产量比、资本开支、分红率"),
        _p("券商有色首席框架",
           "拆「宏观（美元/利率/需求）+ 供需平衡表 + 库存」三层框架。",
           "TC/RC 加工费、交易所库存、中国进口量、下游开工率"),
        _p("空头/风控视角",
           "强周期顶部利润不可持续，警惕价格反转与资源国政策风险。",
           "价格历史分位、交易所累库、资源国税费政策、套保比例"),
    ],
    "meitan": [
        _p("高股息资金风格",
           "煤炭是现金牛 + 高分红，按类债配置，赚分红与估值修复的钱。",
           "股息率、长协价、秦皇岛动力煤价、分红率"),
        _p("保守价值机构风格",
           "买资源储量多、成本低、长协占比高的龙头，赚稳定自由现金流。",
           "吨煤成本、长协/市场煤比例、PB、自由现金流"),
        _p("券商煤炭首席框架",
           "拆「供给（产能/安监/进口）+ 需求（火电/钢铁/化工）」平衡表。",
           "港口库存、电厂日耗、进口煤量、焦钢企业开工率"),
        _p("空头/风控视角",
           "碳中和长逻辑压制估值，煤价下行周期盈利弹性向下同样大。",
           "新能源装机替代、煤价跌破长协、库存高企、保供政策"),
    ],
    "shiyou": [
        _p("能源高股息资金风格",
           "高股息 + 油价弹性，海上油气 > 陆上 > 炼化，赚产量增长与分红的钱。",
           "布伦特油价、OPEC 减产执行、股息率、产量指引"),
        _p("保守价值机构风格",
           "按储量与桶油成本估值，买桶油完全成本最低的。",
           "桶油完全成本、储量替代率、资本开支/折旧、自由现金流"),
        _p("券商石化首席框架",
           "拆「油价（供需/地缘）+ 炼化价差 + 化工品周期」三层。",
           "OPEC+ 产量、美国页岩油钻机数、炼油价差、PX/乙烯价差"),
        _p("空头/风控视角",
           "油价受地缘与 OPEC 政策操纵，警惕需求达峰与新能源替代。",
           "全球需求增速、电动车替代、OPEC 闲置产能、商业库存"),
    ],
    "gangtie": [
        _p("周期博弈资金风格",
           "供给侧改革/限产博弈 + 高股息，波段操作，吨钢毛利是核心。",
           "螺纹钢价、吨钢毛利、限产政策、股息率"),
        _p("保守价值机构风格",
           "买成本低、产品高端（特钢/硅钢）的龙头，破净时介入。",
           "PB、吨钢成本、高端品占比、现金分红"),
        _p("券商钢铁首席框架",
           "拆「地产/基建/制造业」三需求 + 「铁矿/焦炭」两成本。",
           "螺纹钢表观消费、铁矿价格、高炉开工率、社会库存"),
        _p("空头/风控视角",
           "地产下行吞噬需求，产能出清缓慢，警惕出口反倾销。",
           "地产新开工、钢厂盈利率、库存周期、出口反倾销调查"),
    ],
    "huagong": [
        _p("化工周期+成长资金风格",
           "买周期反转 + 成长新材料（电子化学品/新能源材料）双逻辑。",
           "化工品价格指数、MDI/钛白粉价差、出口、新材料放量"),
        _p("保守价值机构风格",
           "买一体化成本优势的周期成长龙头（万华模式），周期底部加仓。",
           "价差历史分位、资本开支、ROIC、PE/PB 分位"),
        _p("券商化工首席框架",
           "拆「原油成本端 + 产品价差 + 库存周期」，价差分位定买卖。",
           "布伦特油价、主要产品价差、行业开工率、库存"),
        _p("空头/风控视角",
           "产能扩张过快导致价差长期低迷，安全环保事故是黑天鹅。",
           "在建产能、价差跌破现金成本、安全环保督察、出口关税"),
    ],
    "jiadian": [
        _p("消费白马资金风格",
           "买全球化 + 高分红的白电龙头，赚稳定现金流与回购的钱。",
           "股息率、海外收入占比、回购金额、空调排产"),
        _p("保守价值机构风格",
           "家电是成熟现金牛行业，按 DCF + 股息定价，地产链错杀时买入。",
           "PE 分位、自由现金流、净现金、分红率"),
        _p("券商家电首席框架",
           "拆「内销（地产后周期+更新）+ 出口（海外补库+新兴市场）」。",
           "空调/冰箱/洗衣机出货、出口订单、铜钢原材料价、地产竣工"),
        _p("空头/风控视角",
           "内需饱和 + 地产拖累，警惕价格战与原材料上涨双重挤压。",
           "零售终端数据、渠道库存、原材料价格、汇率"),
    ],
    "gongyong": [
        _p("高股息+电价改革资金风格",
           "水电核电是类债资产，火电看煤价-电价剪刀差修复。",
           "股息率、利用小时、电价、来水、煤价"),
        _p("保守价值机构风格",
           "买有永久护城河（流域/牌照）的现金流资产，按 DCF 长持。",
           "自由现金流、分红承诺、装机结构、负债率"),
        _p("券商公用事业首席框架",
           "拆「量（利用小时）+ 价（电价）+ 本（煤价/折旧）」。",
           "全社会用电量、市场化交易电价、动力煤价、机组投产"),
        _p("空头/风控视角",
           "电价管制让利风险、来水波动、新能源消纳与辅助服务成本。",
           "电价政策、来水偏枯、弃风弃光率、辅助服务费用"),
    ],
    "jiaotong": [
        _p("交运周期资金风格",
           "航空买供需错配周期、航运买运价弹性、快递买格局改善。",
           "客座率/票价、BDI/SCFI 运价、快递单票价格、油价汇率"),
        _p("保守价值机构风格",
           "买有特许经营权的基础设施（机场/港口/铁路），按 DCF 定价。",
           "旅客吞吐量、股息率、资本开支周期、收费政策"),
        _p("券商交运首席框架",
           "分子行业拆「量 × 价 × 本（油/汇/人力）」。",
           "民航客运量、快递业务量、集运欧线/美线运价、布伦特油价"),
        _p("空头/风控视角",
           "高经营杠杆 + 高负债，需求波动放大亏损。",
           "需求增速、运力投放、汇率贬值、地缘扰动（红海/关税）"),
    ],
    "shangmao": [
        _p("渠道变革资金风格",
           "买渠道变革红利（折扣零售/跨境电商/即时零售）。",
           "社零增速、同店增长、GMV、开店数"),
        _p("保守价值机构风格",
           "零售净利率薄如纸，只买有物业价值 + 现金流的龙头。",
           "净利率、物业重估价值、PE 分位、现金流"),
        _p("券商商贸零售首席框架",
           "拆「客流 × 客单价 × 转化率」，跟踪社零与线上渗透。",
           "社零分品类、线上渗透率、CPI、消费者信心指数"),
        _p("空头/风控视角",
           "消费降级与渠道碎片化，传统零售持续失血。",
           "闭店率、坪效下滑、电商价格战、消费券政策退坡"),
    ],
    "shehui": [
        _p("出行链资金风格",
           "买出行链复苏 + 连锁化率提升（酒店/餐饮）。",
           "RevPAR、翻台率、节假日出行数据、开店数"),
        _p("保守价值机构风格",
           "买轻资产加盟模式、现金流好的龙头。",
           "加盟占比、同店销售、PE 分位、自由现金流"),
        _p("券商社服首席框架",
           "拆「门店数 × 单店模型（翻台/RevPAR × 成本）」。",
           "节假日旅游人次/收入、酒店 OCC/ADR、餐饮收入增速"),
        _p("空头/风控视角",
           "消费疲软下单店模型恶化，扩张越快亏得越多。",
           "同店下滑、闭店数、客单价下行、补贴退坡"),
    ],
    "fangzhi": [
        _p("国货品牌资金风格",
           "买国产品牌崛起 + 运动户外景气。",
           "流水增速、库存周转、折扣率、开店数"),
        _p("保守价值机构风格",
           "买高股息 + 品牌力的龙头，代工商看客户结构。",
           "股息率、PE 分位、渠道库存、Nike/Adidas 订单"),
        _p("券商纺服首席框架",
           "拆「品牌（流水/库存/折扣）+ 制造（订单/产能/汇率）」。",
           "社零服装类、出口订单、棉价、汇率"),
        _p("空头/风控视角",
           "库存周期与潮流更迭是杀手，警惕高库存减值。",
           "库存/收入、折扣加深、闭店数、暖冬天气"),
    ],
    "qinggong": [
        _p("轻工出海资金风格",
           "买家居出海 + 造纸周期反转。",
           "纸价/浆价、家具出口、地产竣工、集装箱运价"),
        _p("保守价值机构风格",
           "买有渠道壁垒的家居龙头与成本优势的造纸龙头。",
           "PB 分位、经销商数量、浆纸价差、现金流"),
        _p("券商轻工首席框架",
           "拆「地产后周期（家居）+ 纸价周期（造纸）+ 出口（关税）」。",
           "竣工面积、家具零售额、木浆价格、美国关税"),
        _p("空头/风控视角",
           "地产链需求长期承压，出口受关税与海运费扰动。",
           "地产销售、海运费、关税清单、渠道库存"),
    ],
    "jiancai": [
        _p("建材集中度资金风格",
           "买消费建材（防水/涂料）集中度提升 + 水泥错峰涨价。",
           "水泥价格、玻璃库存、沥青/乳液价、地产竣工"),
        _p("保守价值机构风格",
           "水泥是区域垄断生意，买成本低、区域格局好的龙头。",
           "吨毛利、PB、股息率、区域集中度"),
        _p("券商建材首席框架",
           "拆「地产 + 基建」需求与「错峰生产/碳减排」供给。",
           "水泥出货率、库容比、玻璃在产产能、专项债发行"),
        _p("空头/风控视角",
           "地产新开工长期下行，需求中枢不断下移，警惕应收账款暴雷。",
           "新开工面积、应收账款（地产商暴雷）、产能出清进度"),
    ],
    "jianzhu": [
        _p("中特估资金风格",
           "中特估 + 高股息央企重估，买低估值建筑央企。",
           "股息率、PB、新签订单、经营性现金流"),
        _p("保守价值机构风格",
           "买现金流改善、分红提升的央企，回避垫资模式。",
           "收现比、两金占比、分红率、PB 分位"),
        _p("券商建筑首席框架",
           "拆「基建投资 + 地产链 + 海外（一带一路）」三需求。",
           "基建投资增速、专项债、新签合同、PPP 项目"),
        _p("空头/风控视角",
           "地方债务约束基建空间，应收账款减值是常态。",
           "地方政府财力、应收账款账龄、减值计提、订单转化"),
    ],
    "nonglin": [
        _p("猪周期资金风格",
           "猪周期是最经典周期，买能繁母猪去化后的反转。",
           "能繁母猪存栏、猪价、仔猪价、出栏体重"),
        _p("保守价值机构风格",
           "买成本最低的养殖龙头（牧原模式），周期底部扩张。",
           "完全成本、头均盈利、资产负债率、PB 分位"),
        _p("券商农业首席框架",
           "拆「能繁 → 仔猪 → 育肥」10 个月传导链 + 疫病扰动。",
           "能繁环比、MSY、非洲猪瘟疫情、二次育肥"),
        _p("空头/风控视角",
           "产能去化不彻底则猪价反弹夭折，规模场占比提升拉长磨底时间。",
           "存栏结构、冻品库存、饲料销量、亏损持续时间"),
    ],
    "huanbao": [
        _p("公用事业化资金风格",
           "买有稳定运营现金流的垃圾焚烧/水务运营龙头。",
           "股息率、运营项目产能、处理费、现金流"),
        _p("保守价值机构风格",
           "运营类资产按 DCF，回避工程垫资类公司。",
           "自由现金流、分红率、应收账款、PB"),
        _p("券商环保首席框架",
           "拆「运营（量 × 价）+ 工程（订单）」，跟政策（化债/价格改革）。",
           "污水处理费、垃圾清运量、国补回款、地方化债进度"),
        _p("空头/风控视角",
           "政府付费依赖财政，应收账款拖欠是最大风险。",
           "应收账款/收入、账龄结构、国补退坡、地方财政状况"),
    ],
    "meirong": [
        _p("新消费资金风格",
           "买国货崛起 + 大单品策略的美妆龙头与医美上游。",
           "GMV 增速、大单品占比、复购率、新品周期"),
        _p("保守价值机构风格",
           "买品牌力已验证、估值回落的龙头，警惕流量依赖。",
           "PE 分位、净利率、营销费用率、现金流"),
        _p("券商美护首席框架",
           "拆「渠道（抖音/天猫）+ 品类（护肤/彩妆/医美）+ 人群」。",
           "社零化妆品类、抖音 GMV、医美机构客流、监管审批"),
        _p("空头/风控视角",
           "流量成本上升 + 消费降级，警惕高营销费用吞噬利润。",
           "ROI/费用率、客单价、医美广告监管、库存"),
    ],
    "chuanmei": [
        _p("AI 应用+内容资金风格",
           "买 AI 应用（游戏/短剧/AI 营销）降本增效 + 新品周期。",
           "游戏版号、流水、短剧投流、AI 应用落地"),
        _p("保守价值机构风格",
           "出版是高股息现金牛，买教材教辅龙头。",
           "股息率、PE 分位、现金流、教材需求稳定性"),
        _p("券商传媒首席框架",
           "拆「内容供给（版号/片单）+ 需求（时长/付费）+ 广告大盘」。",
           "版号发放、电影票房、广告刊例花费、用户时长"),
        _p("空头/风控视角",
           "内容行业爆款不可预测，政策监管是悬顶之剑。",
           "版号节奏、监管政策、流水衰减曲线、商誉"),
    ],
    "zonghe": [
        _p("题材博弈资金风格",
           "重组/资产注入题材小仓位参与，严格纪律。",
           "重组进展、资产注入公告"),
        _p("保守价值机构风格",
           "控股公司按 NAV 折价定价，回避业务庞杂看不懂的。",
           "NAV 折价、子公司质量、分红"),
        _p("券商综合行业框架",
           "分部估值（SOTP），逐块拆业务价值。",
           "分部收入利润、资产处置进展"),
        _p("空头/风控视角",
           "综合类公司治理差、透明度低，长期跑输指数。",
           "关联交易、审计意见、资金占用"),
    ],
}


# ==================== 存储 ====================

def _get_sector_path(sector_key: str) -> Path:
    return SECTOR_KNOWLEDGE_DIR / f"{sector_key}.json"


def _load_sector_knowledge(sector_key: str) -> dict:
    """Load cached sector knowledge; fall back to bootstrap defaults."""
    path = _get_sector_path(sector_key)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    meta = SECTORS.get(sector_key)
    default_perspectives = DEFAULT_SECTOR_KNOWLEDGE.get(sector_key)
    if meta and default_perspectives:
        return {
            "sector_key": sector_key,
            "sector_name": meta["name"],
            "sw_level1": meta["sw"],
            "perspectives": [
                {"stance": s, **p} for s, p in zip(PERSPECTIVE_STANCES, default_perspectives)
            ],
            "last_updated": "default_bootstrap",
        }
    return {}


def _save_sector_knowledge(sector_key: str, data: dict):
    SECTOR_KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    _get_sector_path(sector_key).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _ensure_all_default_sector_knowledge():
    """Ensure default knowledge files exist for all sectors on first run."""
    SECTOR_KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    for key in SECTORS:
        path = _get_sector_path(key)
        if not path.exists():
            data = _load_sector_knowledge(key)
            if data:
                data["_is_default"] = True
                _save_sector_knowledge(key, data)
                logger.info(f"Created default sector knowledge for: {key}")


# ==================== 查询 / 格式化 ====================

def list_sectors() -> list:
    """All registered sectors: key + display name + aliases."""
    return [{"sector_key": k, "sector_name": v["name"], "sw_level1": v["sw"],
             "aliases": v["aliases"]} for k, v in SECTORS.items()]


def get_sector_knowledge(sector_key: str) -> dict:
    data = _load_sector_knowledge(sector_key)
    if not data:
        return {"sector_key": sector_key, "status": "no_knowledge",
                "message": f"暂无板块 {sector_key} 的知识"}
    return data


def get_all_sector_knowledge_status() -> dict:
    status = {}
    for key, meta in SECTORS.items():
        data = _load_sector_knowledge(key)
        status[key] = {
            "sector_name": meta["name"],
            "initialized": bool(data),
            "perspective_count": len(data.get("perspectives", [])),
            "last_updated": data.get("last_updated", "never"),
            "is_default": data.get("_is_default", False),
        }
    return status


def get_sector_knowledge_for_prompt(sector_keys: list) -> str:
    """Format identified sectors' knowledge for prompt injection.

    每个板块输出 4 个视角（立场/方法论风格/观察方法/跟踪信号）。
    调用方（sector_router + context）保证 sector_keys 非空且有效。
    """
    blocks = []
    for key in dict.fromkeys(sector_keys or []):  # 去重保序
        data = _load_sector_knowledge(key)
        if not data:
            continue
        lines = [f"### 板块：{data.get('sector_name', key)}（申万一级：{data.get('sw_level1', '')}）"]
        for p in data.get("perspectives", []):
            lines.append(
                f"- **{p.get('stance', '')}**（{p.get('representative', '')}）\n"
                f"  方法：{p.get('method', '')}\n"
                f"  信号：{p.get('signals', '')}"
            )
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    return (
        "## 板块观察知识库（知名机构/人物方法论蒸馏）\n\n"
        "以下是针对本标的所属板块、从不同立场蒸馏的观察观点与研究方法。"
        "分析时请参考这些视角的框架与信号清单，交叉印证或挑战你的结论；"
        "注意：representative 为方法论风格标注，非该人物/机构的真实语录或当前持仓。\n\n"
        + "\n\n".join(blocks)
    )


# ==================== LLM 蒸馏更新 ====================

async def update_sector_knowledge(sector_key: str) -> dict:
    """Refresh one sector's knowledge via LLM (7-day cadence)."""
    meta = SECTORS.get(sector_key)
    if not meta:
        return {"error": f"Unknown sector: {sector_key}"}

    now = datetime.now()
    existing = _load_sector_knowledge(sector_key)
    if existing and existing.get("last_updated", "") != "default_bootstrap":
        try:
            last_dt = datetime.fromisoformat(existing.get("last_updated", ""))
            if (now - last_dt).total_seconds() < SECTOR_KB_REFRESH_INTERVAL:
                logger.info(f"Sector knowledge for {sector_key} is recent, skipping")
                return existing
        except Exception:
            pass

    sector_name = meta["name"]
    logger.info(f"Updating sector knowledge for: {sector_key} ({sector_name})...")

    system_prompt = f"""你是一位 A 股行业研究方法论专家，负责蒸馏板块观察知识。

你正在更新板块：「{sector_name}」（申万一级：{meta['sw']}）
当前时间：{now.strftime('%Y年%m月%d日')}

请蒸馏该板块 4 个不同立场的知名机构/人物风格对该板块的观察观点与研究方法：
1. 买方趋势派 —— 知名公募/私募基金经理的赛道投资方法
2. 深度价值派 —— 保守价值机构的安全边际定价方法
3. 卖方研究派 —— 券商首席分析师的产业跟踪框架
4. 风险逆向派 —— 空头/风控视角的证伪清单

输出格式为 JSON：
{{
    "sector_key": "{sector_key}",
    "sector_name": "{sector_name}",
    "sw_level1": "{meta['sw']}",
    "perspectives": [
        {{"stance": "买方趋势派", "representative": "方法论风格标注（30字内，不得虚构真实语录）",
          "method": "观察该板块的核心观点与研究方法（80-120字）",
          "signals": "关键跟踪信号（4-6个，顿号分隔）"}},
        {{"stance": "深度价值派", ...}},
        {{"stance": "卖方研究派", ...}},
        {{"stance": "风险逆向派", ...}}
    ],
    "last_updated": "{now.isoformat()}"
}}

要求：方法要具体到可执行的跟踪动作与指标，信号要可观测、可获取；
representative 只写公开已知的方法论风格（如「消费长持型私募风格」），
不得编造具体人物言论或持仓数据。"""

    try:
        response = await chat_complete(system_prompt, f"请更新「{sector_name}」板块的观察知识库。")
        try:
            json_start = response.find('{')
            json_end = response.rfind('}') + 1
            if json_start >= 0 and json_end > json_start:
                data = json.loads(response[json_start:json_end])
                data["last_updated"] = now.isoformat()
                # 结构校验：4 个视角齐全且字段非空，否则回退旧数据
                persp = data.get("perspectives")
                if not isinstance(persp, list) or len(persp) != 4 or not all(
                    isinstance(p, dict) and p.get("method") and p.get("signals") for p in persp
                ):
                    raise ValueError("perspectives 结构不完整")
                for i, stance in enumerate(PERSPECTIVE_STANCES):
                    persp[i]["stance"] = stance
                data["sector_key"] = sector_key
                data["sector_name"] = sector_name
                data["sw_level1"] = meta["sw"]
                _save_sector_knowledge(sector_key, data)
                logger.info(f"Sector knowledge updated for {sector_key}")
                return data
            raise json.JSONDecodeError("No JSON found", response, 0)
        except (json.JSONDecodeError, ValueError) as pe:
            data = dict(existing) if existing else {
                "sector_key": sector_key, "sector_name": sector_name,
                "sw_level1": meta["sw"], "perspectives": [],
            }
            data["raw_response"] = response[:2000]
            data["last_updated"] = now.isoformat()
            data["_update_failed"] = True
            _save_sector_knowledge(sector_key, data)
            logger.warning(f"Sector knowledge update for {sector_key} invalid ({pe}), preserved existing")
            return data
    except Exception as e:
        logger.error(f"Failed to update sector knowledge for {sector_key}: {e}")
        if existing:
            existing["error"] = str(e)
            return existing
        return {"error": str(e)}


async def update_all_sector_knowledge() -> dict:
    """Refresh all sectors' knowledge (called by the 7-day scheduler)."""
    tasks = [update_sector_knowledge(k) for k in SECTORS]
    completed = await asyncio.gather(*tasks, return_exceptions=True)
    results = {}
    for key, result in zip(SECTORS, completed):
        results[key] = {"error": str(result)} if isinstance(result, Exception) else result
    logger.info(f"Sector knowledge update complete for {len(results)} sectors")
    return results

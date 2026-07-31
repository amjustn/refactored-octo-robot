# 财务数据获取与交叉验证规范

对 $ARGUMENTS 执行财务数据获取和交叉验证。

## 数据源优先级

### 美股
| 优先级 | 来源 | URL |
|--------|------|-----|
| 1（主） | macrotrends | macrotrends.net |
| 2（副） | stockanalysis | stockanalysis.com |
| 一手 | SEC EDGAR | sec.gov |

### 港股
| 优先级 | 来源 | URL |
|--------|------|-----|
| 1（主） | aastocks | aastocks.com |
| 2（副） | macrotrends (ADR) | macrotrends.net |
| 一手 | HKEX披露易 | hkexnews.hk |

### A股
| 优先级 | 来源 | URL |
|--------|------|-----|
| 1（主） | 东方财富 | eastmoney.com |
| 2（副） | 巨潮资讯 | cninfo.com.cn |

## 交叉验证规则
- 误差≤1% → 一致，取主来源值
- 误差1-5% → 标记差异，注明可能原因
- 误差>5% → 必须查原始财报核实

## 常见差异原因
GAAP vs Non-GAAP、汇率换算、财年定义、合并口径、数据更新滞后

## 特别规则
- 未上市公司：数据前标记[估计]，不执行交叉验证
- 原始财报优先

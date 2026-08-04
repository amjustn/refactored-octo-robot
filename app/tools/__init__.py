from .financial_rigor import (
    benford,
    calc,
    cross_validate,
    three_scenario,
    verify_market_cap,
    verify_valuation,
)

# Market data tools — lazy import to avoid hard dependency on akshare/yfinance
# These will be available even if the libraries are not installed,
# they just won't be able to fetch live data.
try:
    from .market_data import (
        fetch_company_news,
        fetch_market_indices,
        get_current_date_context,
        get_financial_data,
        get_stock_price,
        search_company,
    )
except ImportError:
    get_stock_price = None
    get_financial_data = None
    fetch_company_news = None
    fetch_market_indices = None
    search_company = None
    get_current_date_context = None

try:
    from .knowledge_updater import (
        get_all_knowledge,
        get_knowledge_status,
        get_knowledge_summary_for_prompt,
        get_master_knowledge,
        update_all_masters,
        update_master_knowledge,
    )
except ImportError:
    update_master_knowledge = None
    update_all_masters = None
    get_master_knowledge = None
    get_all_knowledge = None
    get_knowledge_summary_for_prompt = None
    get_knowledge_status = None

__all__ = [
    "verify_market_cap",
    "verify_valuation",
    "cross_validate",
    "three_scenario",
    "calc",
    "benford",
    "get_stock_price",
    "get_financial_data",
    "fetch_company_news",
    "fetch_market_indices",
    "search_company",
    "get_current_date_context",
    "update_master_knowledge",
    "update_all_masters",
    "get_master_knowledge",
    "get_all_knowledge",
    "get_knowledge_summary_for_prompt",
    "get_knowledge_status",
]

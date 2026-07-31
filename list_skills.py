#!/usr/bin/env python3
"""List all skills."""
import sys
sys.path.insert(0, "/home/q/ai_berkshire_web")

from app.skills import list_skills

for s in list_skills():
    name = s["name"]
    display = s["display_name"]
    multi = s.get("is_multi_agent", False)
    series = s.get("series_mode", False)
    agents = s.get("agents", [])
    print(f"{name:30s} | {display:20s} | multi={multi} series={series} agents={len(agents)}")

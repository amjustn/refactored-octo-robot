#!/usr/bin/env python3
"""Add daily-briefing skill to registry."""
from pathlib import Path

REGISTRY = Path("/home/q/ai_berkshire_web/app/skills/registry.py")
code = REGISTRY.read_text(encoding="utf-8")

# Add daily-briefing skill at the end of SKILLS list (before the closing ])
NEW_SKILL = '''    {
        "name": "daily-briefing",
        "display_name": "金融日报",
        "description": "每日金融日报：自动上网搜索最新行情+新闻+行业动态，生成结构化日报",
        "category": "日报",
        "is_multi_agent": False,
        "agent_count": 1,
        "input_hint": "留空生成今日日报，或输入关注行业/公司，如：新能源 / 腾讯",
    },
'''

# Find the last skill entry and add after it
MARKER = '    {\n        "name": "wechat-article",'

if "daily-briefing" not in code:
    # Find the wechat-article skill block and add daily-briefing after it
    # We need to find the closing ] of the SKILLS list
    idx = code.rfind("]")
    if idx >= 0:
        # Insert before the closing ]
        # Find the line before ]
        insert_pos = code.rfind(",", 0, idx)
        if insert_pos >= 0:
            code = code[:insert_pos+1] + "\n" + NEW_SKILL + code[insert_pos+1:]
            REGISTRY.write_text(code, encoding="utf-8")
            print(f"✅ Added 'daily-briefing' skill to registry")
            print(f"   Saved ({len(code)} bytes)")
        else:
            print("ERROR: Could not find insertion point")
    else:
        print("ERROR: Could not find closing ]")
else:
    print("daily-briefing already in registry")

# Eval cassettes

录制的工具响应，key 为 `{tool}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}`，
与 `app.harness.tools.ToolGateway._cache_key` 完全一致。

- 回放时 `ToolGateway.playback = <cassette dict>`，命中的 key 直接返回录制内容，
  `source="cassette"`、`cache_hit=true`
- 未命中的 key 返回 `no cassette` 错误（离线模式不触网）
- 每季度重录一次；重录后跑 `python eval/report.py` 对比新旧数据漂移

文件格式：单个 JSON 对象，key → 工具响应字符串（通常是 JSON 文本）。

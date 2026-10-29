
## Git 版本控制纪律 (2026-10-09, 用户明确指示)
- 本项目已 git init（main 分支，首次提交 7799236，170 文件）。每次实质性代码变更后单独提交，message 含改动+测试结果。
- .gitignore 已固化排除：.env / data/session.json / data/sessions/ / docs/probe/（含真实 msToken 等会话材料，仅本地）/ .venv / logs / exports。
- 提交前扫描敏感值（sessionid/ttwid/msToken 长值），命中即加排除，不改证据文件本体。

## 待办-类目通道修复（2026-10-09 记录，待合适时机解决）
- scrape_category 类目列表页解析不出商品（无 WAF 拦截时也没有商品），该通道从未被 e2e 验证过；单品 PDP 通道是已验证主力（Phase 5a 闭环）。
- 影响：类目通道是全系统唯一自动发现新商品的入口（video/creator/itemlist 都以 product_id/creator 为输入，不能从零发现）；坏了只影响选品发现，不影响已知商品追踪（PDP 快照 diff 链路完整）。
- 修复路径三选一：① 解析页面内嵌 JSON（__UNIVERSAL_DATA__ / SIGI_STATE，最可能）；② 改走类目 API 端点（同 PDP 模式）；③ headless 渲染（最后手段）。
- 时机：建议等 IPRoyal 固定 IP 到位后再探测，避免调试期暴露账号身份；先保存类目页 HTML 看商品数据是否在内嵌 JSON 里即可定位。

# 第二步：关键词搜索与相关性筛选

目标：保留人穿的定制宠物肖像服装，排除明显的宠物衣服与非服装商品；证据不足进入待确认。

## 处理流程

关键词搜索 → 保存全部返回结果 → 标题初筛 → 本轮 ASIN 去重 → 预算内采集相关/待确认商品 → 标题与主商品描述复核 → 保存统一字段及判断证据。

- 使用既有 AmzPy 搜索；依旧一页搜索、至多三个详情、四次请求预算。没有付费服务或模型调用。
- 规则在 `collector/relevance_rules.json`；判断在 `collector/relevance.py`。
- `related`：服装、定制、宠物肖像三类文本证据齐全。是规则判定，不代表已验证图片或实际定制下单。
- `unrelated`：标题明确是宠物穿的衣服或非服装商品，或主商品明确不支持定制。
- `needs_review`：证据不足，不能以缺少数据断言不相关；保留并允许预算内详情复核。
- 关键词不作为商品证据。描述只取既有归一化逻辑的产品要点/主商品描述，不使用推荐区。
- 商品 `product_scope` 区分宠物肖像专门商品与详情明确支持宠物肖像的通用照片定制服装。
- `discoveries.json` 保留全部搜索结果的判断、原文命中、规则版本及计划动作；不相关只跳过详情，不删除发现记录。
- 排序保持上游返回顺序；`selected_result_position` 不因跳过商品重新编号。它仍不是经过核验的页面排名，广告属性保持未知。
- 预算外候选保存为 `defer_budget`，本阶段尚未形成持久任务队列。去重只在本轮有效，跨次状态属于第三步。
- 详情复核为不相关或待确认的商品仍保存完整字段与原因，便于复查，不混作相关结果。

## 离线核对

从 `F:\shop\crawler-lab` 执行：

```powershell
.\amazon-lab\.venv\Scripts\python.exe -I .\amazon-lab\test_relevance.py
.\amazon-lab\.venv\Scripts\python.exe -I .\amazon-lab\test_probe_conditions.py
.\amazon-lab\.venv\Scripts\python.exe -I .\amazon-lab\audit_relevance.py
```

最后一个命令不联网，读取原先保存的 48 条搜索记录及三件详情，写入 `test/amazon/amzpy/step2-relevance/`：

- `RESULT.md`：全量判断表及三件详情复核。
- `discoveries.json`：搜索证据与计划动作，未实际执行新请求。
- `posts.json`：保留历史采集时间、价格等字段，新增相关性结果。
- `report.json`：计数、规则版本、数据来源与离线分析时间。

原历史搜索中有 JPY 报价，不作为最新美国报价；本轮不重新搜索、不更新价格。规则目前是可审查的初版，尚无人工标注大样本评估，可能误判，不能保证召回率或准确率。后续数据库、持久限流和前台定时调度已接入，参见 [README_COLLECTOR.md](README_COLLECTOR.md)。

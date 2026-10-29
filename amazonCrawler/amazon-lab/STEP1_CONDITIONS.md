# 第一步：固定并核对采集条件

配置：`collection_conditions.json`。当前限定 Amazon.com、美国邮编 10001、USD、英文；价格仅指主商品页面显示价，不是最终定制订单金额。

## 已接入的行为

- `probe.py` 在初始化采集器前检查已保存会话是否存在；不会打印 Cookie。
- 每次搜索/详情响应核对域名、邮编、币种偏好和页面语言。缺少核对证据或条件不符时立即停止该轮，记录原因，退出码为 1。
- 禁止自动跟随跳转，3xx 即停止，避免跳转产生未计入的请求。
- 详情结果增加 `collection_conditions`：预期条件、实际观察、所请求 ASIN、变体状态、比较条件与原因。
- ASIN 来自购买区隐藏字段；尺码、颜色、版型同时读取选中控件和脚本中 `sortedDimValuesForAllDims` 的 `SELECTED` 项。使用 JSON 解码，不执行脚本；可选值列表和标题不作为已选变体证据。
- `observed.variant_evidence` 保存字段来源、关联标识及绑定状态；缺失留空并记录原因。控件与内嵌选中值不同，标记 `variant_value_conflict`。
- 普通变体标识与购买区 ASIN 不同，标记 `variant_evidence_conflict`。`MC_Custom_…` 定制复合标识则标记 `variant_binding_unverified`：保留选中值，但不直接断言是错误变体或已匹配报价。不自动切换商品或替用户选择变体。
- 新商品默认 `variant_status=not_fixed`、`comparison_ready=false`。要固定商品，在 `tracked_variants` 中以 ASIN 为键填入页面已核对的 `asin`、`size`、`color`；只有身份和全部条件匹配才允许比较。不能确认的商品继续保留数据。

`region_verified` 只表示域名、配送邮编、币种证据匹配；完整结果仍需检查语言、商品身份及变体。`comparison_ready` 只表示地区和变体比较前提满足，后续价格分析还要判断卖家是否改变、价格是否缺失。

## 查看离线结果

从 `F:\shop\crawler-lab` 运行：

```powershell
.\amazon-lab\.venv\Scripts\python.exe -I .\amazon-lab\audit_conditions.py
```

只读取 10 月 8 日保存的 HTML 和请求元数据，不联网。独立输出在 `test/amazon/amzpy/step1-conditions/`：

- `conditions-report.json`：三件商品的核对结果与证据路径。
- `RESULT.md`：便于直接阅读的核对表。
- `posts.json`：附带采集条件的统一商品字段。

原始页面与旧结果不会被此命令覆盖。商品 `fetched_at` 保留原采集时间；`audited_at` 是离线核对时间。

## 验证命令（不联网）

```powershell
.\amazon-lab\.venv\Scripts\python.exe -I .\amazon-lab\test_conditions.py
.\amazon-lab\.venv\Scripts\python.exe -I .\amazon-lab\test_preferences.py
.\amazon-lab\.venv\Scripts\python.exe -I .\amazon-lab\test_probe_conditions.py
```

以上是第一步当时的交付范围。后续六步已接入正式模块，参见 [README_COLLECTOR.md](README_COLLECTOR.md)。本轮仍未新增 Amazon 请求，长期运行稳定性未验证。

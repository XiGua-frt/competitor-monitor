# Amazon 美国站商品采集模块交付包

六步代码、配置、81 项离线测试、三件公开商品字段测试样本、历史示例报告和本地 AmzPy 源码均已包含。

## 在新电脑使用

1. 解压整个目录，保持 amazon-lab 和 test 的相对位置。准备 Windows、Python 3.11 和 Chrome（Chrome 仅真实会话准备需要）。
2. 在 PowerShell 进入解压目录下的 amazon-lab。若执行策略阻止脚本，可只为当前终端设置 `Set-ExecutionPolicy -Scope Process Bypass`。
3. 执行 `.\setup.ps1`。此步骤联网下载免费 Python 依赖，不会采集 Amazon；虚拟环境按当前解压路径重新创建。
4. 执行 `.\run-tests.ps1`，或 `.\start-collector.ps1 demo` 验证离线链路。
5. 执行 `.\start-collector.ps1 import-saved`、`.\start-collector.ps1 report` 查看历史数据；这些命令不会联网。结果写入 amazon-lab/data/reports。

## 真实运行

交付包不含 cookie、登录会话、浏览器资料、原完整响应页面、生产数据库和虚拟环境。真实采集前，在专用浏览器窗口设置美国测试邮编 10001、USD 和英语：

```powershell
.\.venv\Scripts\python.exe -I prepare_preferences.py --manual
.\start-collector.ps1 once
# 确认一轮正常后，再启动前台定时运行：
.\start-collector.ps1 schedule
```

prepare_preferences.py 是人工准备会话的工具，会访问 Amazon；不属于采集任务请求账本。程序遇到验证码不自动绕过，按网站正常流程人工处理。会话仅保存在解压目录/private，请勿分享该目录。Ctrl+C 停止前台调度；pause/resume 控制保存在状态库中，恢复不清除配额或冷却。

详细配置、六步行为和数据含义见 amazon-lab/README_COLLECTOR.md。

## 验证边界

离线测试和保存页面回放已通过；当前交付仍是单机版本。历史价格保持 2026-10-08 原时间，不是今天报价。三件商品变体尚未全部确认；真实长期稳定性未验证。

test 中 HTML 是裁剪后的公开字段测试样本，保留本模块解析所需字段和变体证据，不是原始完整响应。包内附清单和 SHA-256 校验文件。本地 AmzPy 来源、提交、修改及 MIT 许可证见 amazon-lab/amzpy。

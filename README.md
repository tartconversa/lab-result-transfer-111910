# 实验室及项目档案台

这是本地生成的外部来源与独立接收端，仅用于提供数据、页面、协议与回执。仓库提供合成来源服务，采集/RPA客户端待开发。所有单位、厂商、研究记录和门户内容均为虚构数据，不连接真实业务系统。

## 启动

安装Docker后，在项目根目录运行 docker compose up --build，服务默认监听 http://127.0.0.1:8765。若直接在主机运行，Python 3.11+执行 pip install -r requirements.txt，再运行 python -m source.app --port 8765。浏览器任务通过Playwright 1.56.0提供的Chromium测试来源页面。

输入示例位于 inputs/request.json；业务字段、页面与协议边界见 docs/source-contract.md。示例是项目可读输入格式，原件与来源修订均由演练服务产生。

## 采集客户端（collector）

仓库包含企业侧 Python 浏览器自动化客户端，全部通过 Playwright Chromium 与门户页面交互，不使用演练控制接口替实验室签发结论。架构与规则映射见 docs/collector-design.md。常用命令（默认台账与下载目录在 var/，已 gitignore）：

```bash
python3 -I run_collector.py claim --request inputs/request.json   # 授权领取正式报告与仪器原件
python3 -I run_collector.py todo                                   # 待办核对：依据/缺件/预览/撤签/替代
python3 -I run_collector.py transfer                               # 档案台页面移交，幂等且自动补齐缺项
python3 -I run_collector.py reconcile                              # 断线后只查归档历史，不重复提交
python3 -I run_collector.py handoffs                               # 移交查询：逐项归档号、pending、替代与暂停
python3 -I run_collector.py claims                                 # 领取查询：样品/配置/原件摘要
```

## 来源说明

服务页面 / 简述当前来源。JSON与XML接口描述输入契约，页面展示来源交互；POST /__control 只用于本地演练人员切换来源修订、可用性和延迟场景。状态可用 --state /path/outside/source-state.json 选择性落盘，默认每次启动恢复初始样本。端点行为和合成记录见 source/app.py。

## 检查

运行 python -m unittest discover -s tests -v 检查来源端、原件、门户状态和合成接收端本身。涉及页面的数据源测试会启动真实Chromium。tests/test_collector.py 以同样方式驱动真实 Chromium 验收采集客户端：授权领取、预览拦截、联合成套、分批缺件补齐、写入后503的历史核实、复测替代、撤签暂停与临时失败重试。它不验证任何尚未编写的采集程序，也不代表采集业务验收已经通过。

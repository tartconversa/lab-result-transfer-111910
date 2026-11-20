# 采集客户端设计（collector 包）

本文件说明企业侧 RPA 客户端如何在只使用合成记录的前提下，通过 Chromium
领取联合实验室门户的正式报告与仪器原件，核对样品/配置归属，再移交独立档案台。
客户端不连接任何真实业务系统，不调用来源演练控制接口 `POST /__control`
（该接口仅测试侧 `tests/support.py` 使用，用于切换来源修订）。

## 模块划分

| 文件 | 职责 |
| --- | --- |
| `collector/browser.py` | Chromium 页面交互层。只汇报“页面事实”：列表状态、报告元数据、公示 SHA256、浏览器下载、档案台文件控件上传、表单提交、历史回执读取。不含业务裁决。 |
| `collector/ledger.py` | 本地项目档案台账。领取记录、原件清单与摘要、移交回执、替代/撤签状态；临时文件原子写入。 |
| `collector/engine.py` | 业务引擎。授权范围、配置归属、联合成套、分批缺件、复测替代、撤签暂停、断线幂等、待办与查询视图。 |
| `collector/cli.py` / `run_collector.py` | 命令行入口；后者支持 `python3 -I` 隔离模式（显式把仓库根加入 `sys.path`）。 |
| `tests/test_collector.py` | 真实 Chromium 集成测试，覆盖全部生命周期场景。 |

## 命令

```bash
python3 -I run_collector.py --base-url http://127.0.0.1:8765 \
    --ledger var/ledger.json --downloads var/downloads claim --request inputs/request.json
python3 -I run_collector.py ... todo        # 待办核对
python3 -I run_collector.py ... transfer    # 移交（幂等，含缺项补齐）
python3 -I run_collector.py ... reconcile   # 断线后仅查历史核实，不重新提交
python3 -I run_collector.py ... handoffs    # 移交查询：归档号/pending/替代/暂停
python3 -I run_collector.py ... claims      # 领取查询：样品、配置、原件摘要
```

下载原件按“每份报告一个独立空目录”存放（`var/downloads/<报告>-rev<修订>/`），
不同报告的同名文件互不覆盖；台账记录的是报告清单里的原始 `name`，
不会拿最新同名文件替补。

## 业务规则到实现的映射

1. **任务结束 ≠ 正式签发。** 引擎只接受报告页元数据 `status == "signed"`；
   `preview` 登记为不可引用且不下载原件（`engine.claim`、`todo_view`），
   列表页与详情页状态不一致时以详情页为准并记录事件。
2. **授权领取。** 仅处理 `request.authorized_samples` 内样品的报告，
   其余进入 `outside_authorization` 并写事件，不打开下载。
3. **配置归属与原件清单。** 台账保存 `sample` 与
   `card/driver/fw` 组合；同一样品不同配置（如 R-A 的 fw-f2 与 R-B 的 fw-f3）
   各自独立。下载严格遍历报告 `originals` 清单，本地 SHA256 必须等于报告页
   公示值，否则按临时失败处理。
4. **联合结论不可拆。** 计划阶段对 `joint` 报告强制使用报告全量项目
   （`plan_transfers`）；档案台对拆单返回 `joint_scope` 时也会被拒并记录。
   联合报告只覆盖请求中的部分项目时，仍按成套处理，原件不齐则整体挂待办。
5. **分批签发不互相阻塞。** 独立项目各自形成移交单元；档案台 `partial` 时
   首批只接收首项，其余进入回执 `pending`。缺项不影响已独立签发项目的归档号。
6. **缺项补齐只补缺。** `plan_supplements` 针对 pending 生成独立补齐 key
   （`<引用>:<报告>:sup:<项目>`），不重交已收件项目；联合缺件仍成套补齐。
7. **复测替代而非作废旧档案。** `supersedes`（如 `R-A:latency`）把对应旧移交
   标记为 `superseded`/`superseded-partial`，原归档号保留在备注中备查；
   未被提及的项目（如 R-J 的 power/thermal）状态不变，绝不因新报告未提及而作废。
8. **撤签与临时失败分离。** `revoked` 仅把该报告支撑的移交置为 `suspended`，
   若项目另有独立有效证明（如 R-B），待办视图仍列其为可引用；下载异常抛
   `TransientBrowserError`，保留 `signed` 状态、写入 `failures`，可重试恢复。
9. **断线幂等。** 每次提交前先读 `/history`，同 key 已有收件记录则直接对账；
   收到写入后 503（`gateway_after_write`）不重试提交，转查历史确认。
   历史仍无记录时保持待办，绝不重复上传制造第二份正式记录。
10. **查询视图。** `claims` 给出项目当前依据与原件摘要；`todo` 给出每项的
    可引用依据、缺件、预览/撤签/替代原因；`handoffs` 给出逐项归档号、
    pending、暂停与被替代的原归档号。

## 状态机（移交单元）

```
open ──回执无 pending──────────────► closed
open ──回执含 pending──────────────► open（后续 :sup: 补齐单元关闭缺项）
any  ──报告撤签────────────────────► suspended（独立证明存在时项目仍可引用）
any  ──复测 supersedes 命中────────► superseded / superseded-partial（归档号保留）
```

## 安全与隔离

- 浏览器由 Playwright 1.56.0 自带 Chromium 启动（headless、`--no-sandbox`），
  下载走 `expect_download` 链路，不直接发 HTTP 抓取业务数据。
- 下载内容视为不可信数据，只做 SHA256 比对与原样上传，不执行、不导入。
- 推荐以 `python3 -I` 运行启动器，避免下载目录中的同名模块（如 `json.py`）
  被解释器加载。
- 台账与下载目录默认在 `var/`（已 gitignore），可经参数指向仓库外路径。

# Clinical Trial Matching with WHO MCP

面向多癌种、跨国家/地区患者的临床试验匹配项目。项目通过 WHO ICTRP MCP
完成数据库检索，并结合 WHO 门户增量、直接注册库核验、资格判断、风险与疗效分析、
论文证据和决策汇总生成患者报告。

本项目仅用于临床试验信息匹配和预筛，不构成医学建议、治疗推荐或入组结论。
试验状态、具体队列、中心、名额和完整入排标准必须由医生及研究中心确认。

## 项目结构

仓库包含五个可以独立发现的 sibling Skills：

- `clinical-trial-matching-who-mcp`：患者输入、检索、核验、编排和报告；
- `trial-gater`：逐试验资格判断；
- `trial-risk-annotator`：患者及癌种特异风险；
- `trial-efficacy-contextualizer`：疗效证据和标准治疗背景；
- `decision-synthesizer`：非排除候选的核实路径汇总。

安装全部 Skills：

```bash
npx skills add . --list
npx skills add . --skill '*'
```

当前流程架构：

```text
患者结构化 / Cancer Buddy 病历包
→ 八维搜索计划
→ WHO MCP 数据库检索 + WHO 门户增量
→ 统一去重、直接注册库核验、通用硬规则
→ trial-gater
→ 非排除试验的 risk + efficacy + 论文证据
→ decision-synthesizer
→ 国内/本国可及、国家记录待核实、境外报告
```

详细模块边界见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 运行要求

- Python 3.10 或更高版本；
- 支持 `database_metadata`、`execute_search_plan`、`get_trial` 的 WHO MCP；
- 一个模型执行后端：模型 API、Claude Code/Codex 类 CLI 或自定义 runner。

项目运行代码和测试仅使用 Python 标准库。WHO MCP 服务管理自己的数据库和依赖。

## 患者输入

`--patient` 支持两种输入：

1. `clinical-trial-matching-patient-v1` 扁平 JSON；
2. Cancer Buddy 多文件病历目录。

Cancer Buddy 目录可以包含 `profile.json`、`patient_summary.json`、`molecular.json`、
`treatment_lines.json`、`labs.json`、`comorbidities.json` 和 `readiness.json`。
患者国家/地区不得从语言或医院名称推断，应在病历数据或 `matching_context.json` 中明确提供。
示例见
[matching_context.example.json](skills/clinical-trial-matching-who-mcp/examples/matching_context.example.json)。

未提供 `--plan` 时，程序会从规范化患者数据生成可审计的八维基础搜索计划。

## 配置 WHO MCP

凭证只能通过环境变量或 CI Secrets 注入，不得写入仓库。

### Streamable HTTP

```bash
export WHO_MCP_TRANSPORT=streamable-http
export WHO_MCP_URL=https://mcp.example.org/mcp
export WHO_MCP_API_KEY=replace-locally
export MCP_REQUEST_TIMEOUT_SECONDS=60
export MCP_DETAIL_CONCURRENCY=4
```

公网 MCP 应使用 HTTPS。只有本机或受信任私网调试可以显式设置：

```bash
export WHO_MCP_ALLOW_INSECURE_HTTP=1
```

### 本地 stdio

```bash
export WHO_MCP_TRANSPORT=stdio
export WHO_MCP_PYTHON=/absolute/path/to/python
export WHO_MCP_SERVER=/absolute/path/to/server.py
export WHO_MCP_DB=/absolute/path/to/trials.db
```

## 正式流程

正式执行只有两个主要入口，不需要模型自行决定是否继续下一批。

### 1. Prepare

`prepare` 完成患者适配、搜索计划、MCP 检索、WHO 增量、去重、直接注册库核验、
通用硬规则和 gater 任务生成。

```bash
python skills/clinical-trial-matching-who-mcp/scripts/pipeline/run_formal_pipeline.py \
  prepare \
  --patient /path/to/patient-or-cancer-buddy-directory \
  --run-dir /path/outside-repository/run \
  --mcp-transport streamable-http
```

WHO 门户检索和直接注册库访问会发送患者衍生查询或试验 ID。获得操作授权后启用：

```bash
export EXTERNAL_REGISTRY_ACCESS_AUTHORIZED=1
```

并在 `prepare` 中使用：

```bash
--portal-delta-mode auto --authorize-external-registry-access
```

复用已经获取且仍有效的增量文件：

```bash
--portal-delta-mode file --portal-delta /path/to/portal_delta.json
```

`--portal-delta-mode off` 不访问 WHO 门户。Portal Delta 只补充数据库水位线之后的
新登记，不能代替旧试验的实时招募状态核验。

### 2. Execute

`execute` 确定性地连续完成全部 gater、论文预取、deep、decision、merge 和 finalize，
支持并发、重试、无效响应隔离、单试验恢复和断点续跑。

通用 OpenAI-compatible API 示例：

```bash
export MODEL_EXECUTION_BACKEND=api
export MODEL_PROVIDER=openai-compatible
export MODEL_BASE_URL=https://provider.example/v1
export MODEL_NAME=provider-model-id
export MODEL_API_KEY=replace-locally

python skills/clinical-trial-matching-who-mcp/scripts/pipeline/run_formal_pipeline.py \
  execute --run-dir /path/outside-repository/run
```

还支持 `openai`、`anthropic`、`glm` 和 `openai-compatible`。不同阶段可以分别设置
`GATER_MODEL_*`、`DEEP_MODEL_*` 和 `DECISION_MODEL_*`。完整配置见
[MODEL_API_EXECUTION.md](MODEL_API_EXECUTION.md)。

查看断点状态：

```bash
python skills/clinical-trial-matching-who-mcp/scripts/pipeline/run_formal_pipeline.py \
  status --run-dir /path/outside-repository/run
```

正式报告只由 `finalize` 路径生成。运行清单会记录召回、硬排除、gater、deep、risk、
efficacy、evidence 和遗漏数量；存在集合遗漏时不会生成正式模板。

## 中文报告

报告结构、筛选分栏和确定性标签会按患者语言渲染。若模型仍输出英文临床叙事，可以
正式 `finalize` 根据患者明确的 `country` 自动选择报告语言：中国患者进入中文
后翻译，其他国家患者直接生成英文报告。翻译只处理患者可见叙事，不重新进行检索、
入排判断或临床决策。历史 `pipeline.json` 仍可手工执行后翻译：

```bash
python skills/clinical-trial-matching-who-mcp/scripts/render/report_translation.py \
  --input /path/to/final/pipeline.json \
  --output /path/to/translated-pipeline.json

python skills/clinical-trial-matching-who-mcp/scripts/render/rerender_pipeline.py \
  --pipeline /path/to/translated-pipeline.json \
  --out /path/to/translated-report \
  --report-language zh-CN
```

翻译模块保留试验 ID、药物名、生物标志物、数值、引用和 URL。它不绑定任何厂商
或模型：`TRANSLATION_MODEL_*` 可配置 OpenAI、Anthropic、GLM、MiniMax 或任意
OpenAI-compatible API；未配置时继承通用 `MODEL_*`。重新渲染属于展示验证产物，会输出
`validation-report.html`，不会伪装成重新完成时效核验的正式报告。

大批量中文报告可按供应商能力将临床分析模型与翻译模型分开配置。翻译器会复用
完全相同的叙事、跳过纯临床 token，并使用占位符保护临床事实；每批结果写入
`report-translations.json` 检查点，中断后只续跑尚未完成的唯一文本。批次与并发可用
`TRANSLATION_BATCH_MAX_UNITS`、`TRANSLATION_BATCH_MAX_CHARACTERS` 和
`TRANSLATION_MODEL_CONCURRENCY` 调整。`TRANSLATION_MODE=required` 可要求中国患者
在缺少翻译 API 时停止；`auto` 会记录 `skipped_no_model_api`，不会静默声称已翻译。

## 报告审查

自动清单证明流程覆盖，不证明每项临床判断正确。正式交付前应使用
[FINAL_REPORT_REVIEW_CHECKLIST_CONCISE.md](FINAL_REPORT_REVIEW_CHECKLIST_CONCISE.md)
人工核对试验描述、病种/队列、分子条件、机制分类、地点分类、招募状态、入排判断、
论文和链接。

## 测试与仓库校验

```bash
python scripts/validate_repository.py
python -m unittest discover \
  -s skills/clinical-trial-matching-who-mcp/tests \
  -p "test_*.py" -v
```

远程 MCP 集成测试只在 CI Secrets 中配置 `WHO_MCP_URL` 和 `WHO_MCP_API_KEY` 时运行；
其余测试不需要真实凭证。

## 安全与数据处理

- 不提交 API Key、SSH 密码、`.env`、数据库、患者输入、运行日志或患者报告；
- 运行目录应放在仓库外，`production-runs/`、`run/` 和报告文件已被忽略；
- 直接注册库核验只允许访问配置的 HTTP(S) 注册库域名，并拒绝本地和私网目标；
- 不在公开 issue、CI 日志或 PR 中包含患者信息和凭证。

安全政策见 [SECURITY.md](SECURITY.md)，第三方数据许可与引用见 [NOTICE.md](NOTICE.md)。

## 文档

- [ARCHITECTURE.md](ARCHITECTURE.md)：当前模块和数据流；
- [MODEL_API_EXECUTION.md](MODEL_API_EXECUTION.md)：模型 API、并发与恢复配置；
- [DEVELOPMENT_TESTING.md](DEVELOPMENT_TESTING.md)：开发与测试约定；
- [FINAL_REPORT_REVIEW_CHECKLIST_CONCISE.md](FINAL_REPORT_REVIEW_CHECKLIST_CONCISE.md)：正式报告人工审查。

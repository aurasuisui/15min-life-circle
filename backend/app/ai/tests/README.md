# app/ai/tests —— S4（AI 层）测试与自测资产

> 本目录**全部零网络**（除 `smoke_llm.py`，它是**故意**要真实调用 DeepSeek 的冒烟脚本）。
> 运行方式见根目录 `README` 或下方命令。

## 文件清单

| 文件 | 作用 |
| --- | --- |
| `conftest.py` | 公共夹具：`fake_report`（契约完整的假报告）、`llm_env`（注入 LLM 环境变量）、`assert_ai_report_schema`（Schema 断言） |
| `fixture_report.json` | 与 `conftest._FAKE_REPORT` 同源的落盘 fixture（供其他子任务/前端直接复用），由 `test_generate.py::test_fixture_file_matches_conftest` 保证不漂移 |
| `test_knowledge.py` | 知识库 4 份规范齐全、8 类设施都有条文支撑、检索可复现 |
| `test_validation.py` | 数值抽取（含条号剔除、中文数字、百分比）、数值集合构造、**数值一致性校验**（防幻觉闸门） |
| `test_client.py` | 配置读取（effort / max_tokens / 超时）、JSON 宽松解析、重试策略、超时/鉴权/连接错误的**不外抛**行为 |
| `test_fallback.py` | 模板降级报告：Schema、真实条文引用、**模板自身数值可追溯**、优先级排序 |
| `test_generate.py` | `generate_report` 契约签名、模板路径、LLM 路径、**幻觉拦截 -> 重试 -> 降级**、畸形输入永不抛异常 |
| `smoke_llm.py` | **真实调用 DeepSeek** 的冒烟脚本（见下） |
| `smoke_llm_result.json` | `smoke_llm.py` 的真实运行结果（可直接作为交付证据，已确认不含任何密钥） |

## 命令

```powershell
# 单元测试（零网络，~100 用例）
cd backend
.\.venv\Scripts\python.exe -m pytest app/ai/tests -q

# 真实调用 DeepSeek 的冒烟（需要 .env 配好 LLM_API_KEY）
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe app\ai\tests\smoke_llm.py
```

> Windows 控制台是 GBK，脚本已自行 `sys.stdout.reconfigure(encoding="utf-8")`；
> 若仍要重定向，用 `Out-File -Encoding utf8`，不要用 `*>`（会生成 UTF-16 文件）。

## 已修复的两个"概率性降级"根因（2026-10-14，S1 现场复现）

两者都会让**真实演示里的 ai_report 间歇性退化为模板**，且都白烧一次 LLM 调用，
均已在 `validation.py` / `client.py` / `prompts.py` 修掉，并各有回归用例：

| # | 现象 | 根因 | 修复 | 回归用例 |
| --- | --- | --- | --- | --- |
| 1 | `first_reject_reason = "…（2026、-10、-14、14、18、21）"` | 校验器把 `meta.generated_at`（如 `2026-10-14T14:18:21+08:00`）的时间戳片段当成"编造的指标数值" → **误杀** | 抽数前先 `strip_dates()` 抹掉 ISO / 中文日期 / 纯时间形态；并把报告字符串字段（generated_at、address、uid…）里的数字纳入合法集合 | `test_validation.py::test_extract_ignores_dates_and_times`、`::test_validate_numbers_accepts_report_timestamp`、`::test_validate_numbers_accepts_report_string_fields` |
| 2 | `degrade_reason = "LLM 调用失败：输出不是合法 JSON"` | 推理模型的 `reasoning + 正文` 吃满 `max_tokens` → JSON **被截断**；旧代码把它交给 JSON 解析器，报出误导性的"不是合法 JSON" | `client.py` 把 `finish_reason == "length"` 单列为可诊断错误（带 usage 明细）；`prompts.py` 收紧 diagnosis（≤220 字）与单条建议长度 | `test_client.py::test_truncated_json_identified_as_length_limit` |

> 另有第三类偶发：模型首轮把 `70.75` 抄成 `70` 而被拦下（纠错重试可救回）。
> 已在系统提示词里要求"逐字符照抄数字，宁可少写一个也不要写错一个"，并收紧 diagnosis 长度。
> 这类属于模型的抄写抖动，**校验器不放宽** —— 它正是防幻觉闸门本身。

## 冒烟脚本验证的四件事

1. **真实推理模型调通**：`deepseek-flash` 返回 `reasoning_content`，usage 里有 `reasoning_tokens`；
2. **正常路径**：`generate_report(..., enable_llm=True)` 产出 `degraded=False` 且通过数值一致性校验；
3. **对抗注入**：提示词里要求模型编造报告中没有的数字。模型要么拒绝执行（输出干净），
   要么执行了而被闸门拦下并降级——两条路径都说明防线生效；
4. **确定性闸门 + 降级链路**：用假客户端强制喂入编造数字（零网络、可复现），
   必须 `degraded=True` + `strategy="template_after_llm_reject"`；
   同时验证无 LLM 时模板报告功能不缺失。

脚本最后做 **密钥扫描**：确认输出 JSON 中不含 `LLM_API_KEY` / 百度 AK / SK，也不含任何 `sk-****` 形态串。

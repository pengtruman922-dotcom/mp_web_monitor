# Multi-Agent 采集系统设计方案

## 背景与问题

当前系统使用单个 Agent 完成整个采集流程（浏览首页 → 各栏目列表页 → 保存条目），存在以下问题：

1. **Context rot**：每次 `browse_page` 返回约 15000 字，10+ 轮后 context 累积到 80K-100K 字，模型对提示词指令的遵循度严重下降
2. **缺少摘要**：采集 Agent 负担过重，摘要要么依赖后处理（串行、无重试、静默失败），要么依赖 Agent 在长 context 中写（质量差）
3. **日期缺失**：URL 中隐含的日期未被提取，提示词中的抽象规则在长 context 中被模型忽略
4. **无重要性排序**：输出按日期倒序，缺少咨询顾问视角的战略重要性排序

## 设计原则

- **Context 隔离**：每个 Agent/Sub-Agent 拿到干净的、精准的上下文，不携带前序阶段的噪声
- **职责单一**：每个 Agent 只做一件事，提示词短而明确，中等模型（GPT-5、Qwen3-max）也能可靠执行
- **代码管理上下文**：Agent 之间的数据传递由代码层控制，只传递精炼的结构化数据
- **渐进降级**：每个阶段独立容错，某阶段失败不阻塞整体流程

## 整体架构

```
_run_single_source(source, task_id)
│
├─ Phase 1a: 首页导航（代码调 browse_page + 一次 simple_completion）
│   Context: ~17K token → 用完即丢
│   输出: [{name: "政策法规", url: "..."}, ...]
│
├─ Phase 1b: 栏目采集 × N（每栏目独立 tool-calling agent，串行）
│   每个 agent context: ≤20K token（含 context 修剪）
│   输出: items[] → 汇总去重
│
├─ Phase 2: 摘要 Agent × M（并发 simple_completion，每条独立调用）
│   每次 context: ~3K token
│   输出: summary string → 回写到 items[]
│
├─ Phase 3: 排序 Agent（单次 simple_completion）
│   Context: ~2.5K token
│   输出: 排序后的序号数组 → 代码层重排 items[]
│
└─ 持久化入库
```

### 上下文传递

| 从 → 到 | 传递什么 | 不传递什么 |
|----------|---------|-----------|
| Phase 1a → 代码层 | `[{name, url}]` 栏目清单 | 首页原文 |
| Phase 1b → 代码层 | `items[]` (title/url/date/type) | 列表页原文、链接列表 |
| 代码层 → Phase 2 | 每条的 title + browse_page 返回文本 | 其他条目信息、Phase 1 context |
| Phase 2 → 代码层 | summary string | page_text |
| 代码层 → Phase 3 | 精炼清单: `[i] [type] date \| title — summary[:80]` | 页面原文、完整摘要、URL |
| Phase 3 → 代码层 | `[3, 0, 7, 1, ...]` 排序数组 | 排序推理过程 |

---

## Phase 1a: 首页导航

### 机制

不使用 tool-calling agent，由代码直接控制：

1. 代码调用 `browse_page(source.url)` 获取首页内容
2. 将首页内容 + focus_areas 传给 `simple_completion`
3. LLM 返回各栏目列表页 URL

### 提示词

**System:**
```
你是网页结构分析专家。请从网页内容中提取栏目列表页的URL。
```

**User:**
```
以下是 {source_name}（{source_url}）的首页内容。
请从中找出与以下栏目相关的列表页链接：{focus_areas}

要求：
- 返回JSON数组：[{"name": "栏目名", "url": "列表页完整URL"}]
- 只返回能进入文章列表的栏目页链接，不要文章详情链接
- 如果首页展示了多个栏目入口，都列出来
- 直接输出JSON，不加其他内容

首页内容：
{homepage_text}
```

### Context 生命周期

一次 `simple_completion` 调用，约 17K token。调用完成后，首页文本即被丢弃，不进入后续任何 Agent 的 context。

### 降级策略

如果 LLM 返回格式无法解析，回退为直接用 `source.url` 作为唯一栏目入口（与当前行为等效）。

---

## Phase 1b: 栏目采集子 Agent

### 机制

每个栏目启动一个独立的 tool-calling agent（复用 `run_agent`），拥有**完全干净的 context window**。

### 工具集

从 6 个精简为 4 个（`CRAWLER_TOOLS`）：

| 工具 | 说明 |
|------|------|
| `browse_page` | 浏览页面 |
| `save_results_batch` | 批量保存条目 |
| `save_result` | 单条保存（可选） |
| `finish` | 结束采集 |

移除 `download_file` 和 `read_document`，降低工具集复杂度。

### 提示词

```
你是政策信息采集助手。请采集以下栏目列表页中，日期范围内的内容条目。

## 任务
- **栏目**: {section_name}
- **列表页URL**: {section_url}
- **采集日期范围**: {date_range}
- **最大采集条数**: {max_items} 条

## 工作流程
1. 用 browse_page 打开列表页
2. browse_page 返回的末尾有"可直接采集的条目"JSON数组
   - 筛选日期范围内的条目，用 save_results_batch 批量保存
   - 对没有标注日期的条目，从URL路径中分析日期（见下方示例）
   - summary 字段留空
3. 如有"下一页"链接且未达到采集上限，翻页继续
4. 采集完成后调用 finish

## URL日期示例

没有显示日期时，从URL路径分析：

| URL | 日期 |
|-----|------|
| /20260203/xxx.html | 2026-02-03 |
| /2026-01/15/xxx.htm | 2026-01-15 |
| /art/2026/2/3/xxx.html | 2026-02-03 |
| /202601/t20260115_xxx.html | 2026-01-15 |

{existing_urls_section}
```

### 执行参数

- `max_turns=15`（单栏目不需要 50 轮）
- `enable_pruning=True`（启用 context 修剪）
- 串行执行各栏目（避免对同一网站并发爬取触发反爬）

### Context 修剪

在 agent 循环中，当 `save_results_batch` 执行成功后，将上一轮 `browse_page` 的返回内容替换为精简摘要：

```
原始: 15000字页面文本 + 链接列表 + 可采集条目JSON
替换为: "[已处理该页面，提取了N条结果，原始内容已省略以节省上下文]"
```

效果：每个栏目 agent 的 context 始终保持在 **≤20K 字**。

### 跨栏目去重

串行执行时，后面栏目 agent 的 `existing_urls_section` 包含前面栏目已采集的 URL，实现跨栏目去重。

---

## Phase 2: 摘要 Agent

### 机制

不使用 tool-calling agent。代码层为每条无摘要条目调用 `browse_page` 获取正文，然后发起独立的 `simple_completion` 调用。

每次调用是**完全隔离的 context**，只包含系统提示 + 标题 + 正文。

### 提示词

**System:**
```
你是政策情报分析师，服务于咨询公司的行业顾问团队。
请根据提供的文章正文撰写一段简明摘要，帮助顾问快速了解文章核心内容。
```

**User:**
```
请为以下文章撰写摘要。

要求：
- 2-3句话，100-200字
- 提炼核心政策要点、关键数据或主要措施
- 不要重复标题内容
- 直接输出摘要，不加前缀

标题：{title}

正文：
{page_text[:6000]}
```

### 代码层编排

```python
sem = asyncio.Semaphore(3)  # 同时最多3个并发

async def _process_one(item):
    async with sem:
        page_text = await browse_page(item["url"])   # 代码负责取页面
        if not page_text or "页面加载失败" in page_text:
            return
        summary = await _call_summary_agent(item["title"], page_text)
        if not summary:                               # 重试一次
            summary = await _call_summary_agent(item["title"], page_text)
        if summary:
            item["summary"] = summary

await asyncio.gather(*[_process_one(item) for item in needs_summary], return_exceptions=True)
```

### 校验规则

- 摘要不为空
- 摘要不等于标题
- 摘要长度 > 20 字（过短则丢弃）

### 降级策略

单条失败（重试 1 次后仍失败）则该条目摘要为空，不阻塞其他条目。

---

## Phase 3: 排序 Agent

### 机制

单次 `simple_completion` 调用。输入是精炼的条目清单（每条约 1 行），输出是排序后的编号数组。

### 输入构造

代码层将 items 压缩为紧凑文本：

```
[0] [政策] 2026-02-03 | 国务院关于加快构建全国统一电力市场体系的指导意见 — 提出到2030年全国统一电力市场体系基本建成...
[1] [新闻] 2026-02-02 | 国家能源局召开新闻发布会介绍能源形势 — 2025年全国能源消费总量同比增长3.2%...
[2] [通知] 2026-02-01 | 关于开展2026年整县屋顶分布式光伏开发试点的通知
[3] [新闻] 2026-01-30 | 局领导赴山西调研煤炭保供工作
...
```

每条仅包含：序号 + 类型 + 日期 + 标题 + 摘要片段（≤80字）。整个 context 约 2000-3000 token。

### 提示词

**System:**
```
你是咨询公司高级政策顾问，负责为企业客户筛选和排序政策情报。
```

**User:**
```
请将以下{N}条政策/新闻条目按战略重要性从高到低排序。

排序原则：
- 全局性政策（国家层面规划、法律法规、重大改革）排最前
- 行业政策、监管动态、标准规范次之
- 统计数据、行业报告再次
- 地方性通知、执行层面文件靠后
- 日常工作动态、人事任免、来访接待排最后
- 同等重要性的，日期较新的优先

请只返回排序后的编号JSON数组，如 [3, 0, 7, 1, 5]
不要输出任何其他内容。

条目列表：
{items_text}
```

### 结果处理

```python
sorted_indices = parse_json_array(result)

# 校验：整数数组，值在有效范围内
# 补齐被模型漏掉的条目（append 到末尾）
seen = set(sorted_indices)
for i in range(len(items)):
    if i not in seen:
        sorted_indices.append(i)

items = [items[i] for i in sorted_indices]
```

### 降级策略

排序 Agent 调用失败（返回格式错误、JSON 解析失败等），降级为按日期倒序排列。

---

## run_agent 改造

当前 `run_agent` 签名耦合了 `MonitorSource` 和提示词构建。改造为支持外部传入参数：

```python
async def run_agent(
    source: MonitorSource,
    existing_urls: list[str] | None = None,
    on_progress: ProgressCallback | None = None,
    cancel_event: asyncio.Event | None = None,
    # ---- 新增参数 ----
    system_prompt: str | None = None,    # 外部传入时跳过内部 build_system_prompt
    user_message: str | None = None,     # 外部传入首条 user 消息
    tools: list[dict] | None = None,     # 外部传入工具集（默认 ALL_TOOLS）
    max_turns: int | None = None,        # 外部传入轮次上限（默认 AGENT_MAX_TURNS）
    enable_pruning: bool = False,        # 是否启用 context 修剪
) -> AgentResult:
```

### Context 修剪逻辑

在 `enable_pruning=True` 时，当 `save_results_batch` 成功执行后，向前查找最近的 `browse_page` tool result，将其内容替换为精简摘要：

```python
if enable_pruning and fn_name == "save_results_batch" and saved_count > 0:
    for j in range(len(messages) - 1, -1, -1):
        if messages[j].get("role") == "tool" and len(messages[j].get("content", "")) > 2000:
            messages[j]["content"] = f"[已处理该页面，提取了{saved_count}条结果，原始内容已省略]"
            break
```

---

## 文件变更清单

| 文件 | 改动 |
|------|------|
| `app/agent/prompts.py` | 新增 `build_section_prompt()` 构建栏目级提示词；保留原 `build_system_prompt` 兼容 |
| `app/llm/schemas.py` | 新增 `CRAWLER_TOOLS` 列表（browse_page + save_results_batch + save_result + finish） |
| `app/agent/runtime.py` | `run_agent` 支持外部传入 prompt/tools/max_turns；增加 context 修剪逻辑 |
| `app/agent/orchestrator.py` | `_run_single_source` 改为四阶段流水线；新增 `_navigate_homepage`、`_crawl_all_sections`、`_call_summary_agent`、`_call_ranking_agent`；替换 `_enrich_summaries` |

共 **4 个文件**，无新增文件。

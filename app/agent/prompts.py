"""System prompts for the crawl agent."""

from datetime import datetime, timedelta
from string import Template


DEFAULT_CRAWL_RULES = """## 栏目筛选规则

### 优先采集的栏目类型
- 本单位工作动态、要闻（如"局工作动态"、"部门新闻"）
- 政策文件、法规（如"最新文件"、"政策法规"、"规范性文件"）
- 新闻发布、数据发布（如"新闻发布"、"统计数据"）
- 政策解读（如"解读回应"、"答记者问"）

### 应排除的栏目类型
- 名称含"派出机构"、"地方"、"区域"的栏目（地方性内容，价值较低）
- 名称含"项目核准"、"审批"、"注册登记"的栏目（行政审批流程）
- 名称含"留言"、"举报"、"互动"、"信访"、"咨询"的栏目（互动服务类）
- 名称含"简介"、"指南"、"机构设置"、"领导信息"的栏目（静态信息页）
- 名称含"年度报告"、"报表"的栏目（低频周期性文件）

### 栏目数量限制
- 最多选择5个栏目进行深入采集
- 内容高度相似的栏目应合并（如"最新文件"和"通知"只选一个）

### 内容优先级（从高到低）
1. 国家层面重大政策（法律法规、国务院文件、部委规章）
2. 高级领导人讲话、活动、人事变动
3. 全国性会议、全国性新闻、全国性数据发布
4. 行业报告、政策解读、标准规范
5. 地方性通知、地方项目（低优先级）
6. 地方监管局/监管办日常动态（最低优先级，一般跳过）"""


# Default prompt template using $variable syntax (compatible with string.Template).
# Curly braces {} are safe to use in the template text — only $var gets substituted.
DEFAULT_TEMPLATE = r"""你是一个政策信息采集助手。你的任务是从政府网站收集最近发布的新内容。

## 需求背景
采集到的政策信息将提供给咨询公司的行业顾问查阅，用于行业研究和政策分析。因此采集时应优先关注具有政策指导意义、行业影响力的内容（如法规、通知、规划、指导意见、统计数据、解读等），而非一般性的会议活动、来访接待、机构内部事务等。

## 当前任务
- **目标网站**: $source_name ($source_url)
- **关注栏目**: $focus_areas
- **采集日期范围**: $date_range（最近${time_range_days}天）
- **最大采集条数**: $max_items 条

## 工作流程

**第一步**: 用 `browse_page` 打开首页，找到各栏目列表页的链接。

**第二步**: 浏览每个栏目的列表页。`browse_page` 返回的结果末尾会有一个"可直接采集的条目"JSON数组，包含标题、链接和日期。
- 筛选出日期在 $date_range 范围内的条目。
- **日期判断方法**：优先看条目标注的 published_date；如果没有标注日期，从 URL 路径中提取日期（许多政府网站的 URL 包含发布日期，如 `/20260130/xxx/c.html` 表示 2026-01-30 发布）。两种方式都无法确定日期的条目可以跳过。
- **去重**：同一 URL 只保存一次（首页不同板块可能展示相同条目）。注意 `http://` 和 `https://` 的同一路径视为相同 URL。
- 用 `save_results_batch` 工具一次性批量保存（将筛选后的JSON数组作为 items_json 参数传入）。
- 从列表页批量保存时，不需要填写 summary 字段（留空即可），系统会自动处理。
- 这比逐条调用 save_result 高效得多，一次可保存整页所有条目。
- 如果列表页底部有"下一页"链接，翻页继续采集。

**第三步**: 对重要的政策文件，进入详情页用 `browse_page` 获取正文，然后用 `save_result` 单独保存。此时 summary 字段应填写对正文内容的精炼概括（200-500字），不能照抄标题。

**第四步**: 所有栏目都扫完后，调用 `finish` 结束。

## 保存工具说明
- `save_results_batch`: 批量保存列表页条目，参数 items_json 是JSON数组字符串。示例: [{"title":"标题","url":"http://...","published_date":"2026-01-30","content_type":"news"}]
- `save_result`: 单条保存详情页内容，需要 title, url, content_type, summary 参数。summary 必须是对原文的精炼概括，不能与标题相同。

## 内容筛选优先级
当日期范围内的条目数量超过 $max_items 条时，按以下优先级选取：
1. **政策法规类**：法律法规、管理办法、指导意见、实施细则、规划等正式政策文件
2. **行业数据类**：统计数据发布、行业发展报告、市场运行数据等
3. **政策解读类**：官方政策解读、答记者问、新闻发布会等
4. **重大行业动态**：全国性会议、重要项目核准、行业标准发布等
5. **一般工作动态**：地方监管动态、机构来访接待等（优先级最低）

## 重要提示
- 优先使用 `save_results_batch` 批量保存，效率更高。
- 批量保存时不要填写 summary（或留空字符串），避免与标题重复。
- 只有进入详情页阅读了正文后，才在 save_result 中填写有意义的 summary。
- 看到列表页有带日期的条目就直接保存，不需要逐条进详情页。
- 日期在 $date_range 范围之外的内容不要保存。
- 达到 $max_items 条时立即调用 finish。
- 如果网站无法访问，如实报告并 finish。
$existing_urls_section"""


# Available template variables (shown in the frontend editor)
TEMPLATE_VARIABLES = {
    "$source_name": "监控源名称（如：国家能源局）",
    "$source_url": "监控源网址",
    "$focus_areas": "关注栏目（已拼接为文本）",
    "$date_range": "采集日期范围（如：2026-01-27 至 2026-02-03）",
    "$time_range_days": "采集天数（如：7）",
    "$max_items": "最大采集条数（如：30）",
    "$existing_urls_section": "已采集URL列表（自动生成，可为空）",
}

# Sample values for preview rendering
PREVIEW_SAMPLE_VALUES = {
    "source_name": "国家能源局",
    "source_url": "https://www.nea.gov.cn/",
    "focus_areas": "政策法规、通知公告、工作动态",
    "date_range": "2026-01-27 至 2026-02-03",
    "time_range_days": "7",
    "max_items": "30",
    "existing_urls_section": "",
}


def build_system_prompt(
    source_name: str,
    source_url: str,
    focus_areas: list[str],
    max_depth: int,
    time_range_days: int = 7,
    max_items: int = 30,
    existing_urls: list[str] | None = None,
    custom_template: str | None = None,
) -> str:
    """Build the system prompt for a crawl agent instance.

    If custom_template is provided, it is used instead of DEFAULT_TEMPLATE.
    Template uses $variable syntax (Python string.Template).
    """
    # Calculate derived variables
    today = datetime.now()
    start_date = today - timedelta(days=time_range_days)
    date_range = f"{start_date.strftime('%Y-%m-%d')} 至 {today.strftime('%Y-%m-%d')}"

    focus_str = "、".join(focus_areas) if focus_areas else "所有栏目"

    # Build existing URLs section
    existing_urls_section = ""
    if existing_urls:
        urls_text = "\n".join(f"- {u}" for u in existing_urls[:100])
        existing_urls_section = (
            "\n## 已采集URL（请跳过）\n"
            "以下URL已在之前的采集中收录，请不要重复采集：\n"
            f"{urls_text}\n"
        )

    context = {
        "source_name": source_name,
        "source_url": source_url,
        "focus_areas": focus_str,
        "date_range": date_range,
        "time_range_days": str(time_range_days),
        "max_items": str(max_items),
        "existing_urls_section": existing_urls_section,
    }

    template_str = custom_template or DEFAULT_TEMPLATE
    return Template(template_str).safe_substitute(context)


def build_section_prompt(
    section_name: str,
    section_url: str,
    date_range: str,
    max_items: int = 30,
    existing_urls: list[str] | None = None,
    crawl_rules: str = "",
) -> str:
    """Build a focused system prompt for a Phase 1b section crawler sub-agent.

    Each section crawler gets a clean, concise prompt (~400 chars + URL list)
    with URL date examples as few-shot guidance.
    """
    existing_urls_section = ""
    if existing_urls:
        urls_text = "\n".join(f"- {u}" for u in existing_urls[:100])
        existing_urls_section = (
            "\n## 已采集URL（请跳过）\n"
            "以下URL已在之前的采集中收录，请不要重复采集：\n"
            f"{urls_text}\n"
        )

    # Extract content priority section from crawl_rules if available
    priority_section = ""
    if crawl_rules:
        # Look for the priority section in custom rules
        marker = "### 内容优先级"
        if marker in crawl_rules:
            priority_section = "\n## 内容筛选优先级（请严格遵守）\n" + crawl_rules[crawl_rules.index(marker):]
        else:
            priority_section = "\n## 采集规则（请严格遵守）\n" + crawl_rules

    if not priority_section:
        priority_section = """
## 内容筛选优先级
当条目数量超过上限时，优先保留以下内容：
1. 国家层面重大政策（法律法规、国务院文件、部委规划、指导意见）
2. 高级领导人讲话、重要批示、人事任免
3. 全国性新闻、全国性会议
4. 行业统计数据、发展报告
5. 地方性通知、执行层面文件（优先级较低）
6. 地方监管局日常工作动态、来访接待（优先级最低，可不采集）"""

    return f"""你是政策信息采集助手。请采集以下栏目列表页中，日期范围内的内容条目。

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
{priority_section}

## content_type 分类
- policy: 法规、规划、指导意见、管理办法等正式文件
- notice: 通知、公告、公示
- news: 新闻、动态、讲话、会议
- file: 附件、数据报告

## URL日期示例

没有显示日期时，从URL路径分析：

| URL | 日期 |
|-----|------|
| /20260203/xxx.html | 2026-02-03 |
| /2026-01/15/xxx.htm | 2026-01-15 |
| /art/2026/2/3/xxx.html | 2026-02-03 |
| /202601/t20260115_xxx.html | 2026-01-15 |
{existing_urls_section}"""

"""Orchestrator: manages parallel agent execution for multiple sources."""
import asyncio
import json
import logging
import re
import uuid
from collections import defaultdict
from datetime import datetime, date, timedelta

from sqlalchemy import select, delete

from app.database.connection import async_session
from app.models.source import MonitorSource
from app.models.task import CrawlTask, TaskStatus, TriggerType
from app.models.result import CrawlResult
from app.models.report import Report
from app.agent.runtime import run_agent, AgentResult
from app.agent.prompts import build_section_prompt, DEFAULT_CRAWL_RULES
from app.agent.tools.browser import browse_page, close_browser
from app.llm.client import simple_completion
from app.llm.schemas import CRAWLER_TOOLS
from app.notification.engine import dispatch_report
from app.config import AGENT_MAX_CONCURRENCY, LLM_MAX_CONCURRENCY

logger = logging.getLogger(__name__)

# Per-source lock to prevent concurrent runs on the same source
_running_sources: set[int] = set()

# task_id -> asyncio.Event, set() means cancellation requested
_cancel_flags: dict[int, asyncio.Event] = {}

# Section history: track consecutive empty runs per section
# source_id -> {section_url -> consecutive_empty_count}
_section_history: dict[int, dict[str, int]] = {}
_SECTION_SKIP_THRESHOLD = 3  # skip section after N consecutive empty runs

# Max sections to identify and crawl
MAX_SECTIONS = 5

# Regex for leading date pattern like "2026-02-06 "
_LEADING_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}\s*')


def _clean_title(title: str) -> str:
    """Clean a title by removing embedded dates and extra whitespace/newlines."""
    if not title:
        return title
    # Replace newlines with spaces
    title = title.replace('\n', ' ').replace('\r', ' ')
    # Strip leading date patterns like "2026-02-06 "
    title = _LEADING_DATE_RE.sub('', title)
    # Strip whitespace
    title = title.strip()
    return title


def request_cancel(task_id: int):
    """Mark a task for cancellation (called from the API layer)."""
    if task_id in _cancel_flags:
        _cancel_flags[task_id].set()


def release_source(source_id: int):
    """Remove a source from the running set (called from the API layer on cancel)."""
    _running_sources.discard(source_id)


def is_cancel_requested(task_id: int) -> bool:
    ev = _cancel_flags.get(task_id)
    return ev is not None and ev.is_set()


def is_running() -> bool:
    """Check if ANY source is currently running."""
    return bool(_running_sources)


def get_running_sources() -> set[int]:
    """Return the set of source IDs currently being crawled."""
    return set(_running_sources)


async def run_batch(
    source_ids: list[int] | None = None,
    triggered_by: str = TriggerType.manual.value,
    user_id: int = 1,
) -> str:
    """Run a crawl batch for the given sources (or all active sources).

    Returns the batch_id.
    """
    batch_id = uuid.uuid4().hex[:12]
    logger.info("Starting batch %s (triggered_by=%s, user_id=%d)", batch_id, triggered_by, user_id)

    runnable: list[MonitorSource] = []

    try:
        # Fetch sources
        async with async_session() as session:
            query = select(MonitorSource).where(
                MonitorSource.is_active == True,
                MonitorSource.user_id == user_id,
            )
            if source_ids:
                query = query.where(MonitorSource.id.in_(source_ids))
            result = await session.execute(query)
            sources = list(result.scalars().all())

        if not sources:
            logger.warning("No active sources found for batch %s", batch_id)
            return batch_id

        # Filter out sources that are already running
        already_running = []
        for src in sources:
            if src.id in _running_sources:
                already_running.append(src.name)
            else:
                runnable.append(src)

        if already_running:
            logger.info("Skipping already-running sources: %s", already_running)

        if not runnable:
            logger.warning("All requested sources are already running")
            return batch_id

        # Mark sources as running
        for src in runnable:
            _running_sources.add(src.id)

        # Create task records
        tasks_map: dict[int, int] = {}  # source_id -> task_id
        async with async_session() as session:
            for src in runnable:
                task = CrawlTask(
                    batch_id=batch_id,
                    source_id=src.id,
                    source_name=src.name,
                    status=TaskStatus.pending.value,
                    triggered_by=triggered_by,
                    user_id=user_id,
                )
                session.add(task)
            await session.commit()

            # Re-fetch to get IDs
            q = await session.execute(
                select(CrawlTask).where(CrawlTask.batch_id == batch_id)
            )
            for t in q.scalars():
                tasks_map[t.source_id] = t.id

        # Run agents in parallel with concurrency limit
        sem = asyncio.Semaphore(AGENT_MAX_CONCURRENCY)

        async def _limited_run(src, tid, bid):
            async with sem:
                try:
                    await _run_single_source(src, tid, bid, user_id=user_id)
                finally:
                    _running_sources.discard(src.id)

        agent_tasks = [_limited_run(src, tasks_map[src.id], batch_id) for src in runnable]
        await asyncio.gather(*agent_tasks, return_exceptions=True)

        # Generate and dispatch report
        await _generate_report(batch_id, user_id=user_id)

    except Exception as e:
        logger.error("Batch %s failed: %s", batch_id, e)
        # Release all locks on error
        for src in runnable:
            _running_sources.discard(src.id)
    finally:
        # Only close browser when no other sources are still running
        # (another concurrent batch may still be using it)
        if not _running_sources:
            await close_browser()

    return batch_id


async def _get_existing_urls(source_id: int) -> list[str]:
    """Fetch all previously crawled URLs for a source (for deduplication)."""
    async with async_session() as session:
        result = await session.execute(
            select(CrawlResult.url).where(CrawlResult.source_id == source_id)
        )
        return [row[0] for row in result.all()]


##############################################################################
# Phase 1a: Homepage — extract items (pure code) + identify sections (LLM)
##############################################################################

def _normalize_date(d: str) -> str:
    """Normalize a date string to YYYY-MM-DD with zero-padding.

    Handles formats like '2026-2-3' -> '2026-02-03'.
    """
    parts = d.split('-')
    if len(parts) == 3:
        try:
            return f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
        except Exception:
            pass
    return d


def _extract_homepage_items(
    page_text: str,
    date_start: str,
    date_end: str,
    source_url: str = "",
) -> list[dict]:
    """Extract directly-harvestable items from browse_page output (no LLM).

    Looks for the "--- 可直接采集的条目" marker, parses the JSON array,
    filters by date range, and deduplicates by URL.

    Returns: [{"title", "url", "published_date", ...}, ...]
    """
    items_marker = "--- 可直接采集的条目"
    if items_marker not in page_text:
        return []

    items_text = page_text[page_text.index(items_marker):]
    # Find JSON array in the text
    match = re.search(r"\[.*\]", items_text, re.DOTALL)
    if not match:
        return []

    try:
        raw_items = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []

    if not isinstance(raw_items, list):
        return []

    # Filter by date range and deduplicate
    seen_urls = set()
    filtered = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        url = item.get("url", "")
        if not url or not item.get("title"):
            continue
        # Normalize http/https
        norm_url = url.replace("http://", "https://")
        if norm_url in seen_urls:
            continue
        seen_urls.add(norm_url)

        pub_date = item.get("published_date", "")
        if pub_date:
            pub_date = _normalize_date(pub_date)
            item["published_date"] = pub_date  # write back normalized
            if pub_date < date_start or pub_date > date_end:
                continue
        # Items without date are kept (may be relevant)
        filtered.append(item)

    # Domain filtering: remove cross-domain (reposted) items
    if source_url and filtered:
        from app.agent.domain_filter import filter_by_domain
        before = len(filtered)
        filtered = filter_by_domain(filtered, source_url)
        if len(filtered) < before:
            logger.info("Homepage domain filter: %d -> %d items", before, len(filtered))

    return filtered


# Regex pattern for detecting local regulatory bureau dynamics in titles
_LOCAL_DYNAMICS_RE = re.compile(
    r"^(?:华北|华东|华中|南方|东北|西北|山东|江苏|浙江|广东|福建|四川|"
    r"湖南|湖北|河南|河北|安徽|江西|云南|贵州|陕西|甘肃|山西|辽宁|吉林|"
    r"黑龙江|广西|海南|内蒙古|新疆|西藏|青海|宁夏|重庆|天津|上海|北京)"
    r"(?:省|市|自治区)?"
    r"(?:能源监管局|能源监管办|发改委|发展改革委|经信委|工信厅|能源局)"
)


def _is_local_dynamics(title: str) -> bool:
    """Check if a title matches local regulatory bureau dynamics pattern."""
    return bool(_LOCAL_DYNAMICS_RE.search(title))


async def _filter_homepage_items(
    items: list[dict],
    crawl_rules: str,
    on_progress=None,
) -> list[dict]:
    """Use regex pre-filter + LLM to filter homepage items, removing low-value content.

    Two-stage filtering:
    1. Regex pre-annotation: mark items whose titles match local bureau patterns
    2. LLM filtering with few-shot examples for accurate classification

    Returns a subset of items that pass the quality filter.
    Falls back to regex-only filtering on LLM failure.
    """
    if len(items) <= 3:
        return items

    # Stage 1: Regex pre-annotation
    local_flags = []
    for item in items:
        title = item.get("title", "")
        local_flags.append(_is_local_dynamics(title))

    local_count = sum(local_flags)
    logger.info("Homepage filter: %d/%d items flagged as local dynamics by regex",
                local_count, len(items))

    # If no local dynamics detected, skip LLM filtering
    if local_count == 0:
        return items

    # Stage 2: LLM filtering with enhanced prompt and few-shot examples
    lines = []
    for i, item in enumerate(items):
        title = item.get("title", "")
        url = item.get("url", "")[:80]
        date = item.get("published_date", "")
        tag = " [疑似地方]" if local_flags[i] else ""
        lines.append(f"[{i}] {date} | {title}{tag} | {url}")

    items_text = "\n".join(lines)

    system = (
        "你是政策信息筛选专家，服务于咨询公司行业顾问。"
        "你的核心任务是过滤掉地方监管局的日常工作动态，只保留全国性、国家级的高价值内容。"
    )
    user = (
        f"请从以下 {len(items)} 条条目中，筛选出值得保留的高价值内容。\n\n"
        f"## 过滤规则（必须严格执行）\n\n"
        f"### 必须过滤的内容（地方监管动态）\n"
        f"标题以地方机构名开头的条目属于地方监管动态，应当过滤：\n"
        f"- 地方机构前缀：华北、华东、华中、南方、东北、西北 + 能源监管局/监管办\n"
        f"- 省级机构：XX省发改委、XX省能源局、XX市XX局\n"
        f"- 标记为 [疑似地方] 的条目大概率应过滤\n\n"
        f"过滤示例：\n"
        f'- "华北能源监管局强化河北南网电力保供监管" → 过滤\n'
        f'- "华东能源监管局赴江苏能源监管办开展调研" → 过滤\n'
        f'- "南方能源监管局召开安全生产例会" → 过滤\n'
        f'- "山东能源监管办开展春节保供电检查" → 过滤\n'
        f'- "东北能源监管局组织召开辽宁电力市场座谈会" → 过滤\n\n'
        f"### 必须保留的内容（全国性高价值）\n"
        f"- 国家级机构发布：国家能源局、国务院、部委等\n"
        f"- 高级领导人活动：习近平、国务院总理、部长级\n"
        f"- 全国性数据/会议/政策\n\n"
        f"保留示例：\n"
        f'- "国家能源局新闻发布会文字实录" → 保留\n'
        f'- "国家能源局发布全国电力统计数据" → 保留\n'
        f'- "习近平同越共中央总书记通电话" → 保留\n'
        f'- "2025年度能源行业十大科技创新成果" → 保留\n\n'
        f"## 采集规则\n{crawl_rules}\n\n"
        f"## 条目列表\n{items_text}\n\n"
        f"请返回保留的编号JSON数组，如 [0, 3, 5]。直接输出JSON，不加其他内容。"
    )

    try:
        raw = await simple_completion(user, system=system, temperature=0.1, max_tokens=512)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```\w*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
            raw = raw.strip()
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            indices = json.loads(match.group(0))
            if isinstance(indices, list):
                valid = [i for i in indices if isinstance(i, int) and 0 <= i < len(items)]
                if valid:
                    # Safety: if filter is too aggressive (< 3 items), keep all non-local items
                    if len(valid) < 3:
                        non_local = [i for i in range(len(items)) if not local_flags[i]]
                        valid = sorted(set(valid + non_local))
                    if on_progress:
                        await on_progress(
                            f"Phase 1a: 质量筛选 {len(items)} → {len(valid)} 条"
                            f"（正则标记 {local_count} 条地方动态）"
                        )
                    logger.info("Homepage filter: %d -> %d items (regex flagged %d local)",
                                len(items), len(valid), local_count)
                    return [items[i] for i in valid]
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Homepage item filtering LLM failed, applying regex-only filter: %s", e)

    # Fallback: use regex-only filtering when LLM fails or returns invalid result
    non_local = [item for item, is_local in zip(items, local_flags) if not is_local]
    if non_local:
        logger.info("Homepage filter (regex fallback): %d -> %d items", len(items), len(non_local))
        return non_local

    return items


def _merge_similar_sections(sections: list[dict]) -> list[dict]:
    """Merge sections that share a common URL path prefix.

    E.g., /zcfg/zxwj/, /zcfg/tz/, /zcfg/gg/ -> single /zcfg/ entry.
    This prevents crawling many sub-categories of the same parent section.
    """
    if len(sections) <= MAX_SECTIONS:
        return sections

    from urllib.parse import urlparse

    # Group by 2-level path prefix (e.g., /zcfg/xxx -> /zcfg)
    groups: dict[str, list[dict]] = defaultdict(list)
    for s in sections:
        url = s.get("url", "")
        parsed = urlparse(url)
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        # Use first path segment as group key (e.g., "zcfg", "gzdt", "xwfb")
        prefix = parts[0] if parts else ""
        key = f"{parsed.scheme}://{parsed.netloc}/{prefix}"
        groups[key].append(s)

    merged = []
    for key, group in groups.items():
        if len(group) == 1:
            merged.append(group[0])
        else:
            # Multiple sub-sections share a parent path.
            # Keep the one with the shortest URL (likely the parent listing page).
            # If URLs are same length, keep the first one.
            parent = min(group, key=lambda s: len(s.get("url", "")))
            # Use a combined name to indicate merged sections
            names = [s.get("name", "") for s in group]
            if len(names) > 2:
                parent["name"] = f"{names[0]}等{len(names)}个子栏目"
            logger.info("Merged %d sub-sections under %s: %s",
                        len(group), parent.get("url"), [s.get("name") for s in group])
            merged.append(parent)

    # Hard cap
    if len(merged) > MAX_SECTIONS:
        merged = merged[:MAX_SECTIONS]

    return merged


async def _identify_sections(
    page_text: str,
    source: MonitorSource,
    on_progress=None,
) -> list[dict]:
    """Use LLM to identify section list-page URLs from the homepage.

    Injects source.crawl_rules into the prompt.
    Returns: [{"name": "栏目名", "url": "列表页URL"}, ...]
    Falls back to [{"name": source.name, "url": source.url}] on failure.
    """
    fallback = [{"name": source.name, "url": source.url}]
    crawl_rules = source.crawl_rules or DEFAULT_CRAWL_RULES

    # Extract just the link list section
    link_section = ""
    link_marker = "--- 页面链接列表 ---"
    if link_marker in page_text:
        link_section = page_text[page_text.index(link_marker):]
        items_marker = "--- 可直接采集的条目"
        if items_marker in link_section:
            link_section = link_section[:link_section.index(items_marker)]
    if not link_section:
        link_section = page_text[:8000]

    system = "你是网页结构分析专家。请从链接列表中识别出值得深入采集的栏目列表页URL。"
    user = (
        f"以下是 {source.name}（{source.url}）首页的链接列表。\n"
        f"请从中找出值得深入采集的栏目列表页链接。\n\n"
        f"## 栏目筛选规则（请严格遵守）\n{crawl_rules}\n\n"
        f"## 数量限制（非常重要）\n"
        f"- 最多返回 {MAX_SECTIONS} 个栏目，优先选择高价值栏目\n"
        f"- 内容高度相似的栏目必须合并：如果一个大栏目下有多个子栏目（如\"政策\"下有\"最新文件\"\"通知\"\"公告\"等），只返回大栏目的入口URL，不要分别列出每个子栏目\n"
        f"- 排除地方性栏目（名称含\"派出\"\"地方\"\"区域\"的栏目）\n"
        f"- 排除互动服务类栏目（名称含\"留言\"\"举报\"\"互动\"\"信访\"\"咨询\"）\n"
        f"- 排除静态信息栏目（名称含\"简介\"\"指南\"\"机构设置\"\"领导信息\"）\n\n"
        f"要求：\n"
        f"- 返回JSON数组：[{{\"name\": \"栏目名\", \"url\": \"列表页完整URL\"}}]\n"
        f"- 只返回能进入文章列表的栏目页链接（如 /zcfg/、/tzgg/、/gzdt/ 等栏目入口），不要具体文章详情链接\n"
        f"- 栏目入口URL通常较短、不含日期，文章URL通常较长、含日期路径\n"
        f"- 直接输出JSON，不加其他内容\n\n"
        f"链接列表：\n{link_section}"
    )

    try:
        raw = await simple_completion(user, system=system, temperature=0.1, max_tokens=2048)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```\w*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
            raw = raw.strip()
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            raw = match.group(0)
        sections = json.loads(raw)
        if isinstance(sections, list) and sections:
            valid = [s for s in sections if isinstance(s, dict) and s.get("url")]
            if valid:
                raw_count = len(valid)
                # Post-processing: merge similar sections and enforce hard cap
                valid = _merge_similar_sections(valid)
                if on_progress:
                    msg = f"Phase 1a: 发现 {raw_count} 个栏目"
                    if len(valid) < raw_count:
                        msg += f"，合并/精简为 {len(valid)} 个"
                    await on_progress(msg)
                logger.info("[%s] Homepage navigation: %d raw -> %d after merge (max %d)",
                            source.name, raw_count, len(valid), MAX_SECTIONS)
                return valid
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("[%s] Homepage navigation LLM parse failed: %s", source.name, e)

    if on_progress:
        await on_progress("Phase 1a: 栏目提取失败，降级为直接使用源URL")
    return fallback


##############################################################################
# Phase 1b: Section-level crawling — independent sub-agents per section
##############################################################################

async def _crawl_all_sections(
    source: MonitorSource,
    sections: list[dict],
    existing_urls: list[str],
    cancel_event: asyncio.Event | None,
    on_progress=None,
    crawl_rules: str = "",
) -> list[dict]:
    """Run a crawler sub-agent for each section (serial), return merged items.

    Each sub-agent gets a clean context window with enable_pruning=True.
    Later sections receive URLs from earlier sections for cross-section dedup.
    """
    time_range_days = source.time_range_days or 7
    max_items = source.max_items or 30
    today = datetime.now()
    start_date = today - timedelta(days=time_range_days)
    date_range = f"{start_date.strftime('%Y-%m-%d')} 至 {today.strftime('%Y-%m-%d')}"

    all_items: list[dict] = []
    collected_urls = set(existing_urls)

    for idx, section in enumerate(sections):
        if cancel_event and cancel_event.is_set():
            break

        section_name = section.get("name", f"栏目{idx + 1}")
        section_url = section.get("url", "")
        if not section_url:
            continue

        if on_progress:
            await on_progress(f"Phase 1b: 采集栏目 ({idx + 1}/{len(sections)}): {section_name}")

        # Build prompt with cross-section dedup URLs
        section_prompt = build_section_prompt(
            section_name=section_name,
            section_url=section_url,
            date_range=date_range,
            max_items=max_items - len(all_items),  # remaining quota
            existing_urls=list(collected_urls) if collected_urls else None,
            crawl_rules=crawl_rules,
        )

        user_msg = f"请开始采集栏目「{section_name}」的列表页：{section_url}"

        try:
            agent_result = await run_agent(
                source,
                existing_urls=list(collected_urls),
                on_progress=on_progress,
                cancel_event=cancel_event,
                system_prompt=section_prompt,
                user_message=user_msg,
                tools=CRAWLER_TOOLS,
                max_turns=20,
                enable_pruning=True,
                crawl_rules=crawl_rules,
            )

            # Collect items and update URL set for next section
            section_item_count = 0
            for item in agent_result.items:
                url = item.get("url", "")
                if url and url not in collected_urls:
                    collected_urls.add(url)
                    all_items.append(item)
                    section_item_count += 1

            # Record section history (consecutive empty count)
            if source.id not in _section_history:
                _section_history[source.id] = {}
            if section_item_count > 0:
                _section_history[source.id][section_url] = 0  # reset on success
            else:
                prev = _section_history[source.id].get(section_url, 0)
                _section_history[source.id][section_url] = prev + 1

            logger.info("[%s] Section '%s': %d items", source.name, section_name, section_item_count)

        except Exception as e:
            logger.error("[%s] Section '%s' agent failed: %s", source.name, section_name, e)
            if on_progress:
                await on_progress(f"栏目 {section_name} 采集失败: {e}")
            continue

        # Stop if we've reached the max
        if len(all_items) >= max_items:
            break

    return all_items


##############################################################################
# Phase 2: Summary agent — concurrent simple_completion per item
##############################################################################

def _parse_summary_and_tags(raw: str) -> tuple[str, str]:
    """Parse LLM response into (summary, comma_separated_tags).

    Expected format:
        摘要正文...
        标签：关键词1,关键词2,关键词3
    """
    lines = raw.strip().split("\n")
    tags = ""
    summary_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("标签：") or stripped.startswith("标签:"):
            tag_part = stripped.split("：", 1)[-1] if "：" in stripped else stripped.split(":", 1)[-1]
            # Normalize separators: 、 or space to comma
            tag_part = tag_part.replace("、", ",").replace(" ", ",")
            # Clean up: remove empty, strip whitespace, deduplicate
            tag_list = [t.strip() for t in tag_part.split(",") if t.strip()]
            tags = ",".join(dict.fromkeys(tag_list))  # deduplicate preserving order
        else:
            summary_lines.append(line)
    summary = "\n".join(summary_lines).strip()
    return summary, tags


async def _summarize_items(
    items: list[dict],
    cancel_event: asyncio.Event | None,
    on_progress=None,
):
    """Generate summaries for items that don't have one.

    Each item gets an independent simple_completion call with clean context.
    Runs with bounded concurrency (LLM_MAX_CONCURRENCY).
    """
    needs_summary = [i for i in items if not i.get("summary")]
    if not needs_summary:
        return

    if on_progress:
        await on_progress(f"Phase 2: 为 {len(needs_summary)} 条内容生成摘要")
    logger.info("Phase 2: generating summaries for %d items", len(needs_summary))

    sem = asyncio.Semaphore(LLM_MAX_CONCURRENCY)
    summary_system = (
        "你是政策情报分析师，服务于咨询公司的行业顾问团队。\n"
        "请根据提供的文章正文撰写一段简明摘要，准确判断内容类型（content_type），并提取与文章核心主题直接相关的关键词标签。\n"
        "标签必须紧扣文章实际内容所属的行业领域和具体议题，不要套用与文章无关的热门标签。"
    )

    async def _process_one(item, idx):
        if cancel_event and cancel_event.is_set():
            return
        url = item.get("url", "")
        title = item.get("title", "")
        if not url:
            return

        async with sem:
            try:
                if on_progress:
                    await on_progress(f"摘要 ({idx + 1}/{len(needs_summary)}): {title[:50]}")

                page_text = await browse_page(url)
                if not page_text or "页面加载失败" in page_text:
                    return

                user_prompt = (
                    f"请为以下文章撰写摘要、判断内容类型并提取关键词标签。\n\n"
                    f"要求：\n"
                    f"- 摘要：2-3句话，100-200字，提炼核心政策要点、关键数据或主要措施，不要重复标题\n"
                    f"- 内容类型（content_type）：根据文章性质准确判断，只能选以下之一：\n"
                    f"  - policy：法律法规、国务院令、部委规章、条例、管理办法、指导意见、实施方案\n"
                    f"  - notice：通知、公告、培训班通知、会议通知、人事任免、招标公告、公示\n"
                    f"  - news：一般新闻报道、评论、分析、领导讲话、会议报道、工作动态\n"
                    f"  - file：报告、白皮书、数据发布、统计公报、研究成果、规划文本\n"
                    f"- 标签：2-3个与文章核心主题直接相关的关键词标签（如行业领域、政策类型、具体议题），"
                    f"避免过于宽泛的标签。标签应反映文章来源网站的领域特征，"
                    f"例如教育培训类网站的文章应使用继续教育、培训管理、学历提升等标签，"
                    f"而非国企改革、人事等无关标签\n\n"
                    f"输出格式（严格遵守）：\n"
                    f"content_type: policy/notice/news/file\n"
                    f"摘要正文内容...\n"
                    f"标签：关键词1,关键词2,关键词3\n\n"
                    f"标题：{title}\n"
                    f"来源URL：{url}\n\n"
                    f"正文：\n{page_text[:6000]}"
                )

                raw = await simple_completion(
                    user_prompt, system=summary_system, temperature=0.2, max_tokens=512
                )
                raw = raw.strip()

                # Extract content_type line before parsing summary/tags
                content_type = None
                ct_lines = []
                for _line in raw.split("\n"):
                    _stripped = _line.strip()
                    if _stripped.lower().startswith("content_type:"):
                        ct_val = _stripped.split(":", 1)[1].strip().lower()
                        if ct_val in ("policy", "notice", "news", "file"):
                            content_type = ct_val
                    else:
                        ct_lines.append(_line)
                raw_for_parse = "\n".join(ct_lines)

                # Parse tags from response
                summary, tags = _parse_summary_and_tags(raw_for_parse)

                # Validate
                if not summary or summary == title.strip() or len(summary) < 20:
                    # Retry once
                    raw = await simple_completion(
                        user_prompt, system=summary_system, temperature=0.3, max_tokens=512
                    )
                    raw = raw.strip()

                    # Re-extract content_type from retry
                    ct_lines = []
                    for _line in raw.split("\n"):
                        _stripped = _line.strip()
                        if _stripped.lower().startswith("content_type:"):
                            ct_val = _stripped.split(":", 1)[1].strip().lower()
                            if ct_val in ("policy", "notice", "news", "file"):
                                content_type = ct_val
                        else:
                            ct_lines.append(_line)
                    raw_for_parse = "\n".join(ct_lines)
                    summary, tags = _parse_summary_and_tags(raw_for_parse)

                if summary and summary != title.strip() and len(summary) >= 20:
                    item["summary"] = summary
                    if tags:
                        item["tags"] = tags
                    if content_type:
                        item["content_type"] = content_type
            except Exception as e:
                logger.warning("Summary failed for %s: %s", url, e)

    await asyncio.gather(
        *[_process_one(item, idx) for idx, item in enumerate(needs_summary)],
        return_exceptions=True,
    )

    generated = sum(1 for i in needs_summary if i.get("summary"))
    if on_progress:
        await on_progress(f"Phase 2: 完成，{generated}/{len(needs_summary)} 条摘要生成成功")
    logger.info("Phase 2 done: %d/%d summaries generated", generated, len(needs_summary))

    # Fallback: generate tags from title if LLM didn't provide any
    for item in items:
        if not item.get("tags"):
            title = item.get("title", "")
            # Simple keyword extraction from title
            keywords = [w for w in re.split(r'[，,、：:|\s]+', title) if len(w) >= 2 and len(w) <= 8][:3]
            if keywords:
                item["tags"] = ",".join(keywords)


##############################################################################
# Phase 3: Ranking agent — single simple_completion
##############################################################################

async def _rank_items(items: list[dict], on_progress=None) -> list[dict]:
    """Rank items by strategic importance using a single LLM call.

    Falls back to date-descending order on failure.
    """
    if len(items) <= 1:
        return items

    if on_progress:
        await on_progress("Phase 3: 按战略重要性排序")
    logger.info("Phase 3: ranking %d items", len(items))

    # Build compact text: [i] [type] date | title — summary[:80]
    type_map = {"news": "新闻", "policy": "政策", "notice": "通知", "file": "文件"}
    lines = []
    for i, item in enumerate(items):
        type_label = type_map.get(item.get("content_type", ""), "内容")
        d = item.get("published_date", "")
        title = item.get("title", "")
        summary_snippet = (item.get("summary") or "")[:80]
        line = f"[{i}] [{type_label}] {d} | {title}"
        if summary_snippet:
            line += f" — {summary_snippet}"
        lines.append(line)

    items_text = "\n".join(lines)

    system = "你是咨询公司高级政策顾问，负责为企业客户筛选和排序政策情报。你非常善于区分国家级和地方级内容的重要性差异。"
    user = (
        f"请将以下{len(items)}条政策/新闻条目按战略重要性从高到低排序。\n\n"
        f"排序原则（严格按层级排序，高层级的一定排在低层级前面）：\n\n"
        f"第一层（最重要）：\n"
        f"- 国家层面重大政策：国务院、部委发布的法律法规、规划纲要、指导意见、改革方案\n"
        f"- 高级领导人（国家级、部级）讲话、批示、署名文章\n"
        f"- 高级领导人事任免（部级及以上）\n\n"
        f"第二层：\n"
        f"- 全国性重要会议（国务院常务会议、部委工作会议、全国性行业会议）\n"
        f"- 全国性重大新闻（全国数据发布、重大项目、行业里程碑）\n"
        f"- 国家级行业标准、规范发布\n\n"
        f"第三层：\n"
        f"- 部委通知、公告\n"
        f"- 行业统计数据、发展报告\n"
        f"- 政策解读、答记者问\n\n"
        f"第四层：\n"
        f"- 地方性政策文件、省级通知\n"
        f"- 地方项目核准、地方会议\n\n"
        f"第五层（最不重要）：\n"
        f"- 地方监管局日常工作动态\n"
        f"- 来访接待、调研视察（非高级领导）\n"
        f"- 一般性工作简报\n\n"
        f"关键判断方法：标题中含有\"国务院\"\"国家\"\"全国\"\"部\"等关键词的通常是第一、二层；含有省份名、\"XX局\"\"XX办\"等地方机构名的通常是第四、五层。\n"
        f"同一层级内，日期较新的优先。\n\n"
        f"请只返回排序后的编号JSON数组，如 [3, 0, 7, 1, 5]\n"
        f"不要输出任何其他内容。\n\n"
        f"条目列表：\n{items_text}"
    )

    try:
        raw = await simple_completion(user, system=system, temperature=0.1, max_tokens=1024)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```\w*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
            raw = raw.strip()

        sorted_indices = json.loads(raw)

        if isinstance(sorted_indices, list):
            # Validate: integers in range
            valid = [i for i in sorted_indices if isinstance(i, int) and 0 <= i < len(items)]
            # Append any missing indices
            seen = set(valid)
            for i in range(len(items)):
                if i not in seen:
                    valid.append(i)

            ranked = [items[i] for i in valid]
            if on_progress:
                await on_progress("Phase 3: 排序完成")
            logger.info("Phase 3: ranking succeeded")
            return ranked

    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Phase 3 ranking failed, falling back to date sort: %s", e)

    # Fallback: sort by date descending
    if on_progress:
        await on_progress("Phase 3: 排序失败，降级为按日期排序")

    def sort_key(item):
        d = item.get("published_date", "")
        return d if d else "0000-00-00"
    items.sort(key=sort_key, reverse=True)
    return items


##############################################################################
# Main pipeline: _run_single_source
##############################################################################

async def _run_single_source(source: MonitorSource, task_id: int, batch_id: str, user_id: int = 1):
    """Run the 4-phase pipeline for a single source and persist results."""
    cancel_event = asyncio.Event()
    _cancel_flags[task_id] = cancel_event

    # Mark task as running
    async with async_session() as session:
        task = await session.get(CrawlTask, task_id)
        task.status = TaskStatus.running.value
        task.started_at = datetime.utcnow()
        await session.commit()

    try:
        async def _on_progress(msg: str):
            try:
                async with async_session() as sess:
                    t = await sess.get(CrawlTask, task_id)
                    timestamp = datetime.utcnow().strftime("%H:%M:%S")
                    t.progress_log = (t.progress_log or "") + f"[{timestamp}] {msg}\n"
                    await sess.commit()
            except Exception:
                pass

        existing_urls = await _get_existing_urls(source.id)
        max_items = source.max_items or 30
        crawl_rules = source.crawl_rules or DEFAULT_CRAWL_RULES

        time_range_days = source.time_range_days or 7
        today = datetime.now()
        start_date = today - timedelta(days=time_range_days)
        date_start = start_date.strftime('%Y-%m-%d')
        date_end = today.strftime('%Y-%m-%d')

        # ── Phase 1a: Browse homepage, extract items + identify sections ──
        if is_cancel_requested(task_id):
            logger.info("[%s] Task %d cancelled", source.name, task_id)
            return

        await _on_progress("Phase 1a: 浏览首页，提取条目和栏目链接")

        try:
            homepage_text = await browse_page(source.url)
        except Exception as e:
            logger.warning("[%s] Failed to browse homepage: %s", source.name, e)
            homepage_text = ""

        if not homepage_text or "页面加载失败" in homepage_text:
            homepage_text = ""

        # Step 1: Extract directly-harvestable items (pure code, no LLM)
        # Disable domain filter if crawl_rules allow cross-domain content
        effective_source_url = "" if "允许跨域" in crawl_rules else source.url
        homepage_items = _extract_homepage_items(homepage_text, date_start, date_end, source_url=effective_source_url) if homepage_text else []

        # Step 2: Identify sections via LLM (with crawl_rules injection)
        sections = await _identify_sections(homepage_text, source, on_progress=_on_progress) if homepage_text else [{"name": source.name, "url": source.url}]

        await _on_progress(f"Phase 1a: 首页提取 {len(homepage_items)} 条条目，{len(sections)} 个栏目")

        # Step 3: LLM quality filter — apply crawl_rules to homepage items
        if homepage_items:
            homepage_items = await _filter_homepage_items(
                homepage_items, crawl_rules, on_progress=_on_progress,
            )

        await _on_progress(f"Phase 1a: 筛选后保留 {len(homepage_items)} 条首页条目")

        if is_cancel_requested(task_id):
            logger.info("[%s] Task %d cancelled", source.name, task_id)
            return

        # ── Phase 1b: Selective section crawling (Plan B) ──
        remaining = max_items - len(homepage_items)
        if remaining <= 0:
            sections_to_crawl = []
            await _on_progress("Phase 1b: 首页条目已足够，跳过栏目补充采集")
        else:
            # Filter out sections that yielded 0 items in previous runs
            # Only apply when sections were identified by LLM (not the fallback path)
            history = _section_history.get(source.id, {})
            if history and homepage_text:
                before_filter = len(sections)
                sections = [
                    s for s in sections
                    if history.get(s.get("url", ""), 0) < _SECTION_SKIP_THRESHOLD
                ]
                skipped = before_filter - len(sections)
                if skipped > 0:
                    logger.info("[%s] Skipped %d sections (empty %d+ consecutive runs)", source.name, skipped, _SECTION_SKIP_THRESHOLD)
                    await _on_progress(f"Phase 1b: 跳过 {skipped} 个连续无数据栏目")

            sections_to_crawl = sections[:MAX_SECTIONS]
            await _on_progress(f"Phase 1b: 补充采集 {len(sections_to_crawl)} 个栏目")

        section_items = []
        if sections_to_crawl:
            # Pass homepage item URLs to avoid duplicates
            homepage_urls = [item.get("url", "") for item in homepage_items]
            combined_existing = existing_urls + homepage_urls

            section_items = await _crawl_all_sections(
                source, sections_to_crawl, combined_existing, cancel_event,
                on_progress=_on_progress, crawl_rules=crawl_rules,
            )

        if is_cancel_requested(task_id):
            logger.info("[%s] Task %d cancelled", source.name, task_id)
            return

        # Merge and deduplicate
        all_items = homepage_items + section_items
        existing_url_set = set(u.replace("http://", "https://") for u in existing_urls)
        seen_urls = set()
        seen_titles = set()
        deduped_items = []
        from app.agent.domain_filter import is_same_domain
        for item in all_items:
            url = item.get("url", "")
            norm_url = url.replace("http://", "https://")
            if norm_url in existing_url_set or norm_url in seen_urls:
                continue
            if url and effective_source_url and not is_same_domain(url, effective_source_url):
                continue
            # Title-based dedup
            norm_title = item.get("title", "").strip().lower()
            if norm_title and norm_title in seen_titles:
                continue
            seen_urls.add(norm_url)
            if norm_title:
                seen_titles.add(norm_title)
            deduped_items.append(item)

        # Trim to max_items
        if len(deduped_items) > max_items:
            def sort_key(item):
                d = item.get("published_date", "")
                return d if d else "0000-00-00"
            deduped_items.sort(key=sort_key, reverse=True)
            deduped_items = deduped_items[:max_items]

        # ── Phase 2: Summary generation ──
        if is_cancel_requested(task_id):
            logger.info("[%s] Task %d cancelled", source.name, task_id)
            return

        await _summarize_items(deduped_items, cancel_event, on_progress=_on_progress)

        # ── Phase 3: Strategic ranking ──
        if is_cancel_requested(task_id):
            logger.info("[%s] Task %d cancelled", source.name, task_id)
            return

        deduped_items = await _rank_items(deduped_items, on_progress=_on_progress)

        # ── Persist results ──
        if is_cancel_requested(task_id):
            logger.info("[%s] Task %d cancelled", source.name, task_id)
            return

        async with async_session() as session:
            for item in deduped_items:
                pub_date = None
                if item.get("published_date"):
                    try:
                        pub_date = date.fromisoformat(item["published_date"])
                    except ValueError:
                        pass

                cr = CrawlResult(
                    task_id=task_id,
                    source_id=source.id,
                    title=_clean_title(item["title"]),
                    url=item["url"],
                    content_type=item.get("content_type", "news"),
                    summary=item.get("summary", ""),
                    tags=item.get("tags", ""),
                    has_attachment=item.get("has_attachment", False),
                    attachment_name=item.get("attachment_name", ""),
                    attachment_type=item.get("attachment_type", ""),
                    attachment_path=item.get("attachment_path", ""),
                    attachment_summary=item.get("attachment_summary", ""),
                    published_date=pub_date,
                    user_id=user_id,
                )
                session.add(cr)
            await session.commit()

        # Mark task as completed
        async with async_session() as session:
            task = await session.get(CrawlTask, task_id)
            task.status = TaskStatus.completed.value
            task.completed_at = datetime.utcnow()
            task.items_found = len(deduped_items)
            await session.commit()

        logger.info("[%s] Pipeline done: %d items persisted", source.name, len(deduped_items))

    except Exception as e:
        logger.error("[%s] Pipeline crashed: %s", source.name, e)
        async with async_session() as session:
            task = await session.get(CrawlTask, task_id)
            task.status = TaskStatus.failed.value
            task.completed_at = datetime.utcnow()
            task.error_log = str(e)
            await session.commit()
    finally:
        _cancel_flags.pop(task_id, None)


async def _generate_overview(by_source: dict[str, list[CrawlResult]]) -> str:
    """Use LLM to generate a structured overview of all results."""
    # Dynamic per-source limit to keep total items manageable
    total_items = sum(len(items) for items in by_source.values())
    per_source_limit = 60 // max(len(by_source), 1) if total_items > 60 else 999

    summary_parts = []
    for src_name, items in by_source.items():
        summary_parts.append(f"【{src_name}】共{len(items)}条:")
        for item in items[:per_source_limit]:
            line = f"- [{item.content_type}] {item.title}"
            if item.published_date:
                line += f" ({item.published_date})"
            if item.summary:
                line += f": {item.summary[:200]}"
            summary_parts.append(line)

    all_summaries = "\n".join(summary_parts)

    today = datetime.now().strftime('%Y年%m月%d日')

    system = (
        "你是咨询公司高级行业顾问，擅长撰写结构清晰、重点突出的政策情报简报。"
        "你的读者是企业高管和行业分析师，他们需要快速把握政策风向和行业动态。"
        "你善于从多条信息中归纳趋势，而非简单罗列事实。"
    )

    prompt = f"""今天是{today}。请根据以下采集条目，撰写一份结构化的政策情报概述（300-600字）。

请按以下步骤思考（不要输出思考过程，只输出最终概述）：
1. 通读所有条目，识别高频关键词和重复出现的主题
2. 归纳出2-5个核心主题，多个条目指向同一趋势时合并论述
3. 为每个主题自由拟定一个精练的section标题
4. 撰写概述

结构要求：
- 第一个section固定为"## 核心要点"，用1-2句话点明本期最重要的趋势信号和方向判断
- 后续2-4个section由你根据内容自由拟定标题（不要硬套模板）
- 如果内容集中在一两个主题，不要硬造section，宁少勿滥

可参考的主题方向（仅供启发，不必照搬）：重大政策信号/行业数据与趋势/监管执行动态/国际合作/人事变动/科技创新/能源安全/市场改革

格式要求：
- 每个部分以 ## 标题开头，标题独占一行，标题后空一行再写正文
- 正文中用 **粗体** 强调关键信息（如政策名称、数据）
- 不要使用编号列表（1. 2. 3.），用自然段落叙述
- 直接输出，不加"概述""以下是"等前缀

质量要求：
- 核心要点必须体现"信号价值"——点明趋势或方向，而非复述标题
- 每个section要有因果分析或影响判断，不要只描述"发生了什么"
- 用具体数据和事实说话，避免"值得关注""需要注意"等空话

采集条目：
{all_summaries}"""

    try:
        overview = await simple_completion(prompt, system=system, temperature=0.3, max_tokens=1500)
        return overview.strip()
    except Exception as e:
        logger.error("Failed to generate overview: %s", e)
        return ""


def _overview_to_html(text: str) -> str:
    """Convert markdown-style overview text to clean, naturally readable HTML.

    Handles:
    - ## headings → styled <h3>
    - **bold** → <strong>
    - Numbered sections (1. **title** ...) → heading + paragraph
    - Bullet lists (- item / * item) → <ul><li>
    - Paragraphs separated by blank lines
    - Strips all remaining markdown artifacts
    """
    if not text:
        return ""

    # Normalize line endings
    text = text.replace("\r\n", "\n").strip()

    # Convert **bold** to <strong>
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)

    lines = text.split("\n")
    html_parts: list[str] = []
    current_body: list[str] = []
    current_list: list[str] = []

    p_style = "margin:6px 0 14px 0;line-height:1.8;color:#374151;"
    heading_style = (
        "margin:16px 0 4px 0;font-size:15px;font-weight:600;"
        "color:#1e40af;border-bottom:1px solid #e5e7eb;padding-bottom:4px;"
    )
    li_style = "margin:2px 0;line-height:1.7;color:#374151;"
    ul_style = "margin:6px 0 14px 0;padding-left:20px;color:#374151;"

    def _flush_body():
        if current_body:
            body = " ".join(current_body).strip()
            if body:
                html_parts.append(f'<p style="{p_style}">{body}</p>')
            current_body.clear()

    def _flush_list():
        if current_list:
            items_html = "".join(
                f'<li style="{li_style}">{item}</li>' for item in current_list
            )
            html_parts.append(f'<ul style="{ul_style}">{items_html}</ul>')
            current_list.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            _flush_body()
            _flush_list()
            continue

        # Match ## heading
        m_hash = re.match(r"^#{1,3}\s+(.+)$", stripped)
        if m_hash:
            _flush_body()
            _flush_list()
            html_parts.append(f'<h3 style="{heading_style}">{m_hash.group(1)}</h3>')
            continue

        # Match bullet list: - text or * text
        m_bullet = re.match(r"^[-*]\s+(.+)$", stripped)
        if m_bullet:
            _flush_body()
            current_list.append(m_bullet.group(1))
            continue

        # If we were building a list and hit non-list content, flush it
        _flush_list()

        # Match inline numbered heading + body: "1. <strong>核心要点</strong> some content"
        m_inline = re.match(
            r"^(\d+)\.\s*<strong>(.+?)</strong>\s*(.+)$", stripped
        )
        if m_inline:
            _flush_body()
            html_parts.append(f'<h3 style="{heading_style}">{m_inline.group(2)}</h3>')
            current_body.append(m_inline.group(3))
            continue

        # Match short numbered heading: "1. 核心要点" or "1. <strong>核心要点</strong>"
        m_num = re.match(
            r"^(\d+)\.\s*(?:<strong>)?(.+?)(?:</strong>)?\s*$", stripped
        )
        if m_num and len(stripped) < 40:
            _flush_body()
            html_parts.append(f'<h3 style="{heading_style}">{m_num.group(2)}</h3>')
            continue

        # Regular body text
        current_body.append(stripped)

    _flush_body()
    _flush_list()

    if not html_parts:
        # Fallback: wrap as paragraph
        return f'<p style="{p_style}">{text}</p>'

    return "\n".join(html_parts)


async def _generate_report(batch_id: str, user_id: int = 1):
    """Generate a report from the batch results and dispatch notifications."""
    async with async_session() as session:
        # Fetch all results for this batch
        tasks_q = await session.execute(
            select(CrawlTask).where(CrawlTask.batch_id == batch_id)
        )
        tasks = list(tasks_q.scalars().all())

        results_q = await session.execute(
            select(CrawlResult).where(
                CrawlResult.task_id.in_([t.id for t in tasks])
            ).order_by(CrawlResult.source_id, CrawlResult.published_date.desc())
        )
        results = list(results_q.scalars().all())

    if not results:
        logger.info("Batch %s: no results to report", batch_id)
        return

    # Group by source
    by_source: dict[str, list[CrawlResult]] = defaultdict(list)
    for r in results:
        # Find source name from tasks
        src_name = next((t.source_name for t in tasks if t.source_id == r.source_id), f"源{r.source_id}")
        by_source[src_name].append(r)

    # Generate aggregated overview via LLM
    overview = await _generate_overview(by_source)

    # Build title: {源名称}更新汇总报告YYYY-MM-DD
    now = datetime.now()
    source_names = "、".join(by_source.keys())
    title = f"{source_names}更新汇总报告{now.strftime('%Y-%m-%d')}"

    # Build HTML
    html_parts = [f"<h1>{title}</h1>"]

    # Overview section
    if overview:
        overview_html = _overview_to_html(overview)
        html_parts.append('<div style="margin:20px 0;padding:20px;background:#f0f7ff;border-radius:8px;border-left:4px solid #1a56db;">')
        html_parts.append('<h2 style="margin:0 0 12px 0;color:#1a56db;font-size:18px;">整体概述</h2>')
        html_parts.append(overview_html)
        html_parts.append('</div>')
        html_parts.append('<hr style="margin:24px 0;border-color:#e5e7eb;">')

    # Build plain text
    text_parts = [title, "=" * 40]

    if overview:
        text_parts.append("\n【整体概述】")
        text_parts.append(overview)
        text_parts.append("\n" + "-" * 40)

    # Per-source sections
    for src_name, items in by_source.items():
        html_parts.append(f'<h2 style="border-left:4px solid #1a56db;padding-left:12px;">{src_name} · {len(items)} 条更新</h2>')
        text_parts.append(f"\n== {src_name} ({len(items)}条更新) ==\n")

        for i, item in enumerate(items, 1):
            # HTML
            type_label = {"news": "新闻", "policy": "政策", "notice": "通知", "file": "文件"}.get(item.content_type, "内容")
            html_parts.append(f'<div style="margin:16px 0;padding:12px;border:1px solid #e5e7eb;border-radius:8px;">')
            html_parts.append(f'<p style="margin:0;"><strong>[{type_label}] {item.title}</strong></p>')
            if item.published_date:
                html_parts.append(f'<p style="color:#6b7280;font-size:14px;">发布日期：{item.published_date}</p>')
            # Only show summary if it's meaningful (not empty, not same as title)
            has_real_summary = item.summary and item.summary.strip() != item.title.strip()
            if has_real_summary:
                html_parts.append(f'<p style="margin:8px 0;">{item.summary}</p>')
            if item.has_attachment and item.attachment_name:
                html_parts.append(f'<p>📎 附件: {item.attachment_name}</p>')
                if item.attachment_summary:
                    html_parts.append(f'<p style="color:#4b5563;font-size:14px;">附件摘要: {item.attachment_summary}</p>')
            html_parts.append(f'<p><a href="{item.url}" style="color:#1a56db;">📖 查看原文</a></p>')
            html_parts.append('</div>')

            # Plain text
            text_parts.append(f"{i}. [{type_label}] {item.title}")
            if item.published_date:
                text_parts.append(f"   日期: {item.published_date}")
            if has_real_summary:
                text_parts.append(f"   > {item.summary[:200]}")
            if item.has_attachment:
                text_parts.append(f"   📎 附件: {item.attachment_name}")
            text_parts.append(f"   链接: {item.url}")
            text_parts.append("")

    html_parts.append('<hr style="margin:24px 0;">')
    html_parts.append('<p style="color:#9ca3af;font-size:12px;">此邮件由政策情报助手自动生成（AI摘要仅供参考）</p>')

    content_html = "\n".join(html_parts)
    content_text = "\n".join(text_parts)

    # Save report
    async with async_session() as session:
        report = Report(
            batch_id=batch_id,
            title=title,
            content_html=content_html,
            content_text=content_text,
            overview=overview,
            user_id=user_id,
        )
        session.add(report)
        await session.commit()
        report_id = report.id

    logger.info("Report generated: %s (id=%d)", title, report_id)

    # Dispatch notifications
    await dispatch_report(batch_id, title, content_html, content_text, results)

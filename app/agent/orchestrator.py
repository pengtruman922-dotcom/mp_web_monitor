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
) -> str:
    """Run a crawl batch for the given sources (or all active sources).

    Returns the batch_id.
    """
    batch_id = uuid.uuid4().hex[:12]
    logger.info("Starting batch %s (triggered_by=%s)", batch_id, triggered_by)

    runnable: list[MonitorSource] = []

    try:
        # Fetch sources
        async with async_session() as session:
            query = select(MonitorSource).where(MonitorSource.is_active == True)
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
                    await _run_single_source(src, tid, bid)
                finally:
                    _running_sources.discard(src.id)

        agent_tasks = [_limited_run(src, tasks_map[src.id], batch_id) for src in runnable]
        await asyncio.gather(*agent_tasks, return_exceptions=True)

        # Generate and dispatch report
        await _generate_report(batch_id)

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

    return filtered


async def _filter_homepage_items(
    items: list[dict],
    crawl_rules: str,
    on_progress=None,
) -> list[dict]:
    """Use LLM to filter homepage items by crawl_rules, removing low-value content.

    Returns a subset of items that pass the quality filter.
    Falls back to returning all items on failure.
    """
    if len(items) <= 3:
        return items

    lines = []
    for i, item in enumerate(items):
        title = item.get("title", "")
        url = item.get("url", "")[:80]
        date = item.get("published_date", "")
        lines.append(f"[{i}] {date} | {title} | {url}")

    items_text = "\n".join(lines)

    system = "你是政策信息筛选专家，服务于咨询公司行业顾问。请严格按照规则筛选高价值条目。"
    user = (
        f"请根据以下采集规则，从首页提取的 {len(items)} 条条目中筛选出值得保留的高价值内容。\n\n"
        f"## 采集规则\n{crawl_rules}\n\n"
        f"## 条目列表\n{items_text}\n\n"
        f"筛选要求：\n"
        f"- 排除地方监管局/监管办的日常工作动态\n"
        f"- 保留国家层面政策、高层领导活动、全国性新闻数据\n"
        f"- 不确定的条目应保留\n"
        f"- 返回保留的编号JSON数组，如 [0, 3, 5]\n"
        f"- 直接输出JSON，不加其他内容"
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
                    if on_progress:
                        await on_progress(
                            f"Phase 1a: 质量筛选 {len(items)} → {len(valid)} 条"
                        )
                    logger.info("Homepage filter: %d -> %d items", len(items), len(valid))
                    return [items[i] for i in valid]
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Homepage item filtering failed, keeping all: %s", e)

    return items


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
        f"要求：\n"
        f"- 返回JSON数组：[{{\"name\": \"栏目名\", \"url\": \"列表页完整URL\"}}]\n"
        f"- 只返回能进入文章列表的栏目页链接（如 /zcfg/、/tzgg/、/gzdt/ 等栏目入口），不要具体文章详情链接\n"
        f"- 栏目入口URL通常较短、不含日期，文章URL通常较长、含日期路径\n"
        f"- 如果找到多个匹配栏目，都列出来\n"
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
                if on_progress:
                    await on_progress(f"Phase 1a: 发现 {len(valid)} 个栏目")
                logger.info("[%s] Homepage navigation found %d sections", source.name, len(valid))
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
                max_turns=15,
                enable_pruning=True,
            )

            # Collect items and update URL set for next section
            for item in agent_result.items:
                url = item.get("url", "")
                if url and url not in collected_urls:
                    collected_urls.add(url)
                    all_items.append(item)

            logger.info("[%s] Section '%s': %d items", source.name, section_name, len(agent_result.items))

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
        "请根据提供的文章正文撰写一段简明摘要，帮助顾问快速了解文章核心内容。"
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
                    f"请为以下文章撰写摘要。\n\n"
                    f"要求：\n"
                    f"- 2-3句话，100-200字\n"
                    f"- 提炼核心政策要点、关键数据或主要措施\n"
                    f"- 不要重复标题内容\n"
                    f"- 直接输出摘要，不加前缀\n\n"
                    f"标题：{title}\n\n"
                    f"正文：\n{page_text[:6000]}"
                )

                summary = await simple_completion(
                    user_prompt, system=summary_system, temperature=0.2, max_tokens=512
                )
                summary = summary.strip()

                # Validate
                if not summary or summary == title.strip() or len(summary) < 20:
                    # Retry once
                    summary = await simple_completion(
                        user_prompt, system=summary_system, temperature=0.3, max_tokens=512
                    )
                    summary = summary.strip()

                if summary and summary != title.strip() and len(summary) >= 20:
                    item["summary"] = summary
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

async def _run_single_source(source: MonitorSource, task_id: int, batch_id: str):
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
        homepage_items = _extract_homepage_items(homepage_text, date_start, date_end) if homepage_text else []

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
            sections_to_crawl = sections[:3]  # At most 3 supplementary sections
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
        deduped_items = []
        for item in all_items:
            url = item.get("url", "")
            norm_url = url.replace("http://", "https://")
            if norm_url in existing_url_set or norm_url in seen_urls:
                continue
            seen_urls.add(norm_url)
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
                    title=item["title"],
                    url=item["url"],
                    content_type=item.get("content_type", "news"),
                    summary=item.get("summary", ""),
                    has_attachment=item.get("has_attachment", False),
                    attachment_name=item.get("attachment_name", ""),
                    attachment_type=item.get("attachment_type", ""),
                    attachment_path=item.get("attachment_path", ""),
                    attachment_summary=item.get("attachment_summary", ""),
                    published_date=pub_date,
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
    # Build a condensed summary of all items for the LLM
    summary_parts = []
    for src_name, items in by_source.items():
        summary_parts.append(f"【{src_name}】共{len(items)}条:")
        for item in items[:20]:  # Limit to avoid token overflow
            line = f"- [{item.content_type}] {item.title}"
            if item.summary:
                line += f": {item.summary[:150]}"
            summary_parts.append(line)

    all_summaries = "\n".join(summary_parts)

    system = (
        "你是咨询公司高级行业顾问，擅长撰写结构清晰、重点突出的政策情报简报。"
        "你的读者是企业高管和行业分析师，他们需要快速把握政策风向和行业动态。"
    )

    prompt = f"""请根据以下采集条目，撰写一份结构化的政策情报概述（300-600字）。

按以下模板输出（## 标题独占一行，正文另起一行，段落之间空一行）：

## 核心要点

1-2句话点明本期最重要的政策信号或行业变化。

## 重大政策动向

如有国家级政策、法规、规划，阐述其要点和影响（2-3句话）。

## 行业数据与趋势

如有统计数据发布、行业里程碑，提炼关键数字（2-3句话）。

## 监管与执行动态

如有监管行动、地方执行、标准发布，简要归纳（2-3句话）。

严格格式要求：
- 每个部分以 ## 标题开头，标题独占一行，标题后空一行再写正文
- 正文中用 **粗体** 强调关键信息（如政策名称、数据）
- 如果某个部分没有对应内容，直接省略该部分
- 不要使用编号列表（1. 2. 3.），用自然段落叙述
- 用具体数据和事实说话，避免空泛评价
- 直接输出，不加"概述""以下是"等前缀

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


async def _generate_report(batch_id: str):
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
        )
        session.add(report)
        await session.commit()
        report_id = report.id

    logger.info("Report generated: %s (id=%d)", title, report_id)

    # Dispatch notifications
    await dispatch_report(batch_id, title, content_html, content_text, results)

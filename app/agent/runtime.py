"""Lightweight Agent runtime: LLM call -> parse tool_call -> execute tool -> loop."""
import asyncio
import json
import logging
import re
from datetime import datetime, date
from typing import Callable, Awaitable

from app.llm.client import chat_completion
from app.llm.schemas import ALL_TOOLS
from app.agent.prompts import build_system_prompt
from app.agent.tools.browser import browse_page, close_browser
from app.agent.tools.downloader import download_file
from app.agent.tools.document import read_document
from app.models.source import MonitorSource
from app.config import AGENT_MAX_TURNS

logger = logging.getLogger(__name__)

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


# Type alias for progress callback
ProgressCallback = Callable[[str], Awaitable[None]]


class AgentResult:
    """Stores the results collected by a single agent run."""

    def __init__(self, source_id: int, source_name: str, source_url: str = ""):
        self.source_id = source_id
        self.source_name = source_name
        self.source_url = source_url
        self.items: list[dict] = []
        self.finish_summary: str = ""
        self.error: str = ""
        self.turns_used: int = 0


async def run_agent(
    source: MonitorSource,
    existing_urls: list[str] | None = None,
    on_progress: ProgressCallback | None = None,
    cancel_event: asyncio.Event | None = None,
    # ---- New parameters for multi-agent architecture ----
    system_prompt: str | None = None,
    user_message: str | None = None,
    tools: list[dict] | None = None,
    max_turns: int | None = None,
    enable_pruning: bool = False,
    crawl_rules: str = "",
) -> AgentResult:
    """Run a crawl agent for a single monitor source.

    The agent loops: send messages to LLM -> parse tool calls -> execute -> repeat.
    on_progress: optional async callback called with status messages during execution.

    When system_prompt/user_message/tools are provided externally (multi-agent mode),
    the internal prompt building is skipped.
    """
    # Disable domain filter if crawl_rules explicitly allow cross-domain content
    effective_source_url = "" if "允许跨域" in crawl_rules else source.url
    result = AgentResult(source.id, source.name, source_url=effective_source_url)

    effective_max_turns = max_turns or AGENT_MAX_TURNS
    effective_tools = tools or ALL_TOOLS

    if system_prompt and user_message:
        # External prompt mode (used by multi-agent orchestrator)
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
    else:
        # Legacy mode: build prompt internally
        time_range_days = source.time_range_days or 7
        max_items = source.max_items or 30

        built_prompt = build_system_prompt(
            source_name=source.name,
            source_url=source.url,
            focus_areas=source.focus_areas or [],
            max_depth=source.max_depth or 3,
            time_range_days=time_range_days,
            max_items=max_items,
            existing_urls=existing_urls,
        )

        messages = [
            {"role": "system", "content": built_prompt},
            {"role": "user", "content": f"请开始采集 {source.name}（{source.url}）最近{time_range_days}天的新增内容。"},
        ]

    async def _progress(msg: str):
        if on_progress:
            try:
                await on_progress(msg)
            except Exception:
                pass

    await _progress("开始采集")

    # Early termination tracking: count consecutive browse turns with no new items
    _consecutive_empty_browses = 0
    _EARLY_TERM_THRESHOLD = 2  # hint after 2 consecutive empty browse turns

    for turn in range(effective_max_turns):
        if cancel_event and cancel_event.is_set():
            result.error = "任务被用户中止"
            await _progress("任务被用户中止")
            break

        result.turns_used = turn + 1
        logger.info("[%s] Turn %d/%d", source.name, turn + 1, effective_max_turns)
        await _progress(f"轮次 {turn + 1}/{effective_max_turns}")

        try:
            response = await chat_completion(messages, tools=effective_tools)
        except Exception as e:
            result.error = f"LLM调用失败: {e}"
            logger.error("[%s] LLM call failed: %s", source.name, e)
            await _progress(f"LLM调用失败: {e}")
            break

        messages.append(response)

        tool_calls = response.get("tool_calls")
        if not tool_calls:
            logger.info("[%s] Agent returned text (no tool calls), ending.", source.name)
            result.finish_summary = response.get("content", "")
            break

        # Track whether this turn had a browse_page and whether items were saved
        items_before_turn = len(result.items)
        turn_had_browse = False

        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            try:
                fn_args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                fn_args = {}

            logger.info("[%s] Tool call: %s(%s)", source.name, fn_name, list(fn_args.keys()))

            if fn_name == "browse_page":
                turn_had_browse = True
                await _progress(f"正在浏览: {fn_args.get('url', '')[:80]}")
            elif fn_name == "download_file":
                await _progress(f"正在下载: {fn_args.get('filename', '')}")
            elif fn_name == "save_result":
                await _progress(f"保存内容: {fn_args.get('title', '')[:50]}（已发现 {len(result.items) + 1} 条）")
            elif fn_name == "save_results_batch":
                batch_count = len(fn_args.get("items", []))
                if not batch_count:
                    raw = fn_args.get("items_json", "")
                    if isinstance(raw, str) and raw.strip():
                        try:
                            batch_count = len(json.loads(raw))
                        except (json.JSONDecodeError, TypeError):
                            batch_count = 0
                await _progress(f"批量保存 {batch_count} 条（当前共 {len(result.items) + batch_count} 条）")

            tool_result = await _execute_tool(fn_name, fn_args, result)

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": tool_result,
            })

            # Context pruning: after successful save_results_batch, replace the
            # most recent large browse_page result with a compact summary
            if enable_pruning and fn_name == "save_results_batch" and "批量保存成功" in tool_result:
                for j in range(len(messages) - 1, -1, -1):
                    msg = messages[j]
                    if msg.get("role") == "tool" and len(msg.get("content", "")) > 2000:
                        messages[j] = {**msg, "content": f"[已处理该页面，提取了{len(result.items)}条结果，原始内容已省略]"}
                        break

            if fn_name == "finish":
                result.finish_summary = fn_args.get("summary", "")
                logger.info("[%s] Agent finished: %s", source.name, result.finish_summary)
                await _progress(f"采集完成，共 {len(result.items)} 条")
                return result

        # Early termination: check if browse happened but no new items were saved
        items_after_turn = len(result.items)
        if turn_had_browse:
            if items_after_turn == items_before_turn:
                _consecutive_empty_browses += 1
                logger.info("[%s] Empty browse turn (%d consecutive)", source.name, _consecutive_empty_browses)
            else:
                _consecutive_empty_browses = 0

            if _consecutive_empty_browses >= _EARLY_TERM_THRESHOLD:
                logger.info("[%s] Early termination hint: %d consecutive empty browses", source.name, _consecutive_empty_browses)
                await _progress(f"连续{_consecutive_empty_browses}页无新数据，提示结束")
                messages.append({
                    "role": "user",
                    "content": f"已连续浏览{_consecutive_empty_browses}个页面未发现新的符合日期范围的条目，建议调用 finish 结束当前栏目采集。",
                })

    if not result.finish_summary and not result.error:
        result.finish_summary = f"达到最大轮次({effective_max_turns})，自动结束"

    await _progress(f"采集结束，共 {len(result.items)} 条")
    return result


async def _execute_tool(name: str, args: dict, result: AgentResult) -> str:
    """Execute a single tool and return the string result."""
    try:
        if name == "browse_page":
            return await browse_page(args["url"])

        elif name == "download_file":
            return await download_file(args["url"], args["filename"])

        elif name == "read_document":
            return await read_document(args["file_path"])

        elif name == "save_result":
            title = _clean_title(args.get("title", ""))
            summary = args.get("summary", "")
            if summary.strip() == title.strip():
                summary = ""
            item = {
                "title": title,
                "url": args.get("url", ""),
                "content_type": args.get("content_type", "news"),
                "summary": summary,
                "published_date": args.get("published_date", ""),
                "has_attachment": args.get("has_attachment", False),
                "attachment_name": args.get("attachment_name", ""),
                "attachment_type": args.get("attachment_type", ""),
                "attachment_path": args.get("attachment_path", ""),
                "attachment_summary": args.get("attachment_summary", ""),
            }
            # Domain check: skip cross-domain items
            if result.source_url and item["url"]:
                from app.agent.domain_filter import is_same_domain
                if not is_same_domain(item["url"], result.source_url):
                    return f"已跳过（跨域内容）: {item['title']}"
            result.items.append(item)
            return f"已保存: {item['title']}（共{len(result.items)}条）"

        elif name == "save_results_batch":
            # Accept both formats:
            # 1. New: items_json (string) — simpler schema for Qwen
            # 2. Old: items (array) — kept for backward compat
            items_data = args.get("items", [])
            if not items_data:
                raw = args.get("items_json", "[]")
                if isinstance(raw, str):
                    try:
                        items_data = json.loads(raw)
                    except json.JSONDecodeError:
                        return f"items_json 格式错误，请提供有效的JSON数组"
                elif isinstance(raw, list):
                    items_data = raw
            saved_count = 0
            skipped_count = 0
            for item in items_data:
                if not isinstance(item, dict):
                    continue
                item_url = item.get("url", "")
                # Domain check: skip cross-domain items
                if result.source_url and item_url:
                    from app.agent.domain_filter import is_same_domain
                    if not is_same_domain(item_url, result.source_url):
                        skipped_count += 1
                        continue
                title = _clean_title(item.get("title", ""))
                summary = item.get("summary", "")
                # Discard summary if it's just a copy of the title
                if summary.strip() == title.strip():
                    summary = ""
                result.items.append({
                    "title": title,
                    "url": item_url,
                    "content_type": item.get("content_type", "news"),
                    "summary": summary,
                    "published_date": item.get("published_date", ""),
                    "has_attachment": False,
                    "attachment_name": "",
                    "attachment_type": "",
                    "attachment_path": "",
                    "attachment_summary": "",
                })
                saved_count += 1
            msg = f"批量保存成功: {saved_count} 条（共{len(result.items)}条）"
            if skipped_count:
                msg += f"，跳过 {skipped_count} 条跨域内容"
            return msg

        elif name == "finish":
            return "采集完成"

        else:
            return f"未知工具: {name}"

    except Exception as e:
        logger.error("Tool %s failed: %s", name, e)
        return f"工具执行失败: {e}"

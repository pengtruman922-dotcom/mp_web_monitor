"""Playwright browser tool for the Agent."""
import asyncio
import logging
import re

from playwright.async_api import async_playwright, Browser, BrowserContext

from app.config import AGENT_PAGE_DELAY

logger = logging.getLogger(__name__)

_browser: Browser | None = None
_context: BrowserContext | None = None


async def _ensure_browser() -> BrowserContext:
    """Launch or reuse a shared browser instance."""
    global _browser, _context
    if _browser is None or not _browser.is_connected():
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        _context = await _browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )
    return _context


async def close_browser():
    """Close the shared browser instance."""
    global _browser, _context
    if _context:
        await _context.close()
        _context = None
    if _browser:
        await _browser.close()
        _browser = None


def _clean_text(text: str) -> str:
    """Remove excessive whitespace from extracted text."""
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


async def browse_page(url: str) -> str:
    """Load a URL and return page text + structured link list.

    Key features:
    - Uses textContent (via cloned DOM) to capture hidden tab/pane content
    - Extracts dates associated with each link from parent elements
    - Includes a polite delay between requests
    """
    context = await _ensure_browser()
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Wait a short while for dynamic content
        await page.wait_for_timeout(2000)

        # Extract text using textContent on a cleaned clone of <body>.
        # Unlike innerText, textContent captures content inside hidden
        # elements (e.g. inactive tab panes), which is critical for sites
        # that use tabbed layouts on their homepages.
        text = await page.evaluate("""
            () => {
                const clone = document.body.cloneNode(true);
                clone.querySelectorAll('script, style, noscript, svg, iframe').forEach(el => el.remove());
                return clone.textContent;
            }
        """)
        text = _clean_text(text)

        # Truncate to avoid overwhelming LLM context
        if len(text) > 15000:
            text = text[:15000] + "\n...[内容截断]"

        # Extract links WITH date context from parent elements.
        # Government sites typically use <li><a>title</a><span>(date)</span></li>.
        # We grab the date from the closest <li> or parent element so the
        # agent can immediately see which items fall in the target date range.
        links = await page.eval_on_selector_all(
            "a[href]",
            r"""els => els.map(el => {
                const inner = el.innerText.trim();
                const attrTitle = (el.getAttribute('title') || '').trim();
                const text = (attrTitle.length > inner.length ? attrTitle : inner).substring(0, 150);
                const href = el.href;
                let date = '';
                const li = el.closest('li') || el.parentElement;
                if (li) {
                    const liText = li.textContent;
                    // Pattern 1: YYYY-MM-DD, YYYY年M月D日, YYYY.M.D, YYYY/M/D
                    const m = liText.match(/(\d{4}[-年.\/]\d{1,2}[-月.\/]\d{1,2})日?/);
                    if (m) {
                        date = m[1].replace(/[年月日]/g, '-').replace(/\//g, '-').replace(/-$/, '');
                        const parts = date.split('-');
                        if (parts.length === 3) {
                            date = parts[0] + '-' + parts[1].padStart(2, '0') + '-' + parts[2].padStart(2, '0');
                        }
                    }
                    // Pattern 2: standalone 8-digit date like 20260130
                    if (!date) {
                        const m2 = liText.match(/(?:^|[^\d])(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?:[^\d]|$)/);
                        if (m2) {
                            date = m2[1] + '-' + m2[2] + '-' + m2[3];
                        }
                    }
                }
                if (!date && href) {
                    // URL pattern 1: /YYYYMMDD/ (with trailing slash)
                    let um = href.match(/\/(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\//);
                    // URL pattern 2: /tYYYYMMDD_ (common on gov.cn)
                    if (!um) um = href.match(/\/t(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])_/);
                    // URL pattern 3: /WYYYYMMDD (used by some ministries)
                    if (!um) um = href.match(/\/W(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])/);
                    // URL pattern 4: /art/YYYY/M/D/ or /art/YYYY/MM/DD/
                    if (!um) {
                        const am = href.match(/\/art\/(20\d{2})\/(\d{1,2})\/(\d{1,2})\//);
                        if (am) um = am;
                    }
                    // URL pattern 5: /YYYY-MM/DD/ or /YYYY-MM/t...
                    if (!um) {
                        const dm = href.match(/\/(20\d{2})[-\/](0[1-9]|1[0-2])\/(?:t?)(\d{2})/);
                        if (dm) um = dm;
                    }
                    if (um) {
                        date = um[1] + '-' + um[2].padStart(2, '0') + '-' + um[3].padStart(2, '0');
                    }
                }
                return { text, href, date };
            }).filter(l => l.text && l.href && !l.href.startsWith('javascript'))"""
        )

        # Format top 200 links, showing dates where available
        if links:
            links = links[:200]
            link_text = "\n\n--- 页面链接列表 ---\n"
            for link in links:
                date_tag = f" ({link['date']})" if link.get('date') else ""
                link_text += f"- [{link['text']}]({link['href']}){date_tag}\n"
            text += link_text

        # Build a structured "extractable items" section from links that
        # have dates — these are content list items the agent can batch-save.
        dated_items = [
            lnk for lnk in (links or [])
            if lnk.get('date')
            and len(lnk.get('text', '')) >= 8
            and lnk['href'].startswith('http')
        ]
        if dated_items:
            import json as _json
            items_for_save = []
            for lnk in dated_items:
                items_for_save.append({
                    "title": lnk['text'],
                    "url": lnk['href'],
                    "published_date": lnk['date'],
                })
            text += f"\n\n--- 可直接采集的条目（共{len(items_for_save)}条，含标题+链接+日期）---\n"
            text += "你可以用 save_results_batch 工具一次性保存以下条目（筛选日期范围内的）：\n"
            text += _json.dumps(items_for_save, ensure_ascii=False, indent=None)
            text += "\n"
        elif links and len(links) > 3:
            # Page has links but none with extractable dates — signal to agent
            text += "\n\n注意：此页面未检测到带日期的条目。如果连续多页无日期条目，建议调用 finish 结束当前栏目。\n"

        return text
    except Exception as e:
        logger.error("Failed to browse %s: %s", url, e)
        return f"页面加载失败: {e}"
    finally:
        await page.close()
        # Polite delay
        await asyncio.sleep(AGENT_PAGE_DELAY)

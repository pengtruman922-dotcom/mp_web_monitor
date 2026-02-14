import asyncio
from playwright.async_api import async_playwright

async def test():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-extensions",
            "--no-first-run",
        ],
    )
    # Block images/fonts/css to speed up loading
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
        locale="zh-CN",
    )
    page = await context.new_page()
    await page.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,css}", lambda route: route.abort())

    try:
        print("Loading page...")
        await page.goto("https://www.nea.gov.cn/", wait_until="domcontentloaded", timeout=60000)
        print("Page loaded, waiting for content...")
        await page.wait_for_timeout(3000)
        title = await page.title()
        text = await page.evaluate("() => document.body.innerText.substring(0, 500)")
        links = await page.evaluate('() => document.querySelectorAll("a[href]").length')
        print("Title:", title)
        print("Links:", links)
        print("Text:", text[:300])
    except Exception as e:
        print("Error:", e)
    await browser.close()
    await pw.stop()

asyncio.run(test())

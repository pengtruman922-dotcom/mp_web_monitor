import asyncio
from playwright.async_api import async_playwright

async def test():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = await browser.new_page()
    try:
        await page.goto("https://www.nea.gov.cn/", wait_until="domcontentloaded", timeout=30000)
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

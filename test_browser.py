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
        ],
    )
    page = await browser.new_page()

    # Test 1: baidu (verify chromium works)
    print("=== Test 1: baidu.com ===")
    try:
        await page.goto("https://www.baidu.com/", timeout=15000)
        title = await page.title()
        print("OK - Title:", title)
    except Exception as e:
        print("FAIL:", e)

    # Test 2: nea.gov.cn with commit (earliest event)
    print("=== Test 2: nea.gov.cn (commit) ===")
    try:
        resp = await page.goto("https://www.nea.gov.cn/", wait_until="commit", timeout=30000)
        print("OK - Status:", resp.status if resp else "no response")
        await page.wait_for_timeout(2000)
        title = await page.title()
        print("Title:", title)
    except Exception as e:
        print("FAIL:", e)

    # Test 3: nea.gov.cn http (not https)
    print("=== Test 3: nea.gov.cn HTTP ===")
    try:
        resp = await page.goto("http://www.nea.gov.cn/", wait_until="commit", timeout=30000)
        print("OK - Status:", resp.status if resp else "no response")
        await page.wait_for_timeout(2000)
        title = await page.title()
        print("Title:", title)
    except Exception as e:
        print("FAIL:", e)

    await browser.close()
    await pw.stop()

asyncio.run(test())

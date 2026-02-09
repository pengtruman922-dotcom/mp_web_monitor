"""Quick script to fetch NEA homepage using the project's Playwright browser tool."""
import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent.tools.browser import browse_page, close_browser


async def main():
    print("Fetching https://www.nea.gov.cn/ ...")
    content = await browse_page("https://www.nea.gov.cn/")

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nea_homepage.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"Done. Saved to {out_path}")
    print(f"Content length: {len(content)} chars")

    await close_browser()


if __name__ == "__main__":
    asyncio.run(main())

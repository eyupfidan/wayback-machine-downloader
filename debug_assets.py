"""Test downloading a single asset end to end."""
import asyncio
import sys
sys.path.insert(0, "C:\\Users\\eyup.fidan\\Desktop\\wayback-tool")

from lib.fetcher import WaybackFetcher

async def main():
    async with WaybackFetcher(workers=1) as f:
        for url in [
            "https://famium.co/images/Famium-Header-Logo.png",
            "https://famium.co/_next/static/css/f27ce39e486d7b87.css",
        ]:
            print(f"\n--- Fetching: {url}")
            result = await f.fetch_snapshot(url, allow_fallback=True)
            print(f"  status: {result.status}")
            print(f"  body length: {len(result.body)}")
            print(f"  content_type: {result.content_type}")
            print(f"  error: {result.error}")
            print(f"  timestamp used: {result.timestamp}")

asyncio.run(main())

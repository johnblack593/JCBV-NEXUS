import asyncio, aiohttp, sys, os
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

async def check():
    keys_str = os.getenv("GEMINI_API_KEYS", "")
    key = keys_str.split(",")[0].strip()
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(), ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as s:
        async with s.get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={key}",
        ) as r:
            if r.status == 200:
                data = await r.json()
                models = [m["name"] for m in data.get("models", [])]
                print("Models:", models)
            else:
                body = await r.text()
                print(f"Status {r.status}: {body[:300]}")

asyncio.run(check())

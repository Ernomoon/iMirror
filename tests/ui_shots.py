"""Screenshots of the UI in every state (demo backend), plus a JS error check.
Usage: python tests/ui_shots.py [chrome-path] [out-dir]"""
import asyncio, sys, os
from pathlib import Path
from playwright.async_api import async_playwright
URL = (Path(__file__).resolve().parent.parent / "app/imirror/ui/index.html").as_uri()
CHROME = sys.argv[1] if len(sys.argv) > 1 else "/usr/bin/google-chrome"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "shots"); OUT.mkdir(exist_ok=True)
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        pg = await b.new_page(viewport={"width": 1180, "height": 780})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
        for mode in ["off", "starting", "ready", "problem", "connecting", "live"]:
            await pg.goto(f"{URL}?demo={mode}")
            await pg.wait_for_timeout(2800)
            await pg.screenshot(path=str(OUT / f"state_{mode}.png"))
        for q in ["bt=waiting", "bt=connected", "bt=connected&cap=1", "bt=error"]:
            await pg.goto(f"{URL}?demo=ready&{q}")
            await pg.wait_for_timeout(1500)
            await pg.click("[data-nav=control]")
            await pg.wait_for_timeout(1300)
            await pg.screenshot(path=str(OUT / f"control_{q.replace('&', '_').replace('=', '-')}.png"))
        await pg.goto(f"{URL}?demo=ready")
        await pg.wait_for_timeout(1500)
        await pg.click("[data-nav=settings]")
        await pg.wait_for_timeout(1200)
        await pg.screenshot(path=str(OUT / "settings.png"))
        print("errors:", errors)
        if errors:
            sys.exit(1)
        await b.close()
asyncio.run(main())

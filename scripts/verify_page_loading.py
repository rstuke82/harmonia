"""Verify production read feedback stays visible, accessible and nonblocking."""

import mimetypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from playwright.sync_api import sync_playwright
    from harmonia.browser_security import CSP

    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        subprocess.run(
            ["node", str(ROOT / "scripts/build_page_fixture.cjs"), directory], check=True
        )

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = folder / ("index.html" if self.path == "/" else self.path.lstrip("/"))
                if path.parent != folder or not path.is_file():
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header(
                    "Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream"
                )
                self.send_header("Content-Security-Policy", CSP)
                self.end_headers()
                self.wfile.write(path.read_bytes())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    **(
                        {"channel": os.environ["HARMONIA_TEST_BROWSER"]}
                        if os.environ.get("HARMONIA_TEST_BROWSER")
                        else {}
                    ),
                )
                for width in (390, 1440, 3440):
                    page = browser.new_page(viewport={"width": width, "height": 900})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.add_init_script(
                        "window.cspViolations=[];document.addEventListener('securitypolicyviolation',e=>window.cspViolations.push(e.effectiveDirective))"
                    )
                    page.goto(f"http://127.0.0.1:{server.server_port}/")
                    page.get_by_role("button", name="Start loading", exact=True).click()
                    status = page.get_by_role("status")
                    status.wait_for()
                    assert "Loading Library" in status.inner_text()
                    page.wait_for_function(
                        "() => document.querySelector('[role=status]').textContent.includes('3s elapsed')"
                    )
                    assert status.bounding_box()["width"] <= width
                    for theme in ("dark", "light"):
                        page.evaluate(
                            "(theme)=>document.documentElement.dataset.theme=theme", theme
                        )
                        contrast = status.evaluate("""e=>{
                          const rgb=v=>v.match(/[0-9.]+/g).slice(0,3).map(Number).map(x=>{x/=255;return x<=.04045?x/12.92:((x+.055)/1.055)**2.4});
                          const lum=v=>{const [r,g,b]=rgb(v);return .2126*r+.7152*g+.0722*b};
                          const bg=lum(getComputedStyle(e).backgroundColor);
                          return [...e.querySelectorAll('strong,.muted')].map(node=>{const fg=lum(getComputedStyle(node).color);return (Math.max(bg,fg)+.05)/(Math.min(bg,fg)+.05)})
                        }""")
                        assert min(contrast) >= 4.5, (theme, contrast)
                    page.screenshot(path=f"/tmp/harmonia101-loading-{width}.png", full_page=True)
                    page.get_by_role("button", name="Keep browsing", exact=True).click()
                    assert page.get_by_role("status").count() == 0
                    page.get_by_role("button", name="Return to playlists", exact=True).click()
                    status.wait_for()
                    page.get_by_role("button", name="Finish loading", exact=True).click()
                    assert page.get_by_role("status").count() == 0
                    page.get_by_role("button", name="Start refresh", exact=True).click()
                    status.wait_for()
                    assert page.get_by_role("heading", name="Saved playlist").is_visible()
                    page.wait_for_function(
                        "() => document.querySelector('[role=status]').textContent.includes('Waiting for the active library job')"
                    )
                    page.evaluate("window.mockMode='fail'")
                    alert = page.get_by_role("alert")
                    alert.wait_for()
                    assert "last loaded data" in alert.inner_text()
                    page.evaluate("window.mockMode='fresh'")
                    page.get_by_role("button", name="Retry", exact=True).click()
                    page.get_by_role("heading", name="Fresh playlist").wait_for()
                    assert page.get_by_role("alert").count() == 0
                    assert not errors, errors
                    assert page.evaluate("window.cspViolations") == []
                    page.screenshot(path=f"/tmp/harmonia101-ready-{width}.png", full_page=True)
                    page.close()
                browser.close()
                print(
                    "Loading, elapsed time, route isolation, background job progress, usable saved data and retry passed at 390, 1440 and 3440px under production CSP."
                )
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()

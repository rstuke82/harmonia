"""Exercise real React styling under production CSP in a disposable headless browser."""

import mimetypes
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from playwright.sync_api import sync_playwright
    from harmonia.browser_security import CSP

    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        subprocess.run(["node", str(ROOT / "scripts/build_csp_fixture.cjs"), directory], check=True)
        (folder / "cover.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="blue"/></svg>'
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
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1200, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script(
                    "window.cspViolations=[];document.addEventListener('securitypolicyviolation',e=>window.cspViolations.push({directive:e.effectiveDirective,blocked:e.blockedURI}));"
                )
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                page.get_by_role("button", name="Dark", exact=True).click()
                page.get_by_role("button", name="Use #7255dc accent", exact=True).click()
                assert page.evaluate("document.documentElement.dataset.theme") == "dark"
                assert (
                    page.evaluate(
                        "getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()"
                    )
                    == "#7255dc"
                )
                image = page.locator(".recent-request-art img")
                image.wait_for()
                page.wait_for_function(
                    "() => document.querySelector('.recent-request-art img').naturalWidth > 0"
                )
                assert "cover.svg" in page.locator(".recent-request-backdrop").evaluate(
                    "e=>getComputedStyle(e).backgroundImage"
                )
                page.get_by_role("button", name="Filters", exact=True).click()
                assert (
                    page.locator(".control-portal").evaluate("e=>getComputedStyle(e).position")
                    == "fixed"
                )
                page.get_by_role("button", name="Open modal", exact=True).click()
                assert page.evaluate("document.body.style.overflow") == "hidden"
                page.get_by_role("button", name="Close modal", exact=True).click()
                assert page.evaluate("document.body.style.overflow") != "hidden"
                assert not errors, errors
                assert page.evaluate("window.cspViolations") == [], page.evaluate(
                    "window.cspViolations"
                )
                # Deliberate injection must be blocked even though trusted JS styling works.
                page.evaluate(
                    "const style=document.createElement('style');style.textContent='body{background:rgb(1,2,3)!important}';document.head.append(style);const image=new Image();image.src='http://example.invalid/insecure.png';document.body.append(image);"
                )
                page.wait_for_function(
                    "() => window.cspViolations.some(e=>e.directive==='style-src-elem')&&window.cspViolations.some(e=>e.directive==='img-src')"
                )
                assert (
                    page.evaluate("getComputedStyle(document.body).backgroundColor")
                    != "rgb(1, 2, 3)"
                )
                browser.close()
                print(
                    "Production CSP preserves React themes, artwork, fitted titles, menus and modals; injected inline styles and external HTTP images are blocked."
                )
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()

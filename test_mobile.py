"""Mobile end-to-end browser smoke test with isolated temporary demo workspace."""

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "tests" / "fixtures"
SCREENSHOT_PATH = Path("/tmp/mobile_test_failure.png")

# Ensure localhost traffic does not hit corporate or sandbox proxies
os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"


def wait_for_server(url: str, timeout_sec: int = 15) -> bool:
    start = time.time()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.time() - start < timeout_sec:
        try:
            with opener.open(url, timeout=1) as response:
                if response.status in (200, 404, 503):
                    return True
        except urllib.error.HTTPError as exc:
            if exc.code in (200, 404, 503):
                return True
        except Exception:
            time.sleep(0.5)
    return False


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "ERROR: playwright is not installed. Run `make setup` and `python3 -m playwright install chromium` first.",
            file=sys.stderr,
        )
        return 1

    temp_dir = tempfile.TemporaryDirectory(prefix="stock_screen_mobile_")
    state_dir = Path(temp_dir.name)
    port = 8555

    print(f"Setting up isolated demo workspace in {state_dir}...")
    setup_cmd = [
        sys.executable,
        "manage.py",
        "setup-demo",
        "--state-dir",
        str(state_dir),
        "--journal-mode",
        "DELETE",
    ]
    res = subprocess.run(setup_cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        print(f"Failed to setup demo workspace: {res.stderr}", file=sys.stderr)
        return 1

    env = os.environ.copy()
    env.update(
        {
            "APP_MODE": "demo",
            "STATE_DIR": str(state_dir),
            "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "FLET_WEB_NO_CDN": "true",
            "no_proxy": "127.0.0.1,localhost",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )

    server_proc = subprocess.Popen(
        [sys.executable, "app.py"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        base_url = f"http://127.0.0.1:{port}"
        if not wait_for_server(base_url, timeout_sec=10):
            err_output = ""
            if server_proc.poll() is not None and server_proc.stderr:
                err_output = server_proc.stderr.read().decode("utf-8")
            print(
                f"ERROR: App server failed to start within timeout. {err_output}", file=sys.stderr
            )
            return 1

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as exc:
                print(f"ERROR: Failed to launch Chromium browser: {exc}", file=sys.stderr)
                return 1

            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
            )
            page = context.new_page()

            def enable_accessibility():
                page.wait_for_selector("flt-semantics-placeholder", timeout=15000)
                page.evaluate('document.querySelector("flt-semantics-placeholder")?.click()')
                time.sleep(1)

            def click_semantics_button(name: str) -> bool:
                info = page.evaluate(
                    """(name) => {
                    const btns = Array.from(document.querySelectorAll('flt-semantics[role="button"], flt-semantics[role="tab"]'));
                    let btn = btns.find(n => (n.innerText || "").trim() === name);
                    if (!btn) btn = btns.find(n => (n.getAttribute('aria-label') || "").trim() === name);
                    if (!btn) {
                        const candidates = btns.filter(n => {
                            const txt = (n.innerText || "").trim();
                            const label = (n.getAttribute('aria-label') || "").trim();
                            return txt.includes(name) || label.includes(name);
                        });
                        if (candidates.length > 0) {
                            candidates.sort((a, b) => (a.innerText || "").length - (b.innerText || "").length);
                            btn = candidates[0];
                        }
                    }
                    if (!btn) return null;
                    const r = btn.getBoundingClientRect();
                    return { x: r.left + r.width / 2, y: r.top + r.height / 2, width: r.width, height: r.height, text: btn.innerText };
                }""",
                    name,
                )
                if info and info["width"] > 0 and info["height"] > 0:
                    page.mouse.click(info["x"], info["y"])
                    return True
                return False

            try:
                # 1. Open home page
                page.goto(f"{base_url}/", timeout=15000)
                page.wait_for_load_state("domcontentloaded")
                enable_accessibility()

                # Click the top-right Settings icon, then its back arrow (real browser events).
                page.locator("flt-semantics[role='button']").first.click()
                page.wait_for_selector("text=系统设置", timeout=10000)
                page.locator("flt-semantics[role='button']").nth(1).click()
                page.wait_for_selector("text=估值基准日", timeout=10000)

                # 2. Click "同业发现" navigation item
                discover_nav = page.locator("[aria-label='同业发现']").first
                discover_nav.click()
                page.wait_for_selector("text=参照标的", timeout=10000)
                time.sleep(1)

                # 3. Click "查看" for company 600001.SH (index 1 in the list)
                company_btn = page.locator("flt-semantics[role='button']:has-text('查看')").nth(1)
                company_btn.click()
                page.wait_for_selector("text=公司筛选事实", timeout=10000)
                time.sleep(1)

                # 4. Fill in personal research reason
                test_reason = "移动端自动化测试理由"
                reason_input = page.locator("textarea[aria-label*='理由']").first
                reason_input.click()
                reason_input.press_sequentially(test_reason)
                time.sleep(1)

                # 5. Verify Save button accessibility and interactive state via semantic locator
                save_btn = page.locator("flt-semantics[role='button']:has-text('保存判断')").first
                save_btn.scroll_into_view_if_needed()
                assert save_btn.is_visible(), "Save button is not visible in accessibility tree"
                assert save_btn.is_enabled(), "Save button is not enabled"

                clicked = click_semantics_button("保存判断")
                assert clicked, "Failed to click Save button"

                # Await UI feedback or database confirmation
                db_path = state_dir / "workspace.sqlite3"
                saved_confirmed = False
                for _ in range(25):
                    has_ui_text = page.evaluate("""() => {
                        const nodes = Array.from(document.querySelectorAll('flt-semantics, p, span, div'));
                        return nodes.some(n => (n.innerText || n.textContent || n.getAttribute('aria-label') || '').includes('保存成功'));
                    }""")
                    if has_ui_text:
                        saved_confirmed = True
                        break
                    if db_path.exists():
                        conn = sqlite3.connect(db_path)
                        r = conn.execute(
                            "SELECT revision FROM watch_items WHERE code='600001.SH'"
                        ).fetchone()
                        conn.close()
                        if r and r[0] >= 1:
                            saved_confirmed = True
                            break
                    time.sleep(0.4)

                assert saved_confirmed, "Save feedback or SQLite record not detected after save"

                # 6. Verify SQLite persistence directly
                db_path = state_dir / "workspace.sqlite3"
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM watch_items WHERE code='600001.SH'").fetchone()
                conn.close()

                assert row is not None, "Watch item 600001.SH was not saved in SQLite"
                assert row["reason"] == test_reason, (
                    f"Expected reason '{test_reason}', got '{row['reason']}'"
                )
                assert row["revision"] >= 1, "Revision should be >= 1"

                # 7. Navigate back to Home and verify persisted item on home list
                home_nav = page.locator("[aria-label='我的研究']").first
                if home_nav.is_visible():
                    home_nav.click()
                else:
                    assert click_semantics_button("我的研究"), "Could not click 我的研究 tab"
                page.wait_for_function(
                    '() => document.body.innerText.includes("关注清单 (1)")', timeout=10000
                )
                page.wait_for_function(
                    '() => document.body.innerText.includes("查看详情")', timeout=10000
                )

                # 8. Reload page and assert persisted reason survives reload on home page
                page.reload()
                page.wait_for_load_state("domcontentloaded")
                enable_accessibility()
                page.wait_for_function(
                    '() => document.body.innerText.includes("关注清单 (1)")', timeout=10000
                )
                page.wait_for_function(
                    '() => document.body.innerText.includes("查看详情")', timeout=10000
                )
                time.sleep(1)

                # 9. Re-enter detail page to verify persisted reason in the detail view
                detail_btn = page.locator("flt-semantics[role='button']:has-text('查看详情')").first
                if detail_btn.is_visible():
                    detail_btn.click()
                else:
                    assert click_semantics_button("查看详情"), "Could not click 查看详情 button"
                page.wait_for_selector("text=公司筛选事实", timeout=10000)
                time.sleep(1)
                detail_reason = page.locator("textarea[aria-label*='理由']").first
                detail_reason.click()
                time.sleep(0.5)
                val = detail_reason.input_value()
                print(f"Detail reason input_value: '{val}'")
                assert val == test_reason, f"Expected reason '{test_reason}', got '{val}'"

                print(
                    "Mobile browser end-to-end test passed: discover -> view -> edit -> save -> DB check -> reload -> home check confirmed."
                )
                return 0
            except Exception as exc:
                try:
                    page.screenshot(path=str(SCREENSHOT_PATH))
                    print(f"Saved failure screenshot to {SCREENSHOT_PATH}", file=sys.stderr)
                except Exception:
                    pass
                print(f"Mobile browser test assertion failed: {exc}", file=sys.stderr)
                return 1
            finally:
                context.close()
                browser.close()
    finally:
        server_proc.terminate()
        server_proc.wait(timeout=5)
        temp_dir.cleanup()


if __name__ == "__main__":
    sys.exit(main())

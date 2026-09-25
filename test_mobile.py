"""Mobile end-to-end browser smoke test with isolated temporary demo workspace."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from workspace import import_snapshot

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
        from playwright.sync_api import expect, sync_playwright
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
        "init",
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
    worker_proc = subprocess.Popen(
        [sys.executable, "worker.py", "--mode", "demo", "--state-dir", str(state_dir)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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
            blocked_requests: list[str] = []

            def local_only(route):
                if urlparse(route.request.url).hostname in ("127.0.0.1", "localhost"):
                    route.continue_()
                else:
                    blocked_requests.append(urlparse(route.request.url).hostname or "unknown")
                    route.abort()

            context.route("**/*", local_only)
            page = context.new_page()
            local_fonts: list[int] = []
            page.on(
                "response",
                lambda response: (
                    local_fonts.append(response.status)
                    if response.url.endswith("/fonts/NotoSansSC-Regular.otf")
                    else None
                ),
            )

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

                # U01: no snapshots or notes; no README/CLI import needed.
                assert click_semantics_button("开始同业研究")
                page.wait_for_selector("text=暂无参照公司", timeout=10000)
                assert click_semantics_button("查找同业"), "Could not submit peer update"
                page.wait_for_selector("text=更新任务", timeout=10000)
                page.wait_for_selector("text=状态：完成", timeout=20000)
                page.get_by_role("button", name="查看本次结果/诊断", exact=True).click()
                page.wait_for_selector("text=当前完整榜", timeout=10000)
                result_url = page.url
                assert "job=" in result_url

                # Actual values and annual trend, not a shell/canvas or rank-only page.
                for width in (360, 390, 430):
                    page.set_viewport_size({"width": width, "height": 844})
                    pb = page.get_by_text("PB 1.15 倍", exact=False).first
                    pb.scroll_into_view_if_needed()
                    assert pb.is_visible()
                    bounds = pb.bounding_box()
                    assert (
                        bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width + 1
                    )
                    assert page.get_by_text("2025年 ROE 14.00%", exact=True).count() == 1
                page.set_viewport_size({"width": 390, "height": 844})
                page.get_by_role("button", name="展开依据、来源与排除原因", exact=True).click()
                page.get_by_text(
                    "沪深主板、TuShare粗行业", exact=False
                ).scroll_into_view_if_needed()
                assert page.get_by_text("最多50家", exact=False).is_visible()
                assert page.get_by_text("与参照比较：", exact=False).count() >= 1
                page.get_by_role("button", name="展开依据、来源与排除原因", exact=True).click()

                # Add anchor directly; no automatic ack.
                page.get_by_role("button", name="加入观察", exact=True).nth(1).click()
                page.wait_for_selector("text=已加入观察", timeout=10000)
                with sqlite3.connect(state_dir / "workspace.sqlite3") as conn:
                    assert conn.execute(
                        "SELECT ack_run_id FROM watch_items WHERE code='600001.SH'"
                    ).fetchone() == (None,)
                page.get_by_role("button", name="查看我的研究", exact=True).first.click()
                page.wait_for_selector("text=公司筛选事实", timeout=10000)
                time.sleep(1)

                # 4. Fill in personal research reason
                test_reason = "移动端自动化测试理由"
                reason_input = page.locator("textarea[aria-label*='理由']").first
                reason_input.click()
                reason_input.press_sequentially(test_reason)
                time.sleep(1)

                # Dirty browser Back must show a real modal, not silently lose the edit.
                company_url = page.url
                page.go_back()
                unsaved_prompt = page.get_by_text("有未保存的研究记录", exact=True)
                unsaved_prompt.wait_for(state="visible", timeout=10000)
                page.get_by_role("button", name="继续编辑", exact=True).click()
                unsaved_prompt.wait_for(state="hidden", timeout=10000)
                expect(page).to_have_url(company_url)
                expect(reason_input).to_have_value(test_reason)
                assert reason_input.is_visible(), "Cancel did not keep the editor visible"

                # 5. Verify Save button accessibility and interactive state via semantic locator
                save_btn = page.locator("flt-semantics[role='button']:has-text('保存判断')").first
                save_btn.scroll_into_view_if_needed()
                assert save_btn.is_visible(), "Save button is not visible in accessibility tree"
                assert save_btn.is_enabled(), "Save button is not enabled"

                save_btn.click()

                # UI success is mandatory; persisted data must NEVER bypass this assertion.
                page.get_by_text("保存成功", exact=False).wait_for(state="visible", timeout=10000)

                # 6. Independently verify persistence, in addition to (not instead of) visible feedback
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

                # U03: real browser back preserves the task's anchor/result; never submits again.
                page.go_back()
                page.wait_for_selector("text=当前完整榜", timeout=10000)
                assert page.url == result_url
                page.get_by_role("button", name="查看我的研究", exact=True).first.click()
                page.wait_for_selector("text=公司筛选事实", timeout=10000)

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

                # 8. Reload returns to safe home; never replays a write/update.
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

                # Later partial synthetic observation must explain its gaps, not replace the old top.
                partial = json.loads((FIXTURES_DIR / "peer_second_change.json").read_text())
                partial["screened_at"] = datetime.now(UTC).isoformat()
                partial["generated_at"] = partial["screened_at"]
                partial_path = state_dir / "synthetic_partial.json"
                partial_path.write_text(json.dumps(partial))
                import_snapshot(state_dir, partial_path, "demo")
                page.locator("[aria-label='同业发现']").first.click()
                page.wait_for_selector("text=本次尝试：部分完成", timeout=10000)
                page.wait_for_selector("text=当前展示上次完整榜", timeout=10000)
                assert page.get_by_text("PB 1.85 倍", exact=False).count() == 1
                page.get_by_role("button", name="查看本次诊断", exact=True).click()
                page.get_by_text("PB 1.80 倍", exact=False).wait_for(state="visible", timeout=10000)
                assert page.get_by_text("PB 1.80 倍", exact=False).count() == 1
                assert 200 in local_fonts, "Chinese font was not loaded from local assets"
                # Refresh revalidates/reads the selected result; it must not replay its job.
                page.reload()
                page.wait_for_load_state("domcontentloaded")
                enable_accessibility()
                page.wait_for_selector("text=本次尝试：部分完成", timeout=10000)
                page.wait_for_selector("text=当前展示上次完整榜", timeout=10000)
                with sqlite3.connect(state_dir / "workspace.sqlite3") as conn:
                    assert conn.execute("SELECT count(*) FROM update_jobs").fetchone()[0] == 1
                # Real confirmation/cancellation and visible deletion feedback, not DB fallbacks.
                page.locator("[aria-label='我的研究']").first.click()
                page.get_by_role("button", name="查看详情", exact=True).first.click()
                removal_reason = page.locator("textarea[aria-label*='理由']").first
                removal_reason.click()  # Flutter syncs the editing value when focused.
                expect(removal_reason).to_have_value(test_reason)
                removal_reason.press("End")
                removal_reason.press_sequentially("（未保存）")
                page.get_by_role("button", name="删除个人研究记录", exact=True).click()
                page.get_by_role("button", name="取消", exact=True).click()
                removal_reason.click()
                expect(removal_reason).to_have_value(test_reason + "（未保存）")
                page.get_by_role("button", name="删除个人研究记录", exact=True).click()
                page.get_by_role("button", name="确认操作", exact=True).click()
                page.get_by_text("已删除个人研究记录，历史扫描保留。", exact=True).wait_for(
                    state="visible"
                )
                page.get_by_text("关注清单 (0)", exact=True).wait_for(state="visible")
                with sqlite3.connect(state_dir / "workspace.sqlite3") as conn:
                    assert conn.execute("SELECT count(*) FROM watch_items").fetchone()[0] == 0
                    assert conn.execute("SELECT count(*) FROM screen_runs").fetchone()[0] == 2
                # Immutable results remain available, and re-adding does not restore old notes/ack.
                page.goto(company_url)
                page.wait_for_load_state("domcontentloaded")
                enable_accessibility()
                page.wait_for_selector("text=公司筛选事实", timeout=10000)
                fresh_reason = page.locator("textarea[aria-label*='理由']").first
                fresh_reason.click()
                expect(fresh_reason).to_have_value("")
                page.get_by_role("button", name="保存判断", exact=True).click()
                page.get_by_text("保存成功", exact=False).wait_for(state="visible")
                with sqlite3.connect(state_dir / "workspace.sqlite3") as conn:
                    assert conn.execute(
                        "SELECT reason,ack_run_id FROM watch_items WHERE code='600001.SH'"
                    ).fetchone() == ("", None)
                    # Synthetic terminal failure, inserted atomically; worker never receives it.
                    conn.execute("""INSERT INTO update_jobs
                        (job_id,request_id,kind,payload_json,dedupe_key,status,phase,requested_at,updated_at,finished_at)
                        SELECT 'synthetic-failure','synthetic-failure',kind,payload_json,dedupe_key,
                        'failed','failed',requested_at,updated_at,updated_at FROM update_jobs LIMIT 1""")
                    target_date = json.loads(
                        conn.execute(
                            "SELECT payload_json FROM update_jobs WHERE job_id='synthetic-failure'"
                        ).fetchone()[0]
                    )["target_date"]
                page.locator("[aria-label='我的研究']").first.click()
                failed_label = f"600001.SH · {target_date} · 失败"
                page.get_by_role("button", name=failed_label, exact=True).click()
                page.get_by_role("button", name="清理失败任务", exact=True).click()
                page.get_by_role("button", name="取消", exact=True).click()
                page.get_by_role("button", name="清理失败任务", exact=True).click()
                page.get_by_role("button", name="确认操作", exact=True).click()
                page.get_by_text("已从列表清理失败任务，防重放记录保留。", exact=True).wait_for(
                    state="visible"
                )
                expect(page.get_by_role("button", name=failed_label, exact=True)).to_have_count(0)
                page.reload()
                page.wait_for_load_state("domcontentloaded")
                enable_accessibility()
                page.get_by_text("关注清单 (1)", exact=True).wait_for(state="visible")
                expect(page.get_by_role("button", name=failed_label, exact=True)).to_have_count(0)
                with sqlite3.connect(state_dir / "workspace.sqlite3") as conn:
                    assert (
                        conn.execute(
                            "SELECT phase FROM update_jobs WHERE job_id='synthetic-failure'"
                        ).fetchone()[0]
                        == "dismissed"
                    )
                # Flet may attempt optional CDN resources; every external request was aborted.
                print(f"External requests blocked (none allowed): {sorted(set(blocked_requests))}")
                print(
                    "Mobile browser test passed (360/390/430px, synthetic, not a real device): empty -> submit -> worker -> comparison -> add -> dirty Back/cancel -> visible save feedback -> back -> reload -> partial/old board -> delete/cancel/re-add -> dismiss failure/reload."
                )
                return 0
            except Exception as exc:
                try:
                    page.screenshot(path=str(SCREENSHOT_PATH))
                    print(f"Saved failure screenshot to {SCREENSHOT_PATH}", file=sys.stderr)
                except Exception:
                    pass
                print(f"Mobile browser test assertion failed: {exc}", file=sys.stderr)
                traceback.print_exc()
                return 1
            finally:
                context.close()
                browser.close()
    finally:
        server_proc.terminate()
        server_proc.wait(timeout=5)
        worker_proc.terminate()
        worker_proc.wait(timeout=5)
        temp_dir.cleanup()


if __name__ == "__main__":
    sys.exit(main())

"""Mobile end-to-end browser smoke test with isolated temporary demo workspace."""

import argparse
import json
import os
import re
import sqlite3
import statistics
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
SCREENSHOT_PATH = Path(f"/tmp/mobile_test_failure_{time.time_ns()}.png")

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


def click_settled(page, control):
    """Wait for Flutter's semantic scroll geometry; never force a hidden hit target."""
    control.scroll_into_view_if_needed()
    handle = control.element_handle()
    page.wait_for_function(
        """el => {
        const r = el.getBoundingClientRect();
        const geometry = [r.x, r.y, r.width, r.height].join(',');
        if (el._testGeometry !== geometry) {
            el._testGeometry = geometry;
            el._testStableSince = performance.now();
        }
        return performance.now() - el._testStableSince >= 250;
    }""",
        arg=handle,
        timeout=5000,
    )
    box = control.bounding_box()
    footer = page.get_by_role("tablist").bounding_box()
    assert box and footer
    if box["y"] < 0 or box["y"] + box["height"] > footer["y"]:
        page.mouse.move(page.viewport_size["width"] / 2, footer["y"] / 2)
        page.mouse.wheel(0, box["y"] + box["height"] / 2 - footer["y"] / 2)
        page.wait_for_function(
            """el => {
            const r = el.getBoundingClientRect();
            const bottom = document.querySelector('[role=tablist]').getBoundingClientRect().top;
            return r.top >= 0 && r.bottom <= bottom;
        }""",
            arg=handle,
            timeout=5000,
        )
    control.click()


def check_reliability(page, base_url, state_dir, enable_accessibility):
    """Small repeatable real-input loop, without the full task/snapshot scenario."""
    from playwright.sync_api import expect

    from auth import create_demo_actor
    from services import save_watch

    run = import_snapshot(state_dir, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    save_watch(create_demo_actor(), "600001.SH", run, {}, 0, state_dir, "demo")
    sockets = []

    def connect(ws):
        assert urlparse(ws.url).hostname == "127.0.0.1"
        sockets.append((ws, ws.connect_to_server()))

    page.route_web_socket("**/*", connect)
    page.goto(f"{base_url}/")
    enable_accessibility()
    previous = ("", "")
    for width in (360, 390, 430):
        page.set_viewport_size({"width": width, "height": 844})
        samples = []
        for attempt in range(1, 11):
            print(f"Reliability {width}px #{attempt}: open/input/save/reconnect/return", flush=True)
            start = time.perf_counter()
            page.get_by_role("button", name="查看详情", exact=True).first.click()
            expect(page).to_have_url(re.compile(r"/company/600001.SH$"))
            notes = page.get_by_role("button", name=re.compile("^(展开|收起)可选笔记"))
            if notes.inner_text().startswith("展开"):
                notes.click()
            reason = page.get_by_role("textbox", name=re.compile("一句理由"))
            next_check = page.get_by_role("textbox", name="下一步核查提示", exact=False)
            reason_value, next_value = f"合成理由{width}-{attempt}", f"核查原文{width}-{attempt}"
            for field, value, old_value in (
                (reason, reason_value, previous[0]),
                (next_check, next_value, previous[1]),
            ):
                click_settled(page, field)
                expect(field).to_be_focused()
                # Flutter populates the editor after focus; don't select its transient empty value.
                expect(field).to_have_value(old_value)
                page.keyboard.press("ControlOrMeta+A")
                page.wait_for_function(
                    "el => el.selectionStart === 0 && el.selectionEnd === el.value.length",
                    arg=field.element_handle(),
                )
                page.keyboard.press("Backspace")
                expect(field).to_have_value("")
                page.keyboard.type(value)
                expect(field).to_have_value(value)
            save = page.get_by_role("button", name="保存笔记与状态", exact=True)
            click_settled(page, save)
            expect(page.get_by_text("保存成功", exact=False)).to_be_visible()
            with sqlite3.connect(state_dir / "workspace.sqlite3") as conn:
                assert conn.execute(
                    "SELECT reason,next_check FROM watch_items WHERE code='600001.SH'"
                ).fetchone() == (reason_value, next_value)
                assert conn.execute("SELECT count(*) FROM update_jobs").fetchone()[0] == 0

            count = len(sockets)
            old_editor = reason.element_handle()
            client, server = sockets[-1]
            server.close(code=1012, reason="synthetic reconnect")
            client.close(code=1012, reason="synthetic reconnect")
            deadline = time.monotonic() + 15
            while len(sockets) == count and time.monotonic() < deadline:
                page.wait_for_timeout(50)  # Poll an observed connection, not a layout delay.
            assert len(sockets) > count, "WebSocket did not reconnect"
            page.wait_for_function("el => !el.isConnected", arg=old_editor)
            expect(notes).to_be_visible()
            if notes.inner_text().startswith("展开"):
                click_settled(page, notes)
            click_settled(page, reason)
            expect(reason).to_have_value(reason_value)
            click_settled(page, next_check)
            expect(next_check).to_have_value(next_value)
            page.get_by_role("tab", name="我的关注", exact=False).click()
            expect(page).to_have_url(f"{base_url}/")
            expect(
                page.get_by_text(next_value, exact=False)
                .or_(page.get_by_role("group", name=re.compile(re.escape(next_value))))
                .first
            ).to_be_visible()
            previous = (reason_value, next_value)
            samples.append(time.perf_counter() - start)
        print(
            f"Reliability {width}px: 10/10; full-loop seconds median={statistics.median(samples):.2f}, max={max(samples):.2f}",
            flush=True,
        )


def main(*, reliability: bool = False) -> int:
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
                if reliability:
                    check_reliability(page, base_url, state_dir, enable_accessibility)
                    print(
                        "Reliability browser checks passed: 360/390/430px × 10; synthetic, not a real device."
                    )
                    return 0
                # 1. Open home page
                page.goto(f"{base_url}/", timeout=15000)
                page.wait_for_load_state("domcontentloaded")
                enable_accessibility()

                # Click the top-right Settings icon, then its back arrow (real browser events).
                page.locator("flt-semantics[role='button']").first.click()
                page.wait_for_selector("text=账户与运行信息", timeout=10000)
                page.locator("flt-semantics[role='button']").nth(1).click()
                page.wait_for_selector("text=估值基准日", timeout=10000)

                # U01: no snapshots or notes; no README/CLI import needed.
                entry = page.get_by_role("button", name="前往同业发现", exact=True)
                expect(entry).to_have_count(1)
                expect(page.get_by_role("button", name="开始同业研究", exact=True)).to_have_count(0)
                for width in (360, 390, 430):
                    page.set_viewport_size({"width": width, "height": 844})
                    title = page.get_by_text("暂无关注的公司", exact=True)
                    title.scroll_into_view_if_needed()
                    expect(title).to_be_visible()
                    entry.scroll_into_view_if_needed()
                    expect(entry).to_be_visible()
                    bounds = entry.bounding_box()
                    assert (
                        bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width + 1
                    )
                page.set_viewport_size({"width": 390, "height": 844})
                entry.click()
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
                    comparison = page.get_by_text(
                        "PB低于本次同口径合格样本中位数", exact=False
                    ).first
                    comparison.scroll_into_view_if_needed()
                    assert comparison.is_visible()
                    bounds = comparison.bounding_box()
                    assert (
                        bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width + 1
                    )
                    assert (
                        page.get_by_text("ROE趋势（2023—2025）：连续上升", exact=True).count() >= 1
                    )
                    assert page.get_by_text("尚未核查：", exact=False).count() >= 1
                page.set_viewport_size({"width": 390, "height": 844})
                page.get_by_role("button", name="展开依据、来源与排除原因", exact=True).click()
                page.get_by_text(
                    "沪深主板、TuShare粗行业", exact=False
                ).scroll_into_view_if_needed()
                assert page.get_by_text(
                    "沪深主板、TuShare粗行业、按市值最多50家", exact=False
                ).is_visible()
                assert page.get_by_text("与参照比较：", exact=False).count() >= 1
                page.get_by_role("button", name="展开依据、来源与排除原因", exact=True).click()

                # Add anchor directly; no automatic ack.
                page.get_by_role("button", name="关注", exact=True).nth(1).click()
                page.wait_for_selector("text=已关注，无需填写笔记", timeout=10000)
                with sqlite3.connect(state_dir / "workspace.sqlite3") as conn:
                    assert conn.execute(
                        "SELECT ack_run_id FROM watch_items WHERE code='600001.SH'"
                    ).fetchone() == (None,)
                page.get_by_role("button", name="查看关注", exact=True).first.click()
                page.wait_for_selector("text=公司筛选事实", timeout=10000)
                time.sleep(1)

                def open_notes():
                    notes_btn = page.get_by_role(
                        "button", name=re.compile("^(展开|收起)可选笔记、状态与已阅$")
                    )
                    notes_btn.scroll_into_view_if_needed()
                    reason = page.locator("textarea[aria-label*='理由']").first
                    if notes_btn.inner_text().startswith("展开"):
                        notes_btn.click()
                    expect(notes_btn).to_have_text("收起可选笔记、状态与已阅")
                    reason.scroll_into_view_if_needed()
                    reason.wait_for(state="visible", timeout=10000)
                    time.sleep(0.5)  # Flutter scroll animation must finish before pointer input.

                # First review has current facts, not empty old-value arrows or expanded metadata.
                expect(
                    page.get_by_text("当前资料（首次待阅，无已阅基准）", exact=True)
                ).to_be_visible()
                fields = page.get_by_role("button", name="查看全部字段与来源", exact=True)
                group = page.get_by_text("指标与公司状态", exact=True)
                expect(group).not_to_be_visible()
                fields.click()
                expect(group).to_be_visible()
                expect(page.get_by_text("PB（倍） · 首次待阅", exact=True)).to_be_visible()
                metadata = page.get_by_text("2025年报告类型 · 首次待阅", exact=True)
                expect(metadata).not_to_be_visible()
                missing = page.get_by_text("未记录的补充字段（", exact=False)
                missing.scroll_into_view_if_needed()
                time.sleep(0.5)
                missing.click()
                expect(metadata).to_be_visible()
                fields.scroll_into_view_if_needed()
                time.sleep(0.5)
                fields.click()
                expect(group).not_to_be_visible()
                # Following is complete without a form; old notes are optional and preserved.
                expect(page.locator("textarea[aria-label*='理由']").first).not_to_be_visible()
                open_notes()
                # 4. Fill in optional personal notes
                test_reason = "移动端自动化测试理由"
                reason_input = page.locator("textarea[aria-label*='理由']").first
                reason_input.click()
                reason_input.press_sequentially(test_reason)
                next_step = "核查经营现金流与利润差异"
                next_input = page.get_by_role("textbox", name="下一步核查提示", exact=False)
                next_input.scroll_into_view_if_needed()
                time.sleep(0.5)  # Wait for Flutter scrolling before hitting the input.
                next_input.click()
                time.sleep(0.5)  # Allow Flutter's single-line editor focus to synchronize.
                next_input.press_sequentially(next_step, delay=40)
                expect(next_input).to_have_value(next_step)
                time.sleep(1)

                # Dirty browser Back must show a real modal, not silently lose the edit.
                company_url = page.url
                page.go_back()
                unsaved_prompt = page.get_by_text("有未保存的研究记录", exact=True)
                unsaved_prompt.wait_for(state="visible", timeout=10000)
                page.get_by_role("button", name="继续编辑", exact=True).click()
                unsaved_prompt.wait_for(state="hidden", timeout=10000)
                expect(page).to_have_url(company_url)
                reason_input.click()  # Flet publishes the editing value when focused.
                expect(reason_input).to_have_value(test_reason)
                assert reason_input.is_visible(), "Cancel did not keep the editor visible"
                next_input.click()
                expect(next_input).to_have_value(next_step)

                # 5. Verify Save button accessibility and interactive state via semantic locator
                save_btn = page.get_by_role("button", name="保存笔记与状态", exact=True)
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
                assert row["next_check"] == next_step, repr(row["next_check"])
                assert row["revision"] >= 1, "Revision should be >= 1"
                assert row["ack_run_id"] is None, "Saving notes must not acknowledge facts"
                page.get_by_text("首次待阅：无已阅基准", exact=False).wait_for(state="visible")
                page.get_by_role("button", name="标记本次变化已阅", exact=True).click()
                page.get_by_text("已标记已阅", exact=False).wait_for(state="visible")
                with sqlite3.connect(db_path) as conn:
                    first_ack = conn.execute(
                        "SELECT ack_run_id FROM watch_items WHERE code='600001.SH'"
                    ).fetchone()[0]
                    assert first_ack is not None

                # U03: real browser back preserves the task's anchor/result; never submits again.
                page.go_back()
                page.wait_for_selector("text=当前完整榜", timeout=10000)
                assert page.url == result_url
                page.get_by_role("button", name="查看关注", exact=True).first.click()
                page.wait_for_selector("text=公司筛选事实", timeout=10000)

                # 7. Navigate back to Home and verify persisted item on home list
                home_nav = page.locator("[aria-label='我的关注']").first
                if home_nav.is_visible():
                    home_nav.click()
                else:
                    assert click_semantics_button("我的关注"), "Could not click 我的关注 tab"
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

                expect(
                    page.get_by_role("group", name=f"上次下一步：{next_step}", exact=False)
                ).to_be_visible()
                # Pause is personal state only; hidden companies and their notes survive reload.
                with sqlite3.connect(db_path) as conn:
                    preserved = conn.execute(
                        "SELECT reason,next_check,note_url,added_run_id,ack_run_id,ack_at FROM watch_items WHERE code='600001.SH'"
                    ).fetchone()
                page.get_by_role("button", name="暂停关注", exact=True).click()
                page.get_by_text(
                    "已暂停关注，不再计入需要复看；笔记和已阅基准保留。", exact=True
                ).wait_for(state="visible")
                expect(page.get_by_role("button", name="查看详情", exact=True)).to_have_count(0)
                page.reload()
                page.wait_for_load_state("domcontentloaded")
                enable_accessibility()
                expect(page.get_by_role("button", name="查看详情", exact=True)).to_have_count(0)
                page.get_by_role("button", name="显示已暂停 (1)", exact=True).click()
                resume = page.get_by_role("button", name="恢复为观察", exact=True)
                for width in (360, 390, 430):
                    page.set_viewport_size({"width": width, "height": 844})
                    resume.scroll_into_view_if_needed()
                    expect(resume).to_be_visible()
                    bounds = resume.bounding_box()
                    assert (
                        bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width + 1
                    )
                page.set_viewport_size({"width": 390, "height": 844})
                resume.click()
                page.get_by_text("已恢复为观察，笔记和已阅基准保留。", exact=True).wait_for(
                    state="visible"
                )
                with sqlite3.connect(db_path) as conn:
                    assert (
                        conn.execute(
                            "SELECT reason,next_check,note_url,added_run_id,ack_run_id,ack_at FROM watch_items WHERE code='600001.SH'"
                        ).fetchone()
                        == preserved
                    )
                    assert conn.execute(
                        "SELECT status FROM watch_items WHERE code='600001.SH'"
                    ).fetchone() == ("observe",)
                    assert conn.execute("SELECT count(*) FROM update_jobs").fetchone()[0] == 1

                # A real UI submission reaches the independent fixed-watch worker.
                update = page.get_by_role("button", name="更新资料", exact=True)
                for width in (360, 390, 430):
                    page.set_viewport_size({"width": width, "height": 844})
                    update.scroll_into_view_if_needed()
                    bounds = update.bounding_box()
                    assert (
                        bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width + 1
                    )
                page.set_viewport_size({"width": 390, "height": 844})
                update.click()
                page.get_by_text("固定关注范围（1家）：600001.SH", exact=True).wait_for(
                    state="visible"
                )
                page.get_by_role("button", name="查看关注最新资料", exact=True).wait_for(
                    state="visible", timeout=15000
                )
                with sqlite3.connect(db_path) as conn:
                    status, phase, result_run = conn.execute(
                        "SELECT status,phase,result_run_id FROM update_jobs WHERE kind='watch'"
                    ).fetchone()
                    assert (status, phase) == ("succeeded", "complete") and result_run
                    assert (
                        conn.execute(
                            "SELECT reason,next_check,note_url,added_run_id,ack_run_id,ack_at FROM watch_items WHERE code='600001.SH'"
                        ).fetchone()
                        == preserved
                    )
                    assert (
                        conn.execute(
                            "SELECT count(*) FROM screen_runs WHERE kind='peer'"
                        ).fetchone()[0]
                        == 1
                    )  # Only the explicit peer scan; watch must not create another board.
                page.reload()
                page.wait_for_load_state("domcontentloaded")
                enable_accessibility()
                page.get_by_role("button", name="查看关注最新资料", exact=True).click()

                # 9. Wait for the asynchronous home render, then inspect persisted notes.
                detail_btn = page.get_by_role("button", name="查看详情", exact=True).first
                detail_btn.wait_for(state="visible", timeout=10000)
                detail_btn.click()
                page.wait_for_selector("text=公司资料事实", timeout=10000)
                expect(
                    page.get_by_text(
                        "本次为固定关注事实更新，不重新选择同业、不生成新名次；PB中位数不适用。",
                        exact=True,
                    )
                ).to_be_visible()
                expect(page.get_by_text("最近同业名次：", exact=False)).to_be_visible()
                time.sleep(1)
                open_notes()
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
                    assert conn.execute("SELECT count(*) FROM update_jobs").fetchone()[0] == 2
                # Real confirmation/cancellation and visible deletion feedback, not DB fallbacks.
                page.locator("[aria-label='我的关注']").first.click()
                expect(page.get_by_text("指标与资料变化", exact=True)).to_be_visible()
                page.get_by_role("button", name="查看详情", exact=True).first.click()
                page.wait_for_selector("text=公司筛选事实", timeout=10000)
                # S2: the comparison is visible before confirmation; opening it is read-only.
                for width in (360, 390, 430):
                    page.set_viewport_size({"width": width, "height": 844})
                    delta = page.get_by_text("【变化】PB（倍）：1.85 → 1.8", exact=True)
                    delta.scroll_into_view_if_needed()
                    expect(delta).to_be_visible()
                    bounds = delta.bounding_box()
                    assert (
                        bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width + 1
                    )
                page.set_viewport_size({"width": 390, "height": 844})
                expect(
                    page.get_by_text("【变化】2025年ROE（%）：15.8 → 16.5", exact=True)
                ).to_be_visible()
                with sqlite3.connect(db_path) as conn:
                    assert (
                        conn.execute(
                            "SELECT ack_run_id FROM watch_items WHERE code='600001.SH'"
                        ).fetchone()[0]
                        == first_ack
                    )
                open_notes()
                page.get_by_role("button", name="标记本次变化已阅", exact=True).click()
                page.get_by_text("已标记已阅", exact=False).wait_for(state="visible")
                with sqlite3.connect(db_path) as conn:
                    assert (
                        conn.execute(
                            "SELECT ack_run_id FROM watch_items WHERE code='600001.SH'"
                        ).fetchone()[0]
                        != first_ack
                    )
                removal_reason = page.locator("textarea[aria-label*='理由']").first
                removal_reason.click()  # Flutter syncs the editing value when focused.
                expect(removal_reason).to_have_value(test_reason)
                removal_reason.press("End")
                removal_reason.press_sequentially("（未保存）")
                delete_button = page.get_by_role("button", name="删除个人研究记录", exact=True)
                delete_button.scroll_into_view_if_needed()
                delete_button.click()
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
                    assert conn.execute("SELECT count(*) FROM screen_runs").fetchone()[0] == 3
                # Immutable results remain available, and re-adding does not restore old notes/ack.
                page.goto(company_url)
                page.wait_for_load_state("domcontentloaded")
                enable_accessibility()
                page.wait_for_selector("text=公司筛选事实", timeout=10000)
                # This session remembers the previously opened section; following still needs no form.
                page.get_by_role("button", name="收起可选笔记、状态与已阅", exact=True).click()
                expect(page.locator("textarea[aria-label*='理由']").first).not_to_be_visible()
                page.get_by_role("button", name="关注", exact=True).click()
                expect(page.get_by_role("button", name="已关注", exact=True)).to_be_disabled()
                open_notes()
                fresh_reason = page.locator("textarea[aria-label*='理由']").first
                fresh_reason.click()
                expect(fresh_reason).to_have_value("")
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
                page.locator("[aria-label='我的关注']").first.click()
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
                # Re-scan is a confirmed submission, not another link to historical results.
                with sqlite3.connect(db_path) as conn:
                    peer_job = conn.execute(
                        "SELECT job_id FROM update_jobs WHERE kind='peer' AND status='succeeded' LIMIT 1"
                    ).fetchone()[0]
                    count_before = conn.execute("SELECT count(*) FROM update_jobs").fetchone()[0]
                page.goto(f"{base_url}/jobs/{peer_job}")
                page.wait_for_load_state("domcontentloaded")
                enable_accessibility()
                page.get_by_role("button", name="重新扫描", exact=True).click()
                page.get_by_role("button", name="取消", exact=True).click()
                with sqlite3.connect(db_path) as conn:
                    assert (
                        conn.execute("SELECT count(*) FROM update_jobs").fetchone()[0]
                        == count_before
                    )
                page.get_by_role("button", name="重新扫描", exact=True).click()
                page.get_by_role("button", name="确认重新扫描", exact=True).click()
                expect(page).not_to_have_url(f"{base_url}/jobs/{peer_job}")
                page.get_by_text("状态：完成", exact=False).wait_for(state="visible", timeout=30000)
                with sqlite3.connect(db_path) as conn:
                    assert (
                        conn.execute("SELECT count(*) FROM update_jobs").fetchone()[0]
                        == count_before + 1
                    )
                page.get_by_role("button", name="查看本次结果/诊断", exact=True).click()
                page.get_by_text("PB 1.85 倍", exact=False).first.wait_for(state="visible")
                # Flet may attempt optional CDN resources; every external request was aborted.
                print(f"External requests blocked (none allowed): {sorted(set(blocked_requests))}")
                print(
                    "Mobile browser test passed (360/390/430px, synthetic, not a real device): empty -> submit -> worker -> comparison -> add -> dirty Back/cancel -> visible save feedback -> back -> reload -> pause/reload/resume -> fixed watch submit/worker/reload -> partial/old board -> acknowledged/current comparison -> explicit ack -> delete/cancel/re-add -> dismiss failure/reload -> rescan cancel/confirm/worker/result."
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reliability",
        action="store_true",
        help="Repeat the focused input/reconnect flow 10 times per width",
    )
    sys.exit(main(reliability=parser.parse_args().reliability))

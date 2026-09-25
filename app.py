"""Flet Web application for personal research workbench."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlparse

from auth import (
    Actor,
    AuthError,
    check_production_auth_config,
    create_demo_actor,
    create_owner_actor,
)
from screen import fmt_number
from services import (
    ServiceError,
    get_company_context,
    get_home,
    get_peer_discover,
    get_update_job,
    job_status_label,
    list_update_jobs,
    mark_seen,
    peer_anchors,
    request_peer_update,
    save_watch,
)
from workspace import WorkspaceError

RAW_MODE = os.getenv("APP_MODE", "demo")
if RAW_MODE not in ("demo", "production"):
    raise ValueError(f"Invalid APP_MODE: '{RAW_MODE}'. Must be 'demo' or 'production'.")
APP_MODE: Literal["demo", "production"] = "production" if RAW_MODE == "production" else "demo"
STATE_DIR = Path(os.getenv("STATE_DIR", ".local/demo")).expanduser().resolve()
TRACKER_ROOT = Path(os.environ["TRACKER_ROOT"]) if os.getenv("TRACKER_ROOT") else None
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8550")
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8550"))

os.environ.setdefault("FLET_SESSION_TIMEOUT", "3600")
os.environ.setdefault("FLET_OAUTH_STATE_TIMEOUT", "600")
FLET_SESSION_TIMEOUT = int(os.environ.get("FLET_SESSION_TIMEOUT", "3600"))
FLET_OAUTH_STATE_TIMEOUT = int(os.environ.get("FLET_OAUTH_STATE_TIMEOUT", "600"))
if FLET_SESSION_TIMEOUT <= 0 or FLET_OAUTH_STATE_TIMEOUT <= 0:
    raise ValueError("FLET_SESSION_TIMEOUT and FLET_OAUTH_STATE_TIMEOUT must be positive integers.")

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
GITHUB_ALLOWED_USER_ID = os.getenv("GITHUB_ALLOWED_USER_ID", "")

# Verify production startup conditions immediately
if APP_MODE == "production":
    check_production_auth_config(GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, GITHUB_ALLOWED_USER_ID)
elif APP_MODE == "demo":
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(f"Demo mode only allows loopback host, got: {HOST}")


def build_app():
    import flet as ft

    async def main(page: ft.Page):
        page.title = "投研工作台"
        page.theme_mode = ft.ThemeMode.LIGHT
        page.fonts = {"NotoSansSC": "fonts/NotoSansSC-Regular.otf"}
        page.theme = ft.Theme(font_family="NotoSansSC")
        page.padding = 16
        page.scroll = ft.ScrollMode.AUTO

        # State per page session
        page_state: dict[str, Any] = {
            "actor": create_demo_actor() if APP_MODE == "demo" else None,
            "route": "/" if APP_MODE == "demo" else "/login",
            "selected_anchor": "600001.SH",
            "generation": 0,
            "drafts": {},
            "current_form_getter": None,
            "current_company_code": None,
            "current_form_baseline": None,
            "job_poll_task": None,
            "pending_peer_request": None,
            "connected": True,
            "company_return": "/",
            "scroll_positions": {},
            "expanded_sections": {},
        }

        # Explicit logout in settings handles actor revocation
        content_container = ft.Container(expand=True)
        login_msg = ft.Text("", size=13, color=ft.Colors.RED_700)

        async def navigate(route: str, *, from_browser: bool = False):
            getter = page_state.get("current_form_getter")
            actor = page_state.get("actor")
            if (
                route != page_state["route"]
                and route != "/login"
                and actor
                and actor.is_valid
                and callable(getter)
                and getter() != page_state.get("current_form_baseline")
            ):
                if from_browser:
                    await page.push_route(page_state["route"])
                current_gen = page_state["generation"]

                async def stay(e):
                    page.pop_dialog()

                async def discard(e):
                    page.pop_dialog()
                    if current_gen != page_state["generation"] or not actor.is_valid:
                        return
                    page_state["drafts"].pop(page_state.get("current_company_code"), None)
                    page_state["current_form_getter"] = None
                    await navigate(route)

                async def save_and_leave(e):
                    page.pop_dialog()
                    if current_gen != page_state["generation"] or not actor.is_valid:
                        return
                    await page_state["save_current_form"](e)
                    if (
                        current_gen == page_state["generation"]
                        and actor.is_valid
                        and getter() == page_state.get("current_form_baseline")
                    ):
                        await navigate(route)

                page.show_dialog(
                    ft.AlertDialog(
                        modal=True,
                        title=ft.Text("有未保存的研究记录"),
                        content=ft.Text("保存成功后才能离开；放弃不会更改已保存记录。"),
                        actions=[
                            ft.TextButton("继续编辑", on_click=stay),
                            ft.TextButton("放弃并离开", on_click=discard),
                            ft.TextButton("保存并离开", on_click=save_and_leave),
                        ],
                    )
                )
                return
            poll = page_state.get("job_poll_task")
            if poll is not None:
                poll.cancel()
                page_state["job_poll_task"] = None
            page_state["generation"] += 1
            if route.startswith("/company/") and not page_state["route"].startswith("/company/"):
                page_state["company_return"] = page_state["route"]
            page_state["route"] = route
            page_state["current_form_getter"] = None
            page_state["current_company_code"] = None
            page_state["current_form_baseline"] = None
            if not from_browser:
                await page.push_route(route)
            await render_current_view()
            page.update()
            await page.scroll_to(offset=page_state["scroll_positions"].get(route, 0), duration=0)

        async def on_route_change(e):
            if e.route != page_state["route"]:
                await navigate(e.route, from_browser=True)

        def on_scroll(e):
            page_state["scroll_positions"][page_state["route"]] = e.pixels

        page.on_route_change = on_route_change
        page.views[0].on_scroll = on_scroll

        async def go_back(e):
            await navigate(page_state["company_return"])

        async def go_home(e):
            await navigate("/")

        async def go_settings(e):
            await navigate("/settings")

        async def go_discover(e):
            await navigate("/discover?" + urlencode({"anchor": page_state["selected_anchor"]}))

        async def handle_login_failure(error_msg: str):
            page_state["generation"] += 1
            page_state.setdefault("drafts", {}).clear()
            page_state["current_form_getter"] = None
            page_state["current_company_code"] = None
            page_state["current_form_baseline"] = None
            old_actor = page_state.get("actor")
            if old_actor:
                old_actor.revoke()
            page_state["actor"] = None
            if APP_MODE == "production":
                page.logout()
            login_msg.value = error_msg
            await navigate("/login")

        # Configure OAuth for production mode (Same-tab authorization flow)
        provider = None
        if APP_MODE == "production":
            from flet.auth.providers import GitHubOAuthProvider

            provider = GitHubOAuthProvider(
                client_id=GITHUB_CLIENT_ID,
                client_secret=GITHUB_CLIENT_SECRET,
                redirect_url=f"{PUBLIC_BASE_URL}/oauth_callback",
            )

            async def on_open_auth_url(url: str):
                await ft.UrlLauncher().launch_url(url, web_only_window_name="_self")

            async def on_login(e: ft.LoginEvent):
                if e.error:
                    await handle_login_failure(f"登录失败: {e.error_description or e.error}")
                    return
                try:
                    user_data = (
                        page.auth.user
                        if (page.auth is not None and hasattr(page.auth, "user"))
                        else None
                    )
                    raw_id = getattr(user_data, "id", None) if user_data is not None else None
                    if raw_id is None and isinstance(user_data, dict):
                        raw_id = user_data.get("id")
                    actor = create_owner_actor(raw_id, GITHUB_ALLOWED_USER_ID)
                    page_state["actor"] = actor
                    login_msg.value = ""
                    await navigate("/")
                except AuthError as exc:
                    await handle_login_failure(f"登录被拒绝: {exc}")

            page.on_login = on_login

        async def on_logout(e):
            poll = page_state.get("job_poll_task")
            if poll is not None:
                poll.cancel()
                page_state["job_poll_task"] = None
            page_state["generation"] += 1
            page_state.setdefault("drafts", {}).clear()
            page_state["current_form_getter"] = None
            page_state["current_company_code"] = None
            page_state["current_form_baseline"] = None
            actor: Actor | None = page_state.get("actor")
            if actor:
                actor.revoke()
            page_state["actor"] = None
            if APP_MODE == "production":
                page.logout()
            content_container.content = await render_login()
            page.update()
            await navigate("/login")

        # --- Views ---
        async def render_login():
            async def trigger_login(e):
                if provider:
                    await page.login(
                        provider,
                        on_open_authorization_url=on_open_auth_url,
                        redirect_to_page=True,
                    )

            return ft.Column(
                controls=[
                    ft.Text("欢迎使用个人投研工作台", size=20, weight=ft.FontWeight.BOLD),
                    ft.Text(
                        "生产模式需要所有者登录以访问私人数据", size=14, color=ft.Colors.GREY_700
                    ),
                    ft.Button(
                        "使用 GitHub 登录",
                        icon=ft.Icons.LOGIN,
                        on_click=trigger_login,
                    ),
                    login_msg,
                ],
                spacing=16,
            )

        async def render_home(gen: int):
            actor: Actor | None = page_state.get("actor")
            if not actor or not actor.is_valid:
                return await render_login()

            data = await asyncio.to_thread(get_home, actor, STATE_DIR, APP_MODE)
            if gen != page_state["generation"] or not actor.is_valid:
                return await render_login()
            jobs = await asyncio.to_thread(list_update_jobs, actor, STATE_DIR, APP_MODE)
            if gen != page_state["generation"] or not actor.is_valid:
                return await render_login()
            items_controls: list[ft.Control] = []

            for it in data["watch_items"]:
                code = it["code"]
                badge_color = (
                    ft.Colors.BLUE_700 if it["status"] == "research" else ft.Colors.GREY_700
                )
                status_chip = ft.Container(
                    content=ft.Text(
                        "研究中"
                        if it["status"] == "research"
                        else ("观察" if it["status"] == "observe" else "暂停"),
                        size=12,
                        color=ft.Colors.WHITE,
                    ),
                    bgcolor=badge_color,
                    padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                    border_radius=4,
                )

                async def on_card_click(e, c=code):
                    await navigate(f"/company/{c}")

                change_desc = it["change_summary"] if it["has_change"] else "覆盖字段暂无未阅变化"
                items_controls.append(
                    ft.Card(
                        content=ft.Container(
                            padding=12,
                            on_click=on_card_click,
                            content=ft.Column(
                                controls=[
                                    ft.Row(
                                        controls=[
                                            ft.Text(
                                                f"{it['name']} ({code})",
                                                size=16,
                                                weight=ft.FontWeight.BOLD,
                                            ),
                                            status_chip,
                                        ],
                                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                    ),
                                    ft.Text(
                                        change_desc,
                                        size=13,
                                        color=ft.Colors.ORANGE_800
                                        if it["has_change"]
                                        else ft.Colors.GREY_700,
                                    ),
                                    ft.Row(
                                        controls=[
                                            ft.Text(
                                                f"数据日：{it.get('valuation_date', '暂无')} | 理由：{it['reason'] or '暂无'}",
                                                size=13,
                                                color=ft.Colors.BLACK_87,
                                                expand=True,
                                            ),
                                            ft.Button("查看详情", on_click=on_card_click),
                                        ],
                                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                    ),
                                ],
                                spacing=4,
                            ),
                        )
                    )
                )

            if not items_controls:
                items_controls.append(
                    ft.Text(
                        "暂无关注标的，可在同业发现中选择标的加入观察", color=ft.Colors.GREY_700
                    )
                )

            job_controls: list[ft.Control] = []
            for job in jobs:

                async def open_job(e, job_id=job["job_id"]):
                    await navigate(f"/jobs/{job_id}")

                job_controls.append(
                    ft.Button(
                        f"{job['anchor']} · {job['target_date']} · {job_status_label(job)}",
                        on_click=open_job,
                    )
                )

            return ft.Column(
                controls=[
                    ft.Container(
                        padding=12,
                        bgcolor=ft.Colors.BLUE_50,
                        border_radius=8,
                        content=ft.Row(
                            controls=[
                                ft.Column(
                                    [
                                        ft.Text("我的研究", size=18, weight=ft.FontWeight.BOLD),
                                        ft.Text(
                                            f"估值基准日：{data['valuation_date']} | 需要复看：{data['needs_review_count']} 家",
                                            size=13,
                                        ),
                                    ]
                                ),
                                ft.Button(
                                    "更新资料",
                                    disabled=True,
                                    tooltip="关注清单单独更新尚未实现；请到同业发现提交同业扫描",
                                ),
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        ),
                    ),
                    ft.Text("固定关注更新尚未接入；同业扫描不会替代关注清单更新。", size=13),
                    ft.Button(
                        "开始同业研究" if not data["total_watch_count"] else "前往同业扫描",
                        on_click=go_discover,
                    ),
                    ft.Divider(height=16),
                    ft.Text(
                        f"关注清单 ({data['total_watch_count']})",
                        size=15,
                        weight=ft.FontWeight.W_600,
                    ),
                    *items_controls,
                    *(
                        [ft.Text("最近同业更新", weight=ft.FontWeight.BOLD)] + job_controls
                        if job_controls
                        else []
                    ),
                ],
                spacing=8,
            )

        async def render_discover(gen: int):
            actor: Actor | None = page_state.get("actor")
            if not actor or not actor.is_valid:
                return await render_login()

            try:
                allowed = await asyncio.to_thread(peer_anchors, actor, TRACKER_ROOT, APP_MODE)
                anchor_error = ""
            except ServiceError as exc:
                allowed, anchor_error = [], str(exc)
            if gen != page_state["generation"] or not actor.is_valid:
                return await render_login()
            codes = {entry["code"] for entry in allowed}
            params = parse_qs(urlparse(page_state["route"]).query)
            anchor = params.get("anchor", [page_state["selected_anchor"]])[0]
            job_id = params.get("job", [None])[0]
            run_id = page_state.get("discover_boards", {}).get(page_state["route"])
            if anchor not in codes and not params.get("anchor"):
                anchor = allowed[0]["code"] if allowed else ""
                page_state["selected_anchor"] = anchor
            page_state["selected_anchor"] = anchor
            try:
                disc_data: dict[str, Any] = (
                    await asyncio.to_thread(
                        get_peer_discover,
                        actor,
                        anchor,
                        STATE_DIR,
                        APP_MODE,
                        job_id=job_id,
                        run_id=run_id,
                    )
                    if anchor
                    else {"has_run": False, "error": anchor_error or "暂无参照标的"}
                )
            except ServiceError as exc:
                disc_data = {"has_run": False, "error": str(exc)}
            if gen == page_state["generation"] and actor.is_valid and disc_data.get("has_run"):
                page_state.setdefault("discover_boards", {})[page_state["route"]] = disc_data[
                    "run_id"
                ]
            if gen != page_state["generation"] or not actor.is_valid:
                return await render_login()

            async def on_anchor_select(e):
                if actor.is_valid and anchor_field.value in codes:
                    page_state["selected_anchor"] = anchor_field.value
                    page_state["pending_peer_request"] = None
                    await navigate("/discover?" + urlencode({"anchor": anchor_field.value}))

            anchor_field = ft.Dropdown(
                label="参照公司",
                value=anchor if anchor in codes else None,
                options=[
                    ft.dropdown.Option(entry["code"], f"{entry['name']} ({entry['code']})")
                    for entry in allowed
                ],
                on_select=on_anchor_select,
                disabled=not allowed,
            )
            update_feedback = ft.Text("", color=ft.Colors.RED_700)
            submit_button = ft.Button("查找同业", disabled=anchor not in codes)

            async def on_submit(e):
                submit_button.disabled = True
                page.update()
                pending = page_state.get("pending_peer_request")
                if not pending or pending["anchor"] != anchor:
                    pending = {"anchor": anchor, "request_id": uuid.uuid4().hex}
                    page_state["pending_peer_request"] = pending
                try:
                    job = await asyncio.to_thread(
                        request_peer_update,
                        actor,
                        anchor,
                        pending["request_id"],
                        STATE_DIR,
                        APP_MODE,
                        TRACKER_ROOT,
                    )
                    if page_state.get("pending_peer_request") is pending:
                        page_state["pending_peer_request"] = None
                    if gen == page_state["generation"] and actor.is_valid:
                        await navigate(f"/jobs/{job['job_id']}")
                except (ServiceError, WorkspaceError, AuthError) as exc:
                    if gen == page_state["generation"] and actor.is_valid:
                        update_feedback.value = str(exc)
                        submit_button.disabled = False
                        page.update()

            submit_button.on_click = on_submit

            async def refresh_result(e):
                route = "/discover?" + urlencode({"anchor": anchor})
                page_state.setdefault("discover_boards", {}).pop(route, None)
                await navigate(route)

            def section(title: str, controls: list[ft.Control]) -> ft.Column:
                key = (page_state["route"], title)
                body = ft.Column(controls, visible=page_state["expanded_sections"].get(key, False))

                async def toggle(e):
                    if gen == page_state["generation"] and actor.is_valid:
                        body.visible = not body.visible
                        page_state["expanded_sections"][key] = body.visible
                        page.update()

                return ft.Column([ft.Button(title, on_click=toggle), body])

            def exclusion_text(row: dict[str, Any]) -> str:
                labels = {
                    "KNOWN_ST_WARNING": "已知风险警示",
                    "INVALID_PB": "PB缺失或非正值",
                    "NON_POSITIVE_ROE_MEAN": "三年ROE均值非正值",
                    "FINANCIAL_REQUEST_FAILED": "财务核查失败",
                    "MISSING_THREE_ANNUAL_REPORTS": "缺少三份年报",
                    "NON_CONSECUTIVE_ANNUAL_REPORTS": "年报年份不连续",
                    "LATEST_ANNUAL_REPORT_TOO_OLD": "最新年报过旧",
                    "AMBIGUOUS_VERSION": "年报版本有歧义",
                    "INVALID_ROE": "ROE数据无效",
                    "qualified": "符合本次筛选字段要求",
                }
                reasons = row.get("exclusions") or row.get("eligibility_reasons") or []
                return (
                    "；".join(
                        labels.get(str(r).split(":")[0], "其他数据缺口，需核查来源")
                        for r in reasons
                    )
                    or "未记录"
                )

            def fact_card(row: dict[str, Any], result: dict[str, Any], ranked: bool = True):
                code = row.get("code") or row.get("ts_code") or "未知代码"
                rank: dict[str, Any] = (
                    next((r for r in result["results"]["ranking"] if r.get("code") == code), {})
                    if ranked
                    else {}
                )
                status = disc_data.get("watch_statuses", {}).get(code)
                status_text = {"observe": "观察", "research": "研究中", "paused": "已暂停"}.get(
                    status, "未加入"
                )
                feedback = ft.Text("", size=13)
                add_button = ft.Button("加入观察", disabled=not row.get("code"))

                async def open_comp(e):
                    if gen == page_state["generation"] and actor.is_valid:
                        await navigate(f"/company/{code}")

                async def add_observation(e):
                    if (
                        gen != page_state["generation"]
                        or not actor.is_valid
                        or not page_state["connected"]
                    ):
                        return
                    add_button.disabled = True
                    page.update()
                    try:
                        await asyncio.to_thread(
                            save_watch,
                            actor,
                            code,
                            result["run_id"],
                            {"status": "observe"},
                            0,
                            STATE_DIR,
                            APP_MODE,
                        )
                        if (
                            gen == page_state["generation"]
                            and actor.is_valid
                            and page_state["connected"]
                        ):
                            add_button.content = "查看我的研究"
                            add_button.disabled = False
                            add_button.on_click = open_comp
                            feedback.value = "已加入观察；未自动标记已阅。已有记录不会被覆盖。"
                            page.update()
                    except (ServiceError, WorkspaceError, AuthError):
                        if gen == page_state["generation"] and actor.is_valid:
                            feedback.value = "加入未确认，请重试；重复加入不会覆盖原记录。"
                            add_button.disabled = False
                            page.update()

                add_button.on_click = add_observation
                annual = row.get("annual_roes") or []
                reasons = row.get("exclusions") or row.get("eligibility_reasons") or []
                return ft.Container(
                    padding=12,
                    bgcolor=ft.Colors.AMBER_50 if code == anchor else ft.Colors.WHITE,
                    border=ft.Border.all(1, ft.Colors.GREY_300),
                    border_radius=6,
                    content=ft.Column(
                        [
                            ft.Text(
                                f"{row.get('name') or code} ({code})"
                                + (" · 参照公司" if code == anchor else ""),
                                size=16,
                                weight=ft.FontWeight.BOLD,
                            ),
                            ft.Text(
                                (
                                    f"研究次序 {rank['position']}"
                                    if rank.get("position")
                                    else "未进入正式排名"
                                )
                                + f" · {status_text}"
                            ),
                            ft.Text(
                                f"PB {fmt_number(row.get('pb'))} 倍 · ROE三年均值 {fmt_number(row.get('roe_mean'))}%",
                                weight=ft.FontWeight.W_600,
                            ),
                            ft.Text(
                                f"估值日：{row.get('valuation_date') or result.get('valuation_date') or '未记录'}",
                                size=12,
                            ),
                            *[
                                ft.Text(
                                    f"{str(a.get('period') or '')[:4] or '年度未知'}年 ROE {fmt_number(a.get('roe_waa'))}%",
                                    size=14,
                                )
                                for a in annual
                            ],
                            *([ft.Text("逐年ROE缺失，不补零")] if not annual else []),
                            *(
                                [
                                    ft.Text(
                                        "排除/缺口：" + exclusion_text(row),
                                        color=ft.Colors.AMBER_900,
                                    )
                                ]
                                if not rank and reasons and reasons != ["qualified"]
                                else []
                            ),
                            ft.Row(
                                [
                                    ft.Button(
                                        "查看我的研究" if status else "查看", on_click=open_comp
                                    ),
                                    *([] if status else [add_button]),
                                ],
                                wrap=True,
                            ),
                            feedback,
                        ],
                        spacing=6,
                    ),
                )

            def evidence(result: dict[str, Any]) -> list[ft.Control]:
                scope = result.get("scope") or {}
                rows = result.get("rows", [])
                present_codes = {r.get("code") for r in rows}
                missing = [c for c in scope.get("selected_codes", []) if c not in present_codes]
                controls: list[ft.Control] = [
                    *(
                        [ft.Text("本次零合格候选；请核对业务排除与数据缺口，不改写为成功。")]
                        if not result["results"]["ranking"]
                        else []
                    ),
                    *(
                        [ft.Text("本次无原关注池外合格对象，不生成新的正式前三。")]
                        if result["results"].get("outside_watchlist_qualified_count") == 0
                        else []
                    ),
                    ft.Text(
                        f"行业：{scope.get('industry') or '未记录'} · 来源：{result.get('source')}"
                    ),
                    ft.Text(
                        f"枚举 {scope.get('enumerated_count', '未记录')} 家 / 截取 {len(scope.get('selected_codes', []))} 家 / 合格 {len(result['results']['ranking'])} 家"
                    ),
                    ft.Text(
                        "沪深主板、TuShare粗行业、按市值最多50家；偏向大市值，不是全行业或全市场。行业标签不能证明业务可比。"
                    ),
                    ft.Text(
                        "缺少估值："
                        + str(scope.get("excluded_missing_valuation") or "无记录")
                        + "；超出上限："
                        + str(scope.get("excluded_by_cap") or "无记录")
                    ),
                    ft.Text("已选但缺少公司行：" + ("、".join(missing) or "无")),
                    ft.Text(
                        "PB升序名次与三年ROE均值降序名次取平均，并列取平均名次；小分差不代表价值显著不同。"
                    ),
                    ft.Text(
                        "低PB需核查资产质量；高ROE需核查杠杆、净资产及一次性收益；逐年值防止均值遮盖下行。均为待核查问题，不是已发现风险或买入建议。"
                    ),
                    ft.Text("\n".join(result.get("review_lines", []))),
                ]
                for row in rows:
                    rank: dict[str, Any] = next(
                        (
                            r
                            for r in result["results"]["ranking"]
                            if r.get("code") == row.get("code")
                        ),
                        {},
                    )
                    annual = row.get("annual_roes") or []
                    controls.extend(
                        [
                            ft.Text(
                                f"{row.get('name') or row.get('code')} · {row.get('code')}",
                                weight=ft.FontWeight.BOLD,
                            ),
                            ft.Text(
                                f"PB名次 {fmt_number(rank.get('pb_rank'), 1)} / ROE名次 {fmt_number(rank.get('roe_rank'), 1)} / 平均名次 {fmt_number(rank.get('research_order'), 1)}"
                            ),
                            ft.Text("排除/核查：" + exclusion_text(row)),
                            ft.Text(
                                f"估值来源 {row.get('valuation_source') or '未记录'} / 财务来源 {row.get('financial_source') or '未记录'} / 核查 {row.get('financial_checked_at') or '未记录'}"
                            ),
                            *[
                                ft.Text(
                                    f"{a.get('period')} · 公告 {a.get('ann_date') or '未记录'} · 版本 {a.get('update_flag', '未记录')} · 选择依据 {a.get('selection_basis') or '未记录'} · 取得 {a.get('acquired_at') or '未记录'}",
                                    size=12,
                                )
                                for a in annual
                            ],
                        ]
                    )
                return controls

            rows_controls: list[ft.Control] = []
            attempt = disc_data.get("attempt")
            if attempt:
                rows_controls.append(
                    ft.Text(
                        f"本次尝试：{attempt['label']} · 目标数据日 {attempt.get('target_date') or '未知'}",
                        weight=ft.FontWeight.BOLD,
                    )
                )
                if attempt.get("error_summary"):
                    rows_controls.append(ft.Text(attempt["error_summary"], color=ft.Colors.RED_700))
                partial = attempt.get("result")
                if partial and partial["health"] != "complete":
                    rows_controls.append(
                        ft.Text("本次部分完成，不作为正式前三；可得事实与缺口见诊断。")
                    )
                    rows_controls.append(
                        section(
                            "查看本次诊断",
                            [
                                *[fact_card(row, partial, ranked=False) for row in partial["rows"]],
                                *evidence(partial),
                            ],
                        )
                    )
                if attempt.get("job_id"):

                    async def open_attempt(e):
                        await navigate(f"/jobs/{attempt['job_id']}")

                    rows_controls.append(ft.Button("查看更新任务", on_click=open_attempt))
            if disc_data.get("has_run"):
                rows_controls.append(
                    ft.Text(
                        (
                            "当前展示上次完整榜"
                            if disc_data.get("showing_previous")
                            else "当前完整榜"
                        )
                        + f" · 估值日 {disc_data['valuation_date']} · 扫描 {disc_data['captured_at']}",
                        size=13,
                    )
                )
                rows = {r.get("code") or r.get("ts_code"): r for r in disc_data["rows"]}
                ranking = disc_data["results"]["ranking"]
                date_sets = {r.get("valuation_date") for r in rows.values()}
                year_sets = {
                    tuple(
                        sorted(str(a.get("period") or "")[:4] for a in (r.get("annual_roes") or []))
                    )
                    for r in rows.values()
                }
                if len(date_sets) > 1 or len(year_sets) > 1:
                    rows_controls.append(
                        ft.Text(
                            "公司估值日或年报覆盖不同，请分别核对；差值不代表同口径优劣。",
                            color=ft.Colors.AMBER_900,
                        )
                    )
                top = disc_data["results"].get("top")
                top = top if isinstance(top, list) else [r.get("code") for r in ranking[:3]]
                featured = list(dict.fromkeys([*top, anchor]))
                if not ranking:
                    rows_controls.append(
                        ft.Text("本次没有合格候选，请展开范围与排除原因；不代表全市场无机会。")
                    )
                if disc_data["results"].get("outside_watchlist_qualified_count") == 0:
                    rows_controls.append(ft.Text("本次无原关注池外合格对象，不扩充研究前三。"))
                rows_controls.extend(
                    fact_card(rows.get(c) or {"code": c, "exclusions": ["缺少事实明细"]}, disc_data)
                    for c in featured
                )
                rows_controls.append(section("展开依据、来源与排除原因", evidence(disc_data)))
                rows_controls.append(
                    section(
                        "展开完整比较",
                        [
                            fact_card(rows.get(r.get("code")) or {"code": r.get("code")}, disc_data)
                            for r in ranking
                            if r.get("code") not in featured
                        ],
                    )
                )
            else:
                rows_controls.append(ft.Text(disc_data.get("message") or "当前无可用完整榜。"))

            return ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Text("同业发现", size=18, weight=ft.FontWeight.BOLD),
                            submit_button,
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    anchor_field,
                    ft.Text(
                        "仅支持原关注参照；金融或行业未知不适用。",
                        size=12,
                    ),
                    ft.Text(
                        "研究次序不是买入建议；请比较实际指标并核查业务、资产与盈利质量。", size=13
                    ),
                    *(
                        [ft.Text("正在查看指定任务的结果/诊断，并非后来最新资料。", size=12)]
                        if job_id
                        else []
                    ),
                    ft.Text(anchor_error, color=ft.Colors.RED_700)
                    if anchor_error
                    else ft.Container(),
                    ft.Text(
                        f"参照标的：{anchor or '暂无'} (估值日: {disc_data.get('valuation_date', '无')})",
                        size=13,
                    ),
                    ft.Text(
                        "提交将按当前原关注范围、已证明的上一交易日扫描；历史任务不变。",
                        size=12,
                    ),
                    update_feedback,
                    ft.Divider(height=16),
                    *(
                        [
                            ft.Container(
                                content=ft.Text(
                                    str(disc_data.get("error") or disc_data.get("warning")),
                                    color=ft.Colors.RED_700
                                    if disc_data.get("error")
                                    else ft.Colors.AMBER_900,
                                ),
                                bgcolor=ft.Colors.RED_50
                                if disc_data.get("error")
                                else ft.Colors.AMBER_50,
                                padding=10,
                            )
                        ]
                        if disc_data.get("error") or disc_data.get("warning")
                        else []
                    ),
                    ft.Column(controls=rows_controls, spacing=8),
                    ft.Button("查看最新资料（不联网更新）", on_click=refresh_result),
                ],
                spacing=8,
            )

        async def render_job(job_id: str, gen: int):
            actor: Actor | None = page_state.get("actor")
            if not actor or not actor.is_valid:
                return await render_login()
            try:
                job = await asyncio.to_thread(get_update_job, actor, job_id, STATE_DIR, APP_MODE)
            except ServiceError:
                return ft.Text("任务不存在或暂不可读取")
            if gen != page_state["generation"] or not actor.is_valid:
                return await render_login()

            status_text = ft.Text("")

            async def open_result(e):
                route = "/discover?" + urlencode({"anchor": job["anchor"], "job": job_id})
                page_state.setdefault("discover_boards", {}).pop(route, None)
                await navigate(route)

            async def retry(e):
                page_state["selected_anchor"] = job["anchor"]
                page_state["pending_peer_request"] = None
                await go_discover(e)

            result_button = ft.Button("查看本次结果/诊断", on_click=open_result)
            retry_button = ft.Button("重新扫描（先确认参照）", on_click=retry)

            def show_status(current: dict[str, Any]):
                active = current["status"] in ("queued", "running")
                result_button.visible = not active
                retry_button.visible = not active
                status_text.value = (
                    f"状态：{job_status_label(current)} · 数据日：{current['target_date']}"
                    + (" · 服务器已接收，可离开页面稍后回来。" if active else "")
                    + (f" · {current['error_summary']}" if current["error_summary"] else "")
                )

            show_status(job)

            async def poll_job():
                try:
                    while True:
                        await asyncio.sleep(3)
                        if (
                            gen != page_state["generation"]
                            or not actor.is_valid
                            or not page_state["connected"]
                        ):
                            return
                        latest = await asyncio.to_thread(
                            get_update_job, actor, job_id, STATE_DIR, APP_MODE
                        )
                        if (
                            gen != page_state["generation"]
                            or not actor.is_valid
                            or not page_state["connected"]
                        ):
                            return
                        show_status(latest)
                        page.update()
                        if latest["status"] not in ("queued", "running"):
                            return
                except (asyncio.CancelledError, AuthError):
                    return
                except (ServiceError, WorkspaceError):
                    if gen == page_state["generation"] and actor.is_valid:
                        status_text.value = "状态查询失败，请返回首页稍后重试"
                        page.update()

            if job["status"] in ("queued", "running"):
                page_state["job_poll_task"] = asyncio.create_task(poll_job())
            return ft.Column(
                controls=[
                    ft.Text("更新任务", size=18, weight=ft.FontWeight.BOLD),
                    ft.Text(f"参照公司：{job['anchor']}"),
                    status_text,
                    result_button,
                    retry_button,
                    ft.Text(
                        "重新扫描须再次点击查找同业，按届时清单与已证明日历确定范围和目标日；不会更改原任务。",
                        size=12,
                    ),
                    ft.Button("返回同业发现", on_click=retry),
                ]
            )

        async def render_company(code: str, gen: int):
            actor: Actor | None = page_state.get("actor")
            if not actor or not actor.is_valid:
                return await render_login()

            ctx = await asyncio.to_thread(
                get_company_context, actor, code, STATE_DIR, APP_MODE, include_personal_notes=True
            )
            if gen != page_state["generation"] or not actor.is_valid:
                return await render_login()

            page_state["current_company_code"] = code
            page_state["current_company_revision"] = ctx.get("revision", 0)
            persisted_baseline = {
                "watch_status": str(ctx.get("watch_status") or "observe"),
                "reason": str(ctx.get("reason") or ""),
                "next_check": str(ctx.get("next_check") or ""),
                "note_url": str(ctx.get("note_url") or ""),
            }
            page_state["current_form_baseline"] = persisted_baseline

            draft = page_state.setdefault("drafts", {}).get(code, {})
            conflict_detected = False
            draft_base_rev = None
            if draft:
                draft_base_rev = draft.get("_base_revision")
                if draft_base_rev is not None and draft_base_rev != ctx.get("revision", 0):
                    conflict_detected = True

            status_val = draft.get("watch_status") or ctx["watch_status"] or "observe"
            reason_val = draft.get("reason") if "reason" in draft else ctx["reason"]
            next_check_val = draft.get("next_check") if "next_check" in draft else ctx["next_check"]
            note_url_val = draft.get("note_url") if "note_url" in draft else (ctx["note_url"] or "")

            status_dd = ft.Dropdown(
                label="观察状态",
                options=[
                    ft.dropdown.Option("research", "研究中"),
                    ft.dropdown.Option("observe", "观察"),
                    ft.dropdown.Option("paused", "暂停"),
                ],
                value=status_val,
                width=150,
            )
            reason_field = ft.TextField(
                label="一句理由 (<=1000字)",
                value=reason_val,
                multiline=True,
                max_length=1000,
            )
            next_check_field = ft.TextField(
                label="下一步核查提示",
                value=next_check_val,
                max_length=1000,
            )
            note_url_field = ft.TextField(
                label="外部笔记链接 (https://)",
                value=note_url_val,
                max_length=2048,
            )

            def get_current_form() -> dict[str, str]:
                return {
                    "watch_status": str(status_dd.value or "observe"),
                    "reason": str(reason_field.value or ""),
                    "next_check": str(next_check_field.value or ""),
                    "note_url": str(note_url_field.value or ""),
                }

            page_state["current_form_getter"] = get_current_form

            def update_draft(e: Any = None) -> None:
                current = get_current_form()
                existing_draft = page_state.get("drafts", {}).get(code)
                existing_base = existing_draft.get("_base_revision") if existing_draft else None
                base_rev = (
                    existing_base
                    if existing_base is not None
                    else page_state.get("current_company_revision", ctx.get("revision", 0))
                )
                current_rev = page_state.get("current_company_revision", ctx.get("revision", 0))
                is_conflict = existing_base is not None and existing_base != current_rev
                is_modified = current != page_state.get("current_form_baseline")

                if is_modified or is_conflict:
                    page_state.setdefault("drafts", {})[code] = {
                        **current,
                        "_base_revision": base_rev,
                    }
                else:
                    page_state.setdefault("drafts", {}).pop(code, None)

            status_dd.on_select = update_draft
            reason_field.on_change = update_draft
            next_check_field.on_change = update_draft
            note_url_field.on_change = update_draft

            feedback_text = ft.Text("", size=13)

            async def on_discard_draft(e: Any = None):
                page_state.setdefault("drafts", {}).pop(code, None)
                await render_current_view()
                page.update()

            async def on_save(e):
                if gen != page_state["generation"] or not page_state["connected"]:
                    return
                if not actor.is_valid:
                    feedback_text.value = "授权已失效，请重新登录"
                    feedback_text.color = ft.Colors.RED_700
                    page.update()
                    return

                if conflict_detected:
                    feedback_text.value = f"版本冲突：当前草稿基于版本 {draft_base_rev}，数据库已更新为版本 {ctx['revision']}，已禁止直接保存"
                    feedback_text.color = ft.Colors.RED_700
                    page.update()
                    return

                try:
                    fields = {
                        "status": status_dd.value,
                        "reason": reason_field.value.strip(),
                        "next_check": next_check_field.value.strip(),
                        "note_url": note_url_field.value.strip() or None,
                    }
                    updated = await asyncio.to_thread(
                        save_watch,
                        actor=actor,
                        code=code,
                        source_run_id=ctx.get("displayed_run_id") or "run_initial",
                        fields=fields,
                        expected_revision=ctx["revision"],
                        state_dir=STATE_DIR,
                        mode=APP_MODE,
                    )
                    if (
                        gen != page_state["generation"]
                        or not actor.is_valid
                        or not page_state["connected"]
                    ):
                        return
                    ctx["revision"] = updated["revision"]
                    ctx["is_watched"] = True
                    page_state["current_company_revision"] = updated["revision"]
                    page_state["current_form_baseline"] = get_current_form()
                    page_state.setdefault("drafts", {}).pop(code, None)
                    feedback_text.value = f"保存成功 (版本: {updated['revision']})"
                    feedback_text.color = ft.Colors.GREEN_700
                except (ServiceError, WorkspaceError) as exc:
                    feedback_text.value = f"保存失败: {exc}"
                    feedback_text.color = ft.Colors.RED_700
                except Exception:
                    feedback_text.value = "保存失败: 请稍后重试"
                    feedback_text.color = ft.Colors.RED_700
                page.update()

            page_state["save_current_form"] = on_save
            comparison_pending = bool(
                ctx.get("ack_run_id") and ctx["ack_run_id"] != ctx.get("displayed_run_id")
            )

            async def on_ack(e):
                if gen != page_state["generation"] or not page_state["connected"]:
                    return
                if comparison_pending:
                    feedback_text.value = (
                        "已阅→当前逐项对照尚未接入，暂不能确认这次变化；仍可保存判断。"
                    )
                    page.update()
                    return
                if not actor.is_valid:
                    feedback_text.value = "授权已失效，请重新登录"
                    feedback_text.color = ft.Colors.RED_700
                    page.update()
                    return

                if conflict_detected:
                    feedback_text.value = f"版本冲突：数据库已更新为版本 {ctx['revision']}，请先刷新或放弃草稿载入最新"
                    feedback_text.color = ft.Colors.RED_700
                    page.update()
                    return
                if ctx.get("has_latest_attempt_gap"):
                    feedback_text.value = (
                        "最新快照异常或本次尝试存在数据缺口，不可将旧资料标记为本次已阅"
                    )
                    feedback_text.color = ft.Colors.RED_700
                    page.update()
                    return

                try:
                    disp_run = ctx.get("displayed_run_id")
                    if not disp_run:
                        feedback_text.value = "暂无可确认的运行资料"
                        feedback_text.color = ft.Colors.RED_700
                    else:
                        updated = await asyncio.to_thread(
                            mark_seen,
                            actor=actor,
                            code=code,
                            displayed_run_id=disp_run,
                            expected_revision=ctx["revision"],
                            state_dir=STATE_DIR,
                            mode=APP_MODE,
                        )
                        if (
                            gen != page_state["generation"]
                            or not actor.is_valid
                            or not page_state["connected"]
                        ):
                            return
                        ctx["revision"] = updated["revision"]
                        page_state["current_company_revision"] = updated["revision"]
                        feedback_text.value = f"已标记已阅 (版本: {updated['revision']})"
                        feedback_text.color = ft.Colors.GREEN_700
                except (ServiceError, WorkspaceError) as exc:
                    feedback_text.value = f"标记已阅失败: {exc}"
                    feedback_text.color = ft.Colors.RED_700
                except Exception:
                    feedback_text.value = "标记已阅失败: 请稍后重试"
                    feedback_text.color = ft.Colors.RED_700
                page.update()

            usable = ctx.get("usable_fact") or {}
            annual_roes = usable.get("annual_roes") or []
            roe_rows = [
                ft.Text(
                    f"{r.get('year')}年: ROE {r.get('roe')}% (公告日: {r.get('ann_date')})", size=13
                )
                for r in annual_roes
            ]

            save_btn = ft.Button("保存判断", on_click=on_save, disabled=conflict_detected)
            ack_btn = ft.Button(
                "标记本次变化已阅",
                on_click=on_ack,
                disabled=comparison_pending
                or conflict_detected
                or ctx.get("has_latest_attempt_gap", False),
            )

            form_controls: list[ft.Control] = [
                ft.Text("个人研究记录", size=15, weight=ft.FontWeight.BOLD),
            ]
            if conflict_detected:
                conflict_banner = ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Text(
                                f"⚠️ 版本冲突提示：此记录已被外部更新（最新版本: {ctx['revision']}，草稿基于版本: {draft_base_rev}）。",
                                weight=ft.FontWeight.BOLD,
                                color=ft.Colors.RED_900,
                            ),
                            ft.Text(
                                "为防止覆盖最新数据，已禁止直接保存。您可以核对/复制草稿内容，然后点击下方按钮载入最新版本：",
                                size=13,
                                color=ft.Colors.RED_800,
                            ),
                            ft.Button("放弃草稿并载入最新版本", on_click=on_discard_draft),
                        ],
                        spacing=6,
                    ),
                    bgcolor=ft.Colors.RED_50,
                    border=ft.Border.all(1, ft.Colors.RED_400),
                    border_radius=6,
                    padding=12,
                )
                form_controls.append(conflict_banner)

            form_controls.extend(
                [
                    status_dd,
                    reason_field,
                    next_check_field,
                    note_url_field,
                    ft.Row(
                        controls=[
                            save_btn,
                            ack_btn,
                        ],
                        spacing=8,
                    ),
                    feedback_text,
                    *(
                        [
                            ft.Text(
                                "已阅→当前逐项对照尚未接入，暂不能确认这次变化；仍可保存判断。",
                                color=ft.Colors.AMBER_900,
                            )
                        ]
                        if comparison_pending
                        else []
                    ),
                ]
            )

            return ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.IconButton(ft.Icons.ARROW_BACK, tooltip="返回", on_click=go_back),
                            ft.Text(
                                f"{ctx['name']} ({code})",
                                size=18,
                                weight=ft.FontWeight.BOLD,
                                expand=True,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.START,
                    ),
                    *(
                        [
                            ft.Container(
                                content=ft.Text(
                                    (
                                        "⚠️ 注意：最新已核验运行快照文件损坏或无法读取；当前展示的是上一次可用事实"
                                        if ctx.get("latest_attempt_error")
                                        == "最新快照文件损坏或无法读取"
                                        else f"⚠️ {ctx.get('latest_attempt_error') or '本次尝试失败/数据缺口'}（{ctx.get('latest_attempt_date') or '日期未知'}），"
                                        f"当前仅展示旧可用事实（{ctx.get('usable_valuation_date') or '无'}），不可标记本次变化已阅。"
                                    ),
                                    weight=ft.FontWeight.BOLD,
                                    color=ft.Colors.RED_900,
                                ),
                                bgcolor=ft.Colors.RED_50,
                                padding=12,
                            )
                        ]
                        if ctx.get("has_latest_attempt_gap")
                        else []
                    ),
                    ft.Card(
                        content=ft.Container(
                            padding=12,
                            content=ft.Column(
                                controls=[
                                    ft.Text("公司筛选事实", size=15, weight=ft.FontWeight.BOLD),
                                    ft.Text(
                                        f"PB: {usable.get('pb', '无')} (估值日: {ctx.get('usable_valuation_date', '无')})",
                                        size=14,
                                    ),
                                    ft.Text(
                                        f"ROE三年均值: {usable.get('roe_mean', '无')}%", size=14
                                    ),
                                    *roe_rows,
                                    ft.Text(
                                        f"核查状态: {usable.get('financial_status', '无')} ({usable.get('financial_checked_at', '')})",
                                        size=12,
                                        color=ft.Colors.GREY_700,
                                    ),
                                ],
                                spacing=4,
                            ),
                        )
                    ),
                    ft.Card(
                        semantic_container=False,
                        content=ft.Container(
                            padding=12,
                            content=ft.Column(
                                controls=form_controls,
                                spacing=8,
                            ),
                        ),
                    ),
                ],
                spacing=12,
            )

        async def render_settings():
            actor: Actor | None = page_state.get("actor")
            return ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.IconButton(ft.Icons.ARROW_BACK, tooltip="返回", on_click=go_home),
                            ft.Text("系统设置", size=18, weight=ft.FontWeight.BOLD),
                        ]
                    ),
                    ft.Text(f"当前模式: {APP_MODE}", size=14),
                    ft.Text(f"数据目录: {STATE_DIR}", size=14),
                    ft.Text(f"用户身份: {actor.user_id if actor else '未登录'}", size=14),
                    ft.Button("退出登录", icon=ft.Icons.LOGOUT, on_click=on_logout)
                    if actor
                    else ft.Container(),
                ],
                spacing=12,
            )

        async def render_current_view():
            gen = page_state["generation"]
            actor: Actor | None = page_state.get("actor")
            if (not actor or not actor.is_valid) and page_state["route"] != "/login":
                page_state["route"] = "/login"
            route = urlparse(page_state["route"]).path
            nav_bar.selected_index = (
                1
                if route == "/discover"
                or route.startswith("/jobs/")
                or (
                    route.startswith("/company/")
                    and page_state["company_return"].startswith("/discover")
                )
                else 0
            )
            if route == "/login":
                content = await render_login()
            elif route == "/":
                content = await render_home(gen)
            elif route == "/discover":
                content = await render_discover(gen)
            elif route.startswith("/jobs/"):
                content = await render_job(route.rsplit("/", 1)[-1], gen)
            elif route.startswith("/company/"):
                c = route.split("/")[-1]
                content = await render_company(c, gen)
            elif route == "/settings":
                content = await render_settings()
            else:
                content = await render_login()

            if gen == page_state["generation"]:
                if route != "/login" and (not actor or not actor.is_valid):
                    page_state["route"] = "/login"
                    content = await render_login()
                content_container.content = content

        async def on_nav_change(e):
            if e.control.selected_index == 0:
                await navigate("/")
            elif e.control.selected_index == 1:
                await go_discover(e)

        nav_bar = ft.NavigationBar(
            destinations=[
                ft.NavigationBarDestination(icon=ft.Icons.LIST, label="我的研究"),
                ft.NavigationBarDestination(icon=ft.Icons.SEARCH, label="同业发现"),
            ],
            selected_index=0,
            on_change=on_nav_change,
        )

        header = ft.Container(
            padding=ft.Padding.symmetric(horizontal=8, vertical=4),
            content=ft.Row(
                controls=[
                    ft.Text("投资研究工作台", size=16, weight=ft.FontWeight.BOLD),
                    ft.Container(
                        content=ft.Text(
                            "DEMO" if APP_MODE == "demo" else "PROD", size=11, color=ft.Colors.WHITE
                        ),
                        bgcolor=ft.Colors.ORANGE_800 if APP_MODE == "demo" else ft.Colors.GREEN_700,
                        padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                        border_radius=4,
                    ),
                    ft.IconButton(ft.Icons.SETTINGS, tooltip="设置", on_click=go_settings),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
        )

        main_column = ft.Container(
            width=640,
            content=ft.Column(
                controls=[
                    header,
                    content_container,
                ],
                expand=True,
            ),
        )

        page.navigation_bar = nav_bar
        page.add(
            ft.ResponsiveRow(
                controls=[
                    ft.Column(
                        controls=[main_column],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                ]
            )
        )
        await render_current_view()
        page.update()

        async def session_watchdog():
            try:
                while True:
                    await asyncio.sleep(5)
                    actor: Actor | None = page_state.get("actor")
                    if actor and not actor.is_valid:
                        page_state["generation"] += 1
                        page_state.setdefault("drafts", {}).clear()
                        page_state["current_form_getter"] = None
                        page_state["current_company_code"] = None
                        page_state["current_form_baseline"] = None
                        actor.revoke()
                        page_state["actor"] = None
                        if page_state["route"] != "/login":
                            await navigate("/login")
            except asyncio.CancelledError:
                pass

        watchdog_task: asyncio.Task[None] | None = asyncio.create_task(session_watchdog())

        async def on_disconnect(e):
            nonlocal watchdog_task
            page_state["connected"] = False
            poll = page_state.get("job_poll_task")
            if poll is not None:
                poll.cancel()
                page_state["job_poll_task"] = None
            page_state["generation"] += 1
            if callable(page_state.get("current_form_getter")) and page_state.get(
                "current_company_code"
            ):
                active_c = page_state["current_company_code"]
                baseline = page_state.get("current_form_baseline")
                existing_draft = page_state.get("drafts", {}).get(active_c)
                existing_base = existing_draft.get("_base_revision") if existing_draft else None
                current_rev = page_state.get("current_company_revision", 0)
                base_rev = existing_base if existing_base is not None else current_rev
                is_conflict = existing_base is not None and existing_base != current_rev
                try:
                    curr = page_state["current_form_getter"]()
                    is_modified = baseline is not None and curr != baseline
                    if is_modified or is_conflict:
                        page_state.setdefault("drafts", {})[active_c] = {
                            **curr,
                            "_base_revision": base_rev,
                        }
                    else:
                        page_state.setdefault("drafts", {}).pop(active_c, None)
                except Exception:
                    pass
            if watchdog_task is not None:
                watchdog_task.cancel()
                watchdog_task = None

        page.on_disconnect = on_disconnect

        async def on_connect(e):
            nonlocal watchdog_task
            page_state["connected"] = True
            if watchdog_task is None or watchdog_task.done():
                watchdog_task = asyncio.create_task(session_watchdog())
            actor: Actor | None = page_state.get("actor")
            if actor and actor.is_valid:
                await render_current_view()
                page.update()
            else:
                page_state.setdefault("drafts", {}).clear()
                page_state["current_form_getter"] = None
                page_state["current_company_code"] = None
                page_state["current_company_revision"] = None
                page_state["current_form_baseline"] = None
                if APP_MODE == "demo":
                    page_state["actor"] = create_demo_actor()
                    await render_current_view()
                    page.update()
                else:
                    if actor:
                        actor.revoke()
                    page_state["actor"] = None
                    if page_state["route"] != "/login":
                        await navigate("/login")
                    else:
                        await render_current_view()
                        page.update()

        page.on_connect = on_connect

        async def on_close(e):
            nonlocal watchdog_task
            page_state["connected"] = False
            poll = page_state.get("job_poll_task")
            if poll is not None:
                poll.cancel()
                page_state["job_poll_task"] = None
            page_state["generation"] += 1
            page_state.setdefault("drafts", {}).clear()
            page_state["current_form_getter"] = None
            page_state["current_company_code"] = None
            page_state["current_company_revision"] = None
            page_state["current_form_baseline"] = None
            actor: Actor | None = page_state.get("actor")
            if actor:
                actor.revoke()
            page_state["actor"] = None
            if watchdog_task is not None:
                watchdog_task.cancel()
                watchdog_task = None

        page.on_close = on_close

    return main


class SecurityMiddleware:
    """ASGI middleware for host validation, origin checking, and upload prevention."""

    def __init__(self, app_instance: Any, is_demo: bool, public_base_url: str):
        self.app = app_instance
        self.is_demo = is_demo
        self.public_base_url = public_base_url
        parsed = urlparse(public_base_url)
        self.expected_scheme = (parsed.scheme or "").lower()
        self.expected_hostname = (parsed.hostname or "").lower()
        self.expected_port = parsed.port or (443 if self.expected_scheme == "https" else 80)

    def _parse_host_port(self, netloc: str) -> tuple[str, int | None] | None:
        """Parse host and optional port according to RFC 3986 / RFC 6454."""
        if not netloc:
            return None
        netloc = netloc.strip().lower()
        if netloc.startswith("["):
            end_bracket = netloc.find("]")
            if end_bracket == -1:
                return None
            hostname = netloc[1:end_bracket]
            rest = netloc[end_bracket + 1 :]
            if rest == "":
                return (hostname, None)
            if not rest.startswith(":"):
                return None
            port_str = rest[1:]
            if not port_str.isdigit():
                return None
            try:
                port = int(port_str)
                if not (1 <= port <= 65535):
                    return None
                return (hostname, port)
            except ValueError:
                return None
        else:
            parts = netloc.split(":")
            if len(parts) == 1:
                return (parts[0], None)
            elif len(parts) == 2:
                if not parts[1].isdigit():
                    return None
                try:
                    port = int(parts[1])
                    if not (1 <= port <= 65535):
                        return None
                    return (parts[0], port)
                except ValueError:
                    return None
            return None

    def _validate_host(self, host_header: str) -> bool:
        if not host_header:
            return False
        parsed = self._parse_host_port(host_header)
        if not parsed:
            return False
        hostname, port = parsed
        effective_port = (
            port if port is not None else (443 if self.expected_scheme == "https" else 80)
        )
        if self.is_demo:
            if hostname not in ("127.0.0.1", "localhost", "::1"):
                return False
            return effective_port == self.expected_port
        else:
            return hostname == self.expected_hostname and effective_port == self.expected_port

    def _validate_origin(self, origin_header: str) -> bool:
        if not origin_header:
            return False
        try:
            parsed = urlparse(origin_header.strip())
        except (ValueError, AttributeError):
            return False

        if parsed.path != "" or parsed.query or parsed.params or parsed.fragment:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False

        scheme = (parsed.scheme or "").lower()
        if scheme != self.expected_scheme:
            return False

        netloc = (parsed.netloc or "").lower()
        parsed_hp = self._parse_host_port(netloc)
        if not parsed_hp:
            return False
        hostname, port = parsed_hp
        origin_port = port if port is not None else (443 if scheme == "https" else 80)

        # RFC 6454 strict comparison against configured public_base_url
        return (
            scheme == self.expected_scheme
            and hostname == self.expected_hostname
            and origin_port == self.expected_port
        )

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        # Explicitly disable /upload
        if (path.startswith("/upload") or path == "/upload") and scope["type"] == "http":
            if callable(send):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 404,
                        "headers": [(b"content-type", b"text/plain")],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"Not Found",
                    }
                )
            return

        headers = dict(scope.get("headers", []))
        host_header = headers.get(b"host", b"").decode("utf-8")

        # Validate Host for both http and websocket
        if not self._validate_host(host_header):
            if scope["type"] == "http":
                if callable(send):
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 403,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                        }
                    )
                    await send(
                        {
                            "type": "http.response.body",
                            "body": b"Forbidden: Invalid Host header",
                        }
                    )
                return
            elif scope["type"] == "websocket":
                if callable(send):
                    await send({"type": "websocket.close", "code": 4403})
                return

        # Check WebSocket Origin
        if scope["type"] == "websocket":
            origin_header = headers.get(b"origin", b"").decode("utf-8")
            if not self._validate_origin(origin_header):
                if callable(send):
                    await send({"type": "websocket.close", "code": 4403})
                return

        return await self.app(scope, receive, send)


def get_asgi_app():
    try:
        import flet.fastapi as flet_fastapi

        inner_app = flet_fastapi.app(build_app(), assets_dir=str(Path(__file__).parent / "assets"))
    except ImportError:

        async def inner_app(scope, receive, send):
            if scope["type"] == "http" and callable(send):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 503,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"Flet is not installed. Please run `make setup` first.",
                    }
                )

    return SecurityMiddleware(
        inner_app, is_demo=(APP_MODE == "demo"), public_base_url=PUBLIC_BASE_URL
    )


asgi_app = get_asgi_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:asgi_app", host=HOST, port=PORT, reload=False, access_log=False)

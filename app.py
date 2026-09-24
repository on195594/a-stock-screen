"""Flet Web application for personal research workbench."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from auth import (
    Actor,
    AuthError,
    check_production_auth_config,
    create_demo_actor,
    create_owner_actor,
)
from services import (
    ServiceError,
    get_company_context,
    get_home,
    get_peer_discover,
    mark_seen,
    save_watch,
)
from workspace import WorkspaceError

RAW_MODE = os.getenv("APP_MODE", "demo")
if RAW_MODE not in ("demo", "production"):
    raise ValueError(f"Invalid APP_MODE: '{RAW_MODE}'. Must be 'demo' or 'production'.")
APP_MODE: Literal["demo", "production"] = "production" if RAW_MODE == "production" else "demo"
STATE_DIR = Path(os.getenv("STATE_DIR", ".local/demo")).expanduser().resolve()
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
        page.padding = 16
        page.scroll = ft.ScrollMode.AUTO

        # State per page session
        page_state: dict[str, Any] = {
            "actor": create_demo_actor() if APP_MODE == "demo" else None,
            "route": "/" if APP_MODE == "demo" else "/login",
            "selected_anchor": "600001.SH",
            "active_code": "600001.SH",
            "generation": 0,
            "drafts": {},
            "current_form_getter": None,
            "current_company_code": None,
            "current_form_baseline": None,
        }

        # Explicit logout in settings handles actor revocation
        content_container = ft.Container(expand=True)
        login_msg = ft.Text("", size=13, color=ft.Colors.RED_700)

        async def navigate(route: str):
            page_state["generation"] += 1
            page_state["route"] = route
            page_state["current_form_getter"] = None
            page_state["current_company_code"] = None
            page_state["current_form_baseline"] = None
            await render_current_view()
            page.update()

        async def go_home(e):
            await navigate("/")

        async def go_settings(e):
            await navigate("/settings")

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
                    page_state["active_code"] = c
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
                                    tooltip="后台更新任务将在S2提供，S1为离线演示",
                                ),
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        ),
                    ),
                    ft.Divider(height=16),
                    ft.Text(
                        f"关注清单 ({data['total_watch_count']})",
                        size=15,
                        weight=ft.FontWeight.W_600,
                    ),
                    *items_controls,
                ],
                spacing=8,
            )

        async def render_discover(gen: int):
            actor: Actor | None = page_state.get("actor")
            if not actor or not actor.is_valid:
                return await render_login()

            anchor = page_state["selected_anchor"]
            disc_data = await asyncio.to_thread(
                get_peer_discover, actor, anchor, STATE_DIR, APP_MODE
            )
            if gen != page_state["generation"] or not actor.is_valid:
                return await render_login()

            rows_controls: list[ft.Control] = []
            if disc_data.get("has_run"):
                verified_rows = disc_data.get("rows", [])
                code_to_name = {
                    str(r.get("code") or r.get("ts_code")): r.get("name")
                    for r in verified_rows
                    if isinstance(r, dict)
                }
                ranking_raw = disc_data.get("results", {}).get("ranking", [])
                ranking = (
                    [r for r in ranking_raw if isinstance(r, dict)]
                    if isinstance(ranking_raw, list)
                    else []
                )
                for item in ranking:
                    c = item.get("code")
                    name = code_to_name.get(str(c)) or item.get("name") or c
                    pos = item.get("position")
                    is_anchor = c == anchor

                    async def open_comp(e, target_code=c):
                        page_state["active_code"] = target_code
                        await navigate(f"/company/{target_code}")

                    rows_controls.append(
                        ft.Container(
                            padding=8,
                            bgcolor=ft.Colors.AMBER_50 if is_anchor else ft.Colors.WHITE,
                            border=ft.Border.all(1, ft.Colors.GREY_300),
                            border_radius=4,
                            content=ft.Row(
                                controls=[
                                    ft.Text(f"#{pos}", size=14, weight=ft.FontWeight.BOLD),
                                    ft.Column(
                                        [
                                            ft.Text(
                                                f"{name} ({c})", size=14, weight=ft.FontWeight.W_600
                                            ),
                                            ft.Text(
                                                f"PB rank: {item.get('pb_rank')} | ROE rank: {item.get('roe_rank')} | 综合: {item.get('research_order')}",
                                                size=12,
                                                color=ft.Colors.GREY_700,
                                            ),
                                        ]
                                    ),
                                    ft.Button("查看", on_click=open_comp),
                                ],
                                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            ),
                        )
                    )

            return ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Text("同业发现", size=18, weight=ft.FontWeight.BOLD),
                            ft.Button(
                                "查找同业", disabled=True, tooltip="查找同业后台任务将在S2提供"
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Text(
                        f"参照标的：{anchor} (估值日: {disc_data.get('valuation_date', '无')})",
                        size=13,
                    ),
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
                ],
                spacing=8,
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

            async def on_ack(e):
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
                disabled=conflict_detected or ctx.get("has_latest_attempt_gap", False),
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
                ]
            )

            return ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.IconButton(ft.Icons.ARROW_BACK, tooltip="返回", on_click=go_home),
                            ft.Text(f"{ctx['name']} ({code})", size=18, weight=ft.FontWeight.BOLD),
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
                        content=ft.Container(
                            padding=12,
                            content=ft.Column(
                                controls=form_controls,
                                spacing=8,
                            ),
                        )
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
            route = page_state["route"]
            if route == "/login":
                content = await render_login()
            elif route == "/":
                content = await render_home(gen)
            elif route == "/discover":
                content = await render_discover(gen)
            elif route.startswith("/company/"):
                c = route.split("/")[-1]
                content = await render_company(c, gen)
            elif route == "/settings":
                content = await render_settings()
            else:
                content = await render_login()

            if gen == page_state["generation"]:
                content_container.content = content

        async def on_nav_change(e):
            if e.control.selected_index == 0:
                await navigate("/")
            elif e.control.selected_index == 1:
                await navigate("/discover")

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

        inner_app = flet_fastapi.app(build_app())
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

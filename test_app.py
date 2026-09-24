"""Tests for ASGI application security middleware and lifespans."""

import asyncio
import inspect
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import flet as ft
import pytest

import app
from app import SecurityMiddleware
from auth import AuthError, check_production_auth_config
from workspace import connect_workspace, get_watch_item, import_snapshot, initialize, register_run


def test_asgi_blocks_upload():
    async def _test():
        async def dummy_app(scope, receive, send):
            pass

        middleware = SecurityMiddleware(
            dummy_app, is_demo=True, public_base_url="http://127.0.0.1:8550"
        )

        scope = {
            "type": "http",
            "path": "/upload/malicious_file",
            "headers": [(b"host", b"127.0.0.1:8550")],
        }
        sent = []

        async def mock_receive():
            return {"type": "http.request"}

        async def mock_send(msg):
            sent.append(msg)

        await middleware(scope, mock_receive, mock_send)
        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == 404

    asyncio.run(_test())


def test_asgi_blocks_non_loopback_in_demo():
    async def _test():
        async def dummy_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})

        middleware = SecurityMiddleware(
            dummy_app, is_demo=True, public_base_url="http://127.0.0.1:8550"
        )

        scope = {
            "type": "http",
            "path": "/",
            "headers": [(b"host", b"external.domain.com")],
        }
        sent = []

        async def mock_receive():
            return {"type": "http.request"}

        async def mock_send(msg):
            sent.append(msg)

        await middleware(scope, mock_receive, mock_send)
        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == 403

    asyncio.run(_test())


def test_asgi_allows_loopback_in_demo():
    async def _test():
        called = False

        async def dummy_app(scope, receive, send):
            nonlocal called
            called = True
            await send({"type": "http.response.start", "status": 200})

        middleware = SecurityMiddleware(
            dummy_app, is_demo=True, public_base_url="http://127.0.0.1:8550"
        )

        scope = {
            "type": "http",
            "path": "/",
            "headers": [(b"host", b"127.0.0.1:8550")],
        }
        sent = []

        async def mock_receive():
            return {"type": "http.request"}

        async def mock_send(msg):
            sent.append(msg)

        await middleware(scope, mock_receive, mock_send)
        assert called is True
        assert sent[0]["status"] == 200

    asyncio.run(_test())


def test_asgi_strict_host_and_origin_probes():
    async def dummy_app(scope, receive, send):
        pass

    s = SecurityMiddleware(dummy_app, is_demo=False, public_base_url="https://stocks.example.com")

    # Host validation tests
    assert s._validate_host("stocks.example.com") is True
    assert s._validate_host("stocks.example.com:443") is True
    assert s._validate_host("stocks.example.com:8080") is False
    assert s._validate_host("stocks.example.com:bad") is False
    assert s._validate_host("stocks.example.com:443:extra") is False
    assert s._validate_host("stocks.example.com:65536") is False
    assert s._validate_host("evil.example.com") is False

    # Origin validation tests
    assert s._validate_origin("https://stocks.example.com") is True
    assert s._validate_origin("https://stocks.example.com:443") is True
    assert s._validate_origin("https://stocks.example.com/") is False
    assert s._validate_origin("https://stocks.example.com/other") is False
    assert s._validate_origin("https://stocks.example.com?x=1") is False
    assert s._validate_origin("https://stocks.example.com#fragment") is False
    assert s._validate_origin("https://user:pass@stocks.example.com") is False
    assert s._validate_origin("https://stocks.example.com:8080") is False
    assert s._validate_origin("https://stocks.example.com:bad") is False
    assert s._validate_origin("https://stocks.example.com:65536") is False


def test_asgi_demo_origin_and_port_checks():
    async def dummy_app(scope, receive, send):
        pass

    s = SecurityMiddleware(dummy_app, is_demo=True, public_base_url="http://127.0.0.1:8550")

    # Correct origin matches configured base URL strictly under RFC 6454
    assert s._validate_origin("http://127.0.0.1:8550") is True
    assert s._validate_origin("http://localhost:8550") is False

    # Different port rejected in demo
    assert s._validate_origin("http://127.0.0.1:9999") is False

    # Invalid port strings and out-of-range ports rejected without exception
    assert s._validate_origin("http://127.0.0.1:bad") is False
    assert s._validate_origin("http://127.0.0.1:65536") is False
    assert s._validate_host("127.0.0.1:bad") is False
    assert s._validate_host("127.0.0.1:65536") is False
    assert s._validate_host("127.0.0.1:8550") is True


def test_asgi_websocket_origin_check():
    async def _test():
        ws_called = False

        async def dummy_app(scope, receive, send):
            nonlocal ws_called
            ws_called = True

        middleware = SecurityMiddleware(
            dummy_app, is_demo=True, public_base_url="http://127.0.0.1:8550"
        )

        # 1. Null/empty origin rejected with websocket.close
        sent_null = []
        scope_null = {
            "type": "websocket",
            "path": "/ws",
            "headers": [(b"host", b"127.0.0.1:8550")],
        }

        async def mock_send_null(msg):
            sent_null.append(msg)

        await middleware(scope_null, None, mock_send_null)
        assert ws_called is False
        assert sent_null[0]["type"] == "websocket.close"

        # 2. Host bypass via suffix (e.g. 127.0.0.1.evil.com) rejected
        sent_evil = []
        scope_evil = {
            "type": "websocket",
            "path": "/ws",
            "headers": [
                (b"host", b"127.0.0.1:8550"),
                (b"origin", b"http://127.0.0.1.evil.com:8550"),
            ],
        }

        async def mock_send_evil(msg):
            sent_evil.append(msg)

        await middleware(scope_evil, None, mock_send_evil)
        assert ws_called is False
        assert sent_evil[0]["type"] == "websocket.close"

        # 3. Loopback origin accepted
        scope_loopback = {
            "type": "websocket",
            "path": "/ws",
            "headers": [
                (b"host", b"127.0.0.1:8550"),
                (b"origin", b"http://127.0.0.1:8550"),
            ],
        }
        await middleware(scope_loopback, None, None)
        assert ws_called is True

    asyncio.run(_test())


def test_production_auth_checks():
    # Missing client id
    with pytest.raises(AuthError, match="GITHUB_CLIENT_ID"):
        check_production_auth_config("", "sec", "123")

    # Missing client secret
    with pytest.raises(AuthError, match="GITHUB_CLIENT_SECRET"):
        check_production_auth_config("client", "", "123")

    # Missing allowed user ID
    with pytest.raises(AuthError, match="GITHUB_ALLOWED_USER_ID"):
        check_production_auth_config("client", "sec", "")

    # Non-numeric user ID
    with pytest.raises(AuthError, match="numeric"):
        check_production_auth_config("client", "sec", "admin_name")

    # Valid config passes
    check_production_auth_config("client", "sec", "12345678")


def test_demo_origin_scheme_mismatch():
    async def dummy_app(scope, receive, send):
        pass

    # 1. Base URL configured with 127.0.0.1
    s_127 = SecurityMiddleware(dummy_app, is_demo=True, public_base_url="http://127.0.0.1:8550")
    assert s_127._validate_origin("http://127.0.0.1:8550") is True
    # Different schemes or hosts are different origins under RFC 6454
    assert s_127._validate_origin("https://127.0.0.1:8550") is False
    assert s_127._validate_origin("http://localhost:8550") is False
    assert s_127._validate_origin("http://[::1]:8550") is False

    # Host header allows loopback variants in demo
    assert s_127._validate_host("127.0.0.1:8550") is True
    assert s_127._validate_host("[::1]:8550") is True
    assert s_127._validate_host("localhost:8550") is True
    assert s_127._validate_host("evil.com:8550") is False

    # 2. Base URL configured with IPv6 [::1]
    s_v6 = SecurityMiddleware(dummy_app, is_demo=True, public_base_url="http://[::1]:8550")
    assert s_v6._validate_origin("http://[::1]:8550") is True
    assert s_v6._validate_origin("http://127.0.0.1:8550") is False

    # 3. Base URL configured with localhost
    s_lh = SecurityMiddleware(dummy_app, is_demo=True, public_base_url="http://localhost:8550")
    assert s_lh._validate_origin("http://localhost:8550") is True
    assert s_lh._validate_origin("http://127.0.0.1:8550") is False


class AppMockPage:
    def __init__(self, auth=None):
        self.title = ""
        self.theme_mode = None
        self.padding = None
        self.scroll = None
        self.on_login = None
        self.on_disconnect = None
        self.on_connect = None
        self.on_close = None
        self.auth = auth
        self.controls = []
        self.navigation_bar = None
        self.updated_count = 0
        self.logged_out = False

    def add(self, *controls):
        self.controls.extend(controls)
        self.update()  # Flet Page.add pushes one update.

    def update(self):
        self.updated_count += 1

    def logout(self):
        self.logged_out = True


def test_home_cards_show_each_company_date(tmp_path, monkeypatch):
    initialize(tmp_path, "demo", journal_mode="DELETE")
    monkeypatch.setattr(app, "APP_MODE", "demo")
    monkeypatch.setattr(app, "STATE_DIR", tmp_path)
    monkeypatch.setattr(
        app,
        "get_home",
        lambda *args: {
            "valuation_date": "各公司数据日不同",
            "needs_review_count": 0,
            "total_watch_count": 2,
            "watch_items": [
                {
                    "code": code,
                    "name": code,
                    "status": "observe",
                    "reason": "测试",
                    "has_change": False,
                    "change_summary": "",
                    "valuation_date": day,
                }
                for code, day in (("600001.SH", "2026-09-20"), ("600002.SH", "2026-09-21"))
            ],
        },
    )

    async def _test():
        page = AppMockPage()
        await app.build_app()(page)
        assert page.updated_count == 2  # Shell first, then data after async load.
        home = page.controls[0].controls[0].controls[0].content.controls[1].content
        assert "各公司数据日不同" in home.controls[0].content.controls[0].controls[1].value
        cards = [c for c in home.controls if isinstance(c, ft.Card)]
        assert len(cards) == 2
        for card, day in zip(cards, ("2026-09-20", "2026-09-21")):
            label = card.content.content.controls[2].controls[0].value
            assert f"数据日：{day} | 理由：测试" == label

    asyncio.run(_test())


def test_home_shell_renders_before_slow_data_read(tmp_path, monkeypatch):
    initialize(tmp_path, "demo", journal_mode="DELETE")
    monkeypatch.setattr(app, "APP_MODE", "demo")
    monkeypatch.setattr(app, "STATE_DIR", tmp_path)
    started = threading.Event()
    release = threading.Event()

    def slow_home(*args):
        started.set()
        assert release.wait(timeout=5)
        return {
            "valuation_date": "暂无",
            "needs_review_count": 0,
            "total_watch_count": 0,
            "watch_items": [],
        }

    monkeypatch.setattr(app, "get_home", slow_home)

    async def check():
        page = AppMockPage()
        task = asyncio.create_task(app.build_app()(page))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            assert page.updated_count == 1
            assert page.navigation_bar is not None
            assert page.controls  # Header is already pushed while SQLite read is pending.
        finally:
            release.set()
        await asyncio.wait_for(task, timeout=5)
        assert page.updated_count == 2

    asyncio.run(check())


def test_real_oauth_login_callback_flow(tmp_path, monkeypatch):
    initialize(tmp_path, "production", journal_mode="DELETE")
    monkeypatch.setattr(app, "APP_MODE", "production")
    monkeypatch.setattr(app, "GITHUB_CLIENT_ID", "test_client_id")
    monkeypatch.setattr(app, "GITHUB_CLIENT_SECRET", "test_client_secret")
    monkeypatch.setattr(app, "GITHUB_ALLOWED_USER_ID", "12345678")
    monkeypatch.setattr(app, "STATE_DIR", tmp_path)

    async def _test():
        main = app.build_app()
        mock_auth = MagicMock()
        mock_auth.user = {"id": "12345678"}
        page = AppMockPage(auth=mock_auth)
        await main(page)
        assert callable(page.on_login)

        col = page.controls[0].controls[0].controls[0]
        content_container = col.content.controls[1]

        # 1. Successful login from login page renders private home view
        success_event = ft.LoginEvent(
            name="login", control=None, error=None, error_description=None
        )
        await page.on_login(success_event)
        assert content_container.content is not None
        assert any(
            isinstance(c, ft.Card) or "关注" in getattr(c, "value", "")
            for c in content_container.content.controls
        )

        # 2. Provider error arriving while viewing private page must clear private view
        provider_err_event = ft.LoginEvent(
            name="login",
            control=None,
            error="access_denied",
            error_description="The user cancelled the authorization request.",
        )
        await page.on_login(provider_err_event)
        login_view = content_container.content
        assert any(
            isinstance(ctrl, ft.Button) and ctrl.content == "使用 GitHub 登录"
            for ctrl in login_view.controls
        )
        login_msg = login_view.controls[3]
        assert "登录失败: The user cancelled" in login_msg.value

        # 3. Re-login successfully
        await page.on_login(success_event)
        assert any(
            isinstance(c, ft.Card) or "关注" in getattr(c, "value", "")
            for c in content_container.content.controls
        )

        # 4. Unauthorized user error arriving while viewing private page must clear private view
        mock_auth.user = {"id": "87654321"}
        unauth_event = ft.LoginEvent(name="login", control=None, error=None, error_description=None)
        await page.on_login(unauth_event)
        login_view = content_container.content
        assert any(
            isinstance(ctrl, ft.Button) and ctrl.content == "使用 GitHub 登录"
            for ctrl in login_view.controls
        )
        login_msg = login_view.controls[3]
        assert "登录被拒绝: unauthorized GitHub user ID" in login_msg.value

    asyncio.run(_test())


def test_real_disconnect_and_reconnect_lifecycle(tmp_path, monkeypatch):
    initialize(tmp_path, "production", journal_mode="DELETE")
    monkeypatch.setattr(app, "APP_MODE", "production")
    monkeypatch.setattr(app, "GITHUB_CLIENT_ID", "test_client_id")
    monkeypatch.setattr(app, "GITHUB_CLIENT_SECRET", "test_client_secret")
    monkeypatch.setattr(app, "GITHUB_ALLOWED_USER_ID", "12345678")
    monkeypatch.setattr(app, "STATE_DIR", tmp_path)

    from workspace import add_watch_item, connect_workspace, register_run

    init_snap = {
        "rows": [
            {
                "code": "600001.SH",
                "name": "测试标的甲",
                "facts_usable": True,
                "pb": 1.5,
                "valuation_date": "2026-09-20",
                "valuation_source": "tracker",
                "financial_source": "tracker",
                "risk_source": "tracker",
                "annual_roes": [
                    {"year": y, "roe": 14.0, "source": "tracker", "ann_date": f"{y + 1}0415"}
                    for y in (2023, 2024, 2025)
                ],
                "roe_mean": 14.0,
            }
        ],
        "results": {"ranking": [{"code": "600001.SH", "name": "测试标的甲", "position": 1}]},
    }
    snap_bytes = json.dumps(init_snap).encode("utf-8")
    snap_path = tmp_path / "snapshots" / "init.json"
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    snap_path.write_bytes(snap_bytes)

    conn = connect_workspace(tmp_path, "production")
    register_run(
        conn,
        run_id="run_init",
        kind="peer",
        anchor_code="600001.SH",
        rule_id="peer-screen-v1",
        captured_at="2026-09-20T16:00:00+08:00",
        valuation_date="2026-09-20",
        health="complete",
        snapshot_path="snapshots/init.json",
        snapshot_bytes=snap_bytes,
    )
    add_watch_item(conn, code="600001.SH", name="测试标的甲", added_run_id="run_init")
    conn.commit()
    conn.close()

    async def _test():
        main = app.build_app()
        mock_auth = MagicMock()
        mock_auth.user = {"id": "12345678"}
        page = AppMockPage(auth=mock_auth)
        await main(page)

        # Successful login
        success_event = ft.LoginEvent(
            name="login", control=None, error=None, error_description=None
        )
        await page.on_login(success_event)
        col = page.controls[0].controls[0].controls[0]
        content_container = col.content.controls[1]
        assert any(
            isinstance(c, ft.Card) or "关注" in getattr(c, "value", "")
            for c in content_container.content.controls
        )

        # Navigate to company details
        card = next(c for c in content_container.content.controls if isinstance(c, ft.Card))
        await card.content.on_click(None)

        # Find reason_field in personal research form card and type unsaved draft
        form_card = content_container.content.controls[2]
        reason_field = form_card.content.content.controls[2]
        assert "一句理由" in getattr(reason_field, "label", "")
        assert reason_field.value == ""
        reason_field.value = "未保存的草稿理由"
        reason_field.on_change(None)

        # 1. Brief disconnect and fast reconnect keeps authorization and preserves unsaved draft!
        await page.on_disconnect(None)
        await page.on_connect(None)
        reconnected_form_card = content_container.content.controls[2]
        reconnected_reason_field = reconnected_form_card.content.content.controls[2]
        assert reconnected_reason_field.value == "未保存的草稿理由"

        # Save the form and confirm draft is cleared from page_state
        save_btn = reconnected_form_card.content.content.controls[5].controls[0]
        await save_btn.on_click(None)
        feedback_text = reconnected_form_card.content.content.controls[6]
        assert "保存成功" in feedback_text.value

        # Strictly prove draft was cleared: update record directly in SQLite from another tab/process
        from workspace import get_watch_item, save_watch_item

        db_conn = connect_workspace(tmp_path, "production")
        current_item = get_watch_item(db_conn, "600001.SH")
        assert current_item is not None
        save_watch_item(
            db_conn,
            code="600001.SH",
            expected_revision=current_item["revision"],
            status="research",
            reason="由另一会话在数据库中更新的最新理由",
            next_check="新提示",
        )
        db_conn.commit()
        db_conn.close()

        # Reconnect: verifies fresh data is loaded from SQLite without resurrecting old in-memory values
        await page.on_disconnect(None)
        await page.on_connect(None)
        saved_form_card = content_container.content.controls[2]
        saved_reason_field = saved_form_card.content.content.controls[2]
        assert saved_reason_field.value == "由另一会话在数据库中更新的最新理由"

        # 2. When actor expires during disconnect, reconnect redirects to login view
        import auth

        orig_mono = auth.time.monotonic
        monkeypatch.setattr(auth.time, "monotonic", lambda: orig_mono() + 100000.0)
        await page.on_disconnect(None)
        await page.on_connect(None)
        login_view = content_container.content
        assert any(
            isinstance(ctrl, ft.Button) and ctrl.content == "使用 GitHub 登录"
            for ctrl in login_view.controls
        )

        # 3. Session close revokes actor
        monkeypatch.setattr(auth.time, "monotonic", orig_mono)
        await page.on_login(success_event)
        assert any(
            isinstance(c, ft.Card) or "关注" in getattr(c, "value", "")
            for c in content_container.content.controls
        )
        await page.on_close(None)
        await page.on_connect(None)
        login_view = content_container.content
        assert any(
            isinstance(ctrl, ft.Button) and ctrl.content == "使用 GitHub 登录"
            for ctrl in login_view.controls
        )

    asyncio.run(_test())


def test_real_logout_cleanup_and_actor_revocation(tmp_path, monkeypatch):
    initialize(tmp_path, "production", journal_mode="DELETE")
    monkeypatch.setattr(app, "APP_MODE", "production")
    monkeypatch.setattr(app, "GITHUB_CLIENT_ID", "test_client_id")
    monkeypatch.setattr(app, "GITHUB_CLIENT_SECRET", "test_client_secret")
    monkeypatch.setattr(app, "GITHUB_ALLOWED_USER_ID", "12345678")
    monkeypatch.setattr(app, "STATE_DIR", tmp_path)

    async def _test():
        main = app.build_app()
        mock_auth = MagicMock()
        mock_auth.user = {"id": "12345678"}
        page = AppMockPage(auth=mock_auth)
        await main(page)

        # Log in
        await page.on_login(
            ft.LoginEvent(name="login", control=None, error=None, error_description=None)
        )
        assert page.logged_out is False

        # Navigate to settings
        col = page.controls[0].controls[0].controls[0]
        header = col.content.controls[0]
        settings_btn = header.content.controls[2]
        assert inspect.iscoroutinefunction(settings_btn.on_click)
        await settings_btn.on_click(None)

        content_container = col.content.controls[1]
        settings_view = content_container.content
        assert settings_view.controls[0].controls[1].value == "系统设置"
        settings_back = settings_view.controls[0].controls[0]
        assert inspect.iscoroutinefunction(settings_back.on_click)
        await settings_back.on_click(None)
        assert content_container.content is not settings_view

        await settings_btn.on_click(None)
        logout_btn = content_container.content.controls[4]
        await logout_btn.on_click(None)

        assert page.logged_out is True
        current_content = content_container.content
        assert any(
            isinstance(ctrl, ft.Button) and ctrl.content == "使用 GitHub 登录"
            for ctrl in current_content.controls
        )

    asyncio.run(_test())


@pytest.mark.parametrize("gap", ["unusable", "missing"])
def test_company_gap_disables_ack_and_labels_old_facts(tmp_path, monkeypatch, gap):
    initialize(tmp_path, "demo", journal_mode="DELETE")
    monkeypatch.setattr(app, "APP_MODE", "demo")
    monkeypatch.setattr(app, "STATE_DIR", tmp_path)
    fixture = Path(__file__).parent / "tests/fixtures/peer_complete_v1.json"
    previous_run = import_snapshot(tmp_path, fixture, "demo")
    from workspace import add_watch_item

    snap = json.loads(fixture.read_text(encoding="utf-8"))
    if gap == "missing":
        snap["rows"] = snap["rows"][1:]
    else:
        snap["rows"][0].update(pb=None, facts_usable=False, financial_status="failed")
    raw = json.dumps(snap, ensure_ascii=False).encode("utf-8")
    path = "snapshots/new_gap.json"
    (tmp_path / path).write_bytes(raw)
    conn = connect_workspace(tmp_path, "demo")
    try:
        add_watch_item(conn, code="600001.SH", name="示例公司甲", added_run_id=previous_run)
        register_run(
            conn,
            run_id="new_gap",
            kind="peer",
            anchor_code="600001.SH",
            rule_id="peer-screen-v1",
            captured_at="2026-09-21T16:00:00+08:00",
            valuation_date="2026-09-21",
            health="partial",
            snapshot_path=path,
            snapshot_bytes=raw,
        )
        conn.commit()
    finally:
        conn.close()

    async def check():
        page = AppMockPage()
        await app.build_app()(page)
        content = page.controls[0].controls[0].controls[0].content.controls[1]
        home_card = next(c for c in content.content.controls if isinstance(c, ft.Card))
        await home_card.content.on_click(None)
        company = content.content
        assert (
            "缺少事实数据" if gap == "missing" else "本次尝试失败/数据缺口"
        ) in company.controls[1].content.value
        assert "2026-09-21" in company.controls[1].content.value
        assert "旧可用事实（2026-09-20）" in company.controls[1].content.value
        form = company.controls[3].content.content.controls
        ack = form[5].controls[1]
        assert ack.disabled is True
        await ack.on_click(None)  # Even a stale callback cannot acknowledge old facts.
        assert "不可将旧资料标记" in form[6].value
        company_back = company.controls[0].controls[0]
        assert inspect.iscoroutinefunction(company_back.on_click)
        await company_back.on_click(None)
        assert content.content is not company

    asyncio.run(check())
    conn = connect_workspace(tmp_path, "demo")
    try:
        assert get_watch_item(conn, "600001.SH")["ack_run_id"] is None
    finally:
        conn.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_company_latest_snapshot_error_disables_ack(tmp_path, monkeypatch, damage):
    initialize(tmp_path, "demo", journal_mode="DELETE")
    monkeypatch.setattr(app, "APP_MODE", "demo")
    monkeypatch.setattr(app, "STATE_DIR", tmp_path)
    fixture = Path(__file__).parent / "tests/fixtures/peer_complete_v1.json"
    previous_run = import_snapshot(tmp_path, fixture, "demo")
    from workspace import add_watch_item

    raw = json.dumps({"anchor": "600001.SH", "rows": [{"code": "600001.SH"}]}).encode()
    path = tmp_path / "snapshots" / "latest.json"
    path.write_bytes(raw)
    conn = connect_workspace(tmp_path, "demo")
    try:
        add_watch_item(conn, code="600001.SH", name="示例公司甲", added_run_id=previous_run)
        register_run(
            conn,
            run_id="latest",
            kind="peer",
            anchor_code="600001.SH",
            rule_id="peer-screen-v1",
            captured_at="2026-09-21T16:00:00+08:00",
            valuation_date="2026-09-21",
            health="complete",
            snapshot_path="snapshots/latest.json",
            snapshot_bytes=raw,
        )
        conn.commit()
    finally:
        conn.close()
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("corrupt", encoding="utf-8")

    async def check():
        page = AppMockPage()
        await app.build_app()(page)
        content = page.controls[0].controls[0].controls[0].content.controls[1]
        home_card = next(c for c in content.content.controls if isinstance(c, ft.Card))
        await home_card.content.on_click(None)
        company = content.content
        assert (
            company.controls[1].content.value
            == "⚠️ 注意：最新已核验运行快照文件损坏或无法读取；当前展示的是上一次可用事实"
        )
        form = company.controls[3].content.content.controls
        ack = form[5].controls[1]
        assert ack.disabled is True
        await ack.on_click(None)
        assert "不可将旧资料标记" in form[6].value

    asyncio.run(check())
    conn = connect_workspace(tmp_path, "demo")
    try:
        assert get_watch_item(conn, "600001.SH")["ack_run_id"] is None
    finally:
        conn.close()


def test_company_callbacks_show_safe_feedback_on_unexpected_error(tmp_path, monkeypatch):
    initialize(tmp_path, "demo", journal_mode="DELETE")
    monkeypatch.setattr(app, "APP_MODE", "demo")
    monkeypatch.setattr(app, "STATE_DIR", tmp_path)
    fixture = Path(__file__).parent / "tests/fixtures/peer_complete_v1.json"
    first = import_snapshot(tmp_path, fixture, "demo")
    from workspace import add_watch_item

    with connect_workspace(tmp_path, "demo") as conn:
        add_watch_item(conn, code="600001.SH", name="示例公司甲", added_run_id=first)

    def fail(**_kwargs):
        raise RuntimeError("private diagnostic")

    monkeypatch.setattr(app, "save_watch", fail)
    monkeypatch.setattr(app, "mark_seen", fail)

    async def check():
        page = AppMockPage()
        await app.build_app()(page)
        content = page.controls[0].controls[0].controls[0].content.controls[1]
        card = next(c for c in content.content.controls if isinstance(c, ft.Card))
        await card.content.on_click(None)
        company = content.content
        form = company.controls[2].content.content.controls
        buttons = form[5]
        feedback = form[6]
        await buttons.controls[0].on_click(None)
        assert feedback.value == "保存失败: 请稍后重试"
        await buttons.controls[1].on_click(None)
        assert feedback.value == "标记已阅失败: 请稍后重试"
        assert "private diagnostic" not in feedback.value

    asyncio.run(check())


def test_timeout_boundaries_configured():
    assert app.FLET_SESSION_TIMEOUT > 0
    assert app.FLET_OAUTH_STATE_TIMEOUT > 0
    assert app.FLET_SESSION_TIMEOUT == 3600
    assert app.FLET_OAUTH_STATE_TIMEOUT == 600


def test_disconnect_draft_version_conflict_prevents_overwrite(tmp_path, monkeypatch):
    initialize(tmp_path, "production", journal_mode="DELETE")
    monkeypatch.setattr(app, "APP_MODE", "production")
    monkeypatch.setattr(app, "GITHUB_CLIENT_ID", "test_client_id")
    monkeypatch.setattr(app, "GITHUB_CLIENT_SECRET", "test_client_secret")
    monkeypatch.setattr(app, "GITHUB_ALLOWED_USER_ID", "12345678")
    monkeypatch.setattr(app, "STATE_DIR", tmp_path)

    from workspace import (
        add_watch_item,
        connect_workspace,
        get_watch_item,
        register_run,
        save_watch_item,
    )

    init_snap = {
        "rows": [
            {
                "code": "600001.SH",
                "name": "测试标的甲",
                "facts_usable": True,
                "pb": 1.5,
                "valuation_date": "2026-09-20",
                "valuation_source": "tracker",
                "financial_source": "tracker",
                "risk_source": "tracker",
                "annual_roes": [
                    {"year": y, "roe": 14.0, "source": "tracker", "ann_date": f"{y + 1}0415"}
                    for y in (2023, 2024, 2025)
                ],
                "roe_mean": 14.0,
            }
        ],
        "results": {"ranking": [{"code": "600001.SH", "name": "测试标的甲", "position": 1}]},
    }
    snap_bytes = json.dumps(init_snap).encode("utf-8")
    snap_path = tmp_path / "snapshots" / "init.json"
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    snap_path.write_bytes(snap_bytes)

    conn = connect_workspace(tmp_path, "production")
    register_run(
        conn,
        run_id="run_init",
        kind="peer",
        anchor_code="600001.SH",
        rule_id="peer-screen-v1",
        captured_at="2026-09-20T16:00:00+08:00",
        valuation_date="2026-09-20",
        health="complete",
        snapshot_path="snapshots/init.json",
        snapshot_bytes=snap_bytes,
    )
    add_watch_item(conn, code="600001.SH", name="测试标的甲", added_run_id="run_init")
    conn.commit()
    conn.close()

    async def _test():
        main = app.build_app()
        mock_auth = MagicMock()
        mock_auth.user = {"id": "12345678"}
        page = AppMockPage(auth=mock_auth)
        await main(page)

        # Login
        await page.on_login(
            ft.LoginEvent(name="login", control=None, error=None, error_description=None)
        )
        col = page.controls[0].controls[0].controls[0]
        content_container = col.content.controls[1]

        # Navigate to company
        card = next(c for c in content_container.content.controls if isinstance(c, ft.Card))
        await card.content.on_click(None)

        # Type draft reason while revision is 1
        form_card = content_container.content.controls[2]
        reason_field = form_card.content.content.controls[2]
        reason_field.value = "用户编写中的草稿"
        reason_field.on_change(None)

        # Disconnect happens
        await page.on_disconnect(None)

        # While disconnected, another session updates SQLite to revision 2
        db_conn = connect_workspace(tmp_path, "production")
        it = get_watch_item(db_conn, "600001.SH")
        assert it is not None
        save_watch_item(
            db_conn,
            code="600001.SH",
            expected_revision=it["revision"],
            status="research",
            reason="并发会话已提交更新",
        )
        db_conn.commit()
        db_conn.close()

        # Reconnect
        await page.on_connect(None)

        reconnected_card = content_container.content.controls[2]
        ctrls = reconnected_card.content.content.controls
        # Conflict banner inserted at index 1
        assert "版本冲突" in getattr(ctrls[1].content.controls[0], "value", "")
        # Reason field preserved at index 3
        reason_in_ui = ctrls[3]
        assert reason_in_ui.value == "用户编写中的草稿"

        # Save button disabled
        btn_row = ctrls[6]
        save_btn = btn_row.controls[0]
        assert save_btn.disabled is True

        # Direct save blocked
        await save_btn.on_click(None)
        feedback = ctrls[7]
        assert "版本冲突" in feedback.value

        # While in conflict mode, user continues typing edits
        reason_in_ui.value = "冲突状态下继续编辑的内容"
        reason_in_ui.on_change(None)

        # Second disconnect happens
        await page.on_disconnect(None)

        # Second reconnect
        await page.on_connect(None)

        reconnected_card2 = content_container.content.controls[2]
        ctrls2 = reconnected_card2.content.content.controls
        # Conflict banner MUST STILL be present
        assert "版本冲突" in getattr(ctrls2[1].content.controls[0], "value", "")
        # Reason field preserved with latest typed edits
        reason_in_ui2 = ctrls2[3]
        assert reason_in_ui2.value == "冲突状态下继续编辑的内容"
        # Save button MUST STILL be disabled
        save_btn2 = ctrls2[6].controls[0]
        assert save_btn2.disabled is True

        # Direct save still blocked
        await save_btn2.on_click(None)
        feedback2 = ctrls2[7]
        assert "版本冲突" in feedback2.value

        # Discard draft button
        discard_btn = ctrls2[1].content.controls[2]
        assert "放弃草稿" in discard_btn.content
        await discard_btn.on_click(None)

        # Reloads fresh DB revision 2
        fresh_card = content_container.content.controls[2]
        fresh_ctrls = fresh_card.content.content.controls
        fresh_reason = fresh_ctrls[2]
        assert fresh_reason.value == "并发会话已提交更新"
        fresh_save_btn = fresh_ctrls[5].controls[0]
        assert fresh_save_btn.disabled is False

    asyncio.run(_test())


def test_discover_damaged_snapshot_shows_fallback_then_error(tmp_path, monkeypatch):
    initialize(tmp_path, "demo", journal_mode="DELETE")
    monkeypatch.setattr(app, "APP_MODE", "demo")
    monkeypatch.setattr(app, "STATE_DIR", tmp_path)
    fixture = Path(__file__).parent / "tests/fixtures/peer_complete_v1.json"
    first = import_snapshot(tmp_path, fixture, "demo")
    raw = b"{}"
    path = tmp_path / "snapshots" / "latest.json"
    path.write_bytes(raw)
    with connect_workspace(tmp_path, "demo") as conn:
        register_run(
            conn,
            run_id="latest",
            kind="peer",
            anchor_code="600001.SH",
            rule_id="peer-screen-v1",
            captured_at="2026-09-21T16:00:00+08:00",
            valuation_date="2026-09-21",
            health="complete",
            snapshot_path="snapshots/latest.json",
            snapshot_bytes=raw,
        )
        old_path = (
            tmp_path
            / conn.execute(
                "SELECT snapshot_path FROM screen_runs WHERE run_id=?", (first,)
            ).fetchone()["snapshot_path"]
        )
    path.unlink()

    async def check():
        page = AppMockPage()
        await app.build_app()(page)
        page.navigation_bar.selected_index = 1
        await page.navigation_bar.on_change(SimpleNamespace(control=page.navigation_bar))
        disc = page.controls[0].controls[0].controls[0].content.controls[1].content
        assert "最新同业运行 (latest) 快照损坏" in disc.controls[3].content.value
        assert disc.controls[4].controls  # history ranking remains visible
        old_path.unlink()
        await page.navigation_bar.on_change(SimpleNamespace(control=page.navigation_bar))
        disc = page.controls[0].controls[0].controls[0].content.controls[1].content
        assert disc.controls[3].content.value == "最新同业快照损坏或无法读取"
        assert disc.controls[4].controls == []

    asyncio.run(check())

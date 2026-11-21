"""Real browser assertions; no screenshots, traces, HTML or token URLs exported."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import httpx

from .host import require, wait_for


def onboard(base, username, password):
    """Create the owner only on a freshly booted, not-yet-onboarded guest."""
    with httpx.Client(base_url=base, timeout=60) as client:
        steps = client.get("/api/onboarding")
        require(steps.status_code == 200, "fresh onboarding endpoint missing")
        require(all(not s["done"] for s in steps.json()), "guest is not fresh")
        r = client.post("/api/onboarding/users", json={
            "name": "HAOS acceptance owner", "username": username, "password": password,
            "client_id": base + "/", "language": "en"})
        require(r.status_code == 200, "HA owner creation failed")
        r = client.post("/auth/token", data={"grant_type": "authorization_code",
                                            "code": r.json()["auth_code"], "client_id": base + "/"})
        require(r.status_code == 200, "HA owner authentication failed")
        client.headers["Authorization"] = "Bearer " + r.json()["access_token"]
        for step, payload in [
            ("core_config", {}), ("analytics", {}),
            ("integration", {"client_id": base + "/", "redirect_uri": base + "/?auth_callback=1"}),
        ]:
            require(client.post("/api/onboarding/" + step, json=payload).status_code == 200,
                    "HA onboarding step failed")


def error_code(error):
    """Classify only known browser failures; never retain exception text."""
    from playwright.sync_api import Error, TimeoutError

    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, Error):
        # Playwright includes DOM, credentials and token URLs in error messages.
        # Inspect a bounded prefix only to emit these fixed labels.
        message = str(error)[:4096]
        for marker, code in (
            ("strict mode violation", "ambiguous_locator"),
            ("net::ERR_CONNECTION_REFUSED", "connection_refused"),
            ("net::ERR_NAME_NOT_RESOLVED", "name_not_resolved"),
            ("net::ERR_ABORTED", "navigation_aborted"),
            ("interrupted by another navigation", "navigation_interrupted"),
            ("Executable doesn't exist", "browser_executable_missing"),
            ("Host system is missing dependencies", "browser_dependencies_missing"),
            ("Target page, context or browser has been closed", "browser_closed"),
        ):
            if marker in message:
                return code
        return "playwright_error"
    if isinstance(error, AssertionError):
        return "assertion_failed"
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return "response_shape"
    if isinstance(error, RuntimeError):
        return "requirement_failed"
    return "other"


def ingress(base, ha_username, ha_password, username, password, member, public_base, *, progress=None):
    from playwright.sync_api import sync_playwright, expect

    if progress is None:
        progress = {}
    progress.update(stage="playwright_start", error=None, javascript_errors=0, asset_errors=0,
                    document=None, cleanup={"context": None, "browser": None, "driver": None})
    driver = browser = context = None
    failed = False
    try:
        driver = sync_playwright().start()
        progress["stage"] = "browser_launch"
        browser = driver.chromium.launch(headless=True, args=["--no-sandbox"])
        progress["stage"] = "browser_context"
        context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        progress["stage"] = "new_page"
        page = context.new_page()
        # Keep only counts, never error messages or URLs containing ingress tokens.
        def page_error(_):
            progress["javascript_errors"] += 1

        def response_seen(response):
            in_ingress = "/api/hassio_ingress/" in response.url
            if response.status >= 400 and in_ingress and urlsplit(response.url).path.endswith((".js", ".css")):
                progress["asset_errors"] += 1
            if response.request.resource_type == "document":
                progress["document"] = {"scope": "ingress" if in_ingress else "ha",
                                        "http_status": response.status,
                                        "same_origin": urlsplit(response.url)[:2] == urlsplit(base)[:2]}

        page.on("pageerror", page_error)
        page.on("response", response_seen)
        progress["stage"] = "ha_navigation"
        page.goto(base + "/", wait_until="domcontentloaded")
        progress["stage"] = "ha_username"
        page.locator('input[name="username"]').fill(ha_username)
        progress["stage"] = "ha_password"
        page.locator('input[name="password"]').fill(ha_password)
        progress["stage"] = "ha_submit"
        page.get_by_role("button", name=re.compile(r"^(Log in|Sign in)$", re.I)).click()
        sidebar = page.locator("ha-sidebar")
        progress["stage"] = "ha_sidebar"
        expect(sidebar).to_be_visible(timeout=60000)
        progress["stage"] = "ingress_sidebar_link"
        sidebar.get_by_role("link", name="Omnigent", exact=True).click()
        progress["stage"] = "ingress_frame"
        frame = wait_for(lambda: next((f for f in page.frames if
                                      "/api/hassio_ingress/" in f.url), None),
                         60, "HA sidebar Ingress iframe", 1)
        progress["stage"] = "addon_username"
        frame.get_by_label("Username", exact=True).fill(username)
        progress["stage"] = "addon_password"
        frame.get_by_label("Password", exact=True).fill(password)
        progress["stage"] = "addon_submit"
        frame.get_by_role("button", name="Sign in", exact=True).click()
        progress["stage"] = "addon_login"
        expect(frame.get_by_label("Password", exact=True)).not_to_be_visible(timeout=30000)
        progress["stage"] = "ingress_path"
        match = re.search(r"(/api/hassio_ingress/[^/]+)", urlsplit(frame.url).path)
        require(match is not None, "login escaped HA Ingress")
        ingress_base = base + match.group(1)

        # Visit actual lazy-loaded settings pages through the real HA proxy.
        for section, heading in [("members", "Members"), ("policies", "Global Policies"),
                                 ("sharing", "Session sharing"), ("mcp-server", "Create profile")]:
            progress["stage"] = "settings_navigation"
            progress["section"] = section
            frame.goto(ingress_base + "/settings/" + section, wait_until="domcontentloaded")
            progress["stage"] = "settings_heading"
            expect(frame.get_by_role("heading", name=heading, exact=True)).to_be_visible(timeout=30000)
        name = "Ingress browser fixture"
        progress["stage"] = "profile_name"
        frame.get_by_label("Profile name", exact=True).fill(name)
        progress["stage"] = "profile_account"
        frame.get_by_label("Omnigent account", exact=True).fill(member["username"])
        progress["stage"] = "profile_password"
        frame.get_by_label("Account password", exact=True).fill(member["password"])
        progress["stage"] = "profile_create"
        with page.expect_response(lambda r: r.request.method == "POST" and
                                  r.url.endswith("/v1/admin/mcp-server/profiles")) as result:
            frame.get_by_role("button", name="Create MCP link", exact=True).click()
        progress["stage"] = "profile_response"
        created = result.value.json()
        dialog = frame.get_by_role("dialog")
        progress["stage"] = "profile_dialog"
        expect(dialog).to_be_visible()
        progress["stage"] = "profile_canonical_url"
        canonical = created["connection_url"]
        require(canonical == public_base + "/mcp/private_" + created["token"],
                "Ingress API connection URL differs from complete public base")
        progress["stage"] = "profile_copy"
        frame.get_by_role("button", name="Copy URL", exact=True).click()
        progress["stage"] = "profile_clipboard"
        require(page.evaluate("navigator.clipboard.readText()") == canonical,
                "Ingress copy differs from API connection URL")
        progress["stage"] = "profile_dismiss"
        frame.get_by_role("button", name="Done", exact=True).click()
        progress["stage"] = "profile_hidden"
        expect(dialog).not_to_be_visible()
        require(canonical not in frame.locator("body").inner_text(), "one-time URL remained visible")

        # Expire only the add-on session, preserving HA's separate login.
        progress["stage"] = "expire_addon_session"
        cookies = context.cookies()
        context.clear_cookies()
        context.add_cookies([c for c in cookies if c["name"] not in ("ap_session", "__Host-ap_session")])
        progress["stage"] = "expired_navigation"
        frame.goto(ingress_base + "/settings/mcp-server", wait_until="domcontentloaded")
        progress["stage"] = "expired_login"
        expect(frame.get_by_label("Password", exact=True)).to_be_visible(timeout=30000)
        require("/api/hassio_ingress/" in frame.url, "expired session escaped Ingress")
        progress["stage"] = "relogin_username"
        frame.get_by_label("Username", exact=True).fill(username)
        progress["stage"] = "relogin_password"
        frame.get_by_label("Password", exact=True).fill(password)
        progress["stage"] = "relogin_submit"
        frame.get_by_role("button", name="Sign in", exact=True).click()
        progress["stage"] = "addon_relogin"
        expect(frame.get_by_role("heading", name="Create profile", exact=True)).to_be_visible(timeout=30000)
        progress["stage"] = "addon_logout"
        frame.get_by_role("button", name="Sign out", exact=True).click()
        progress["stage"] = "logout_login"
        expect(frame.get_by_label("Password", exact=True)).to_be_visible(timeout=30000)
        require("/api/hassio_ingress/" in frame.url, "logout escaped Ingress")
        progress["stage"] = "browser_errors"
        require(not progress["javascript_errors"] and not progress["asset_errors"],
                "Ingress browser JavaScript or asset failure")
        progress["stage"] = "complete"
        return created
    except Exception as error:
        failed = True
        progress["error"] = error_code(error)
        raise
    finally:
        cleanup_failed = False
        for name, resource in (("context", context), ("browser", browser), ("driver", driver)):
            if resource is not None:
                try:
                    resource.stop() if name == "driver" else resource.close()
                    progress["cleanup"][name] = True
                except Exception:
                    progress["cleanup"][name] = False
                    cleanup_failed = True
        if cleanup_failed and not failed:
            progress["stage"] = "browser_cleanup"
            progress["error"] = "cleanup_failed"
            raise RuntimeError("owned browser cleanup failed")

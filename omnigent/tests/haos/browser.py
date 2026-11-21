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


def ingress(base, ha_username, ha_password, username, password, member, public_base):
    from playwright.sync_api import sync_playwright, expect

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        page = context.new_page()
        # Keep only counts, never error messages or URLs containing ingress tokens.
        failures = []
        page.on("pageerror", lambda _: failures.append("javascript"))
        page.on("response", lambda r: failures.append("asset") if
                r.status >= 400 and "/api/hassio_ingress/" in r.url
                and urlsplit(r.url).path.endswith((".js", ".css")) else None)
        page.goto(base + "/", wait_until="domcontentloaded")
        page.locator('input[name="username"]').fill(ha_username)
        page.locator('input[name="password"]').fill(ha_password)
        page.get_by_role("button", name=re.compile(r"^(Log in|Sign in)$", re.I)).click()
        sidebar = page.locator("ha-sidebar")
        expect(sidebar).to_be_visible(timeout=60000)
        sidebar.get_by_role("link", name="Omnigent", exact=True).click()
        frame = wait_for(lambda: next((f for f in page.frames if
                                      "/api/hassio_ingress/" in f.url), None),
                         60, "HA sidebar Ingress iframe", 1)
        frame.get_by_label("Username", exact=True).fill(username)
        frame.get_by_label("Password", exact=True).fill(password)
        frame.get_by_role("button", name="Sign in", exact=True).click()
        expect(frame.get_by_label("Password", exact=True)).not_to_be_visible(timeout=30000)
        match = re.search(r"(/api/hassio_ingress/[^/]+)", urlsplit(frame.url).path)
        require(match is not None, "login escaped HA Ingress")
        ingress_base = base + match.group(1)

        # Visit actual lazy-loaded settings pages through the real HA proxy.
        for section, heading in [("members", "Members"), ("policies", "Global Policies"),
                                 ("sharing", "Session sharing"), ("mcp-server", "Create profile")]:
            frame.goto(ingress_base + "/settings/" + section, wait_until="domcontentloaded")
            expect(frame.get_by_role("heading", name=heading, exact=True)).to_be_visible(timeout=30000)
        name = "Ingress browser fixture"
        frame.get_by_label("Profile name", exact=True).fill(name)
        frame.get_by_label("Omnigent account", exact=True).fill(member["username"])
        frame.get_by_label("Account password", exact=True).fill(member["password"])
        with page.expect_response(lambda r: r.request.method == "POST" and
                                  r.url.endswith("/v1/admin/mcp-server/profiles")) as result:
            frame.get_by_role("button", name="Create MCP link", exact=True).click()
        created = result.value.json()
        dialog = frame.get_by_role("dialog")
        expect(dialog).to_be_visible()
        canonical = created["connection_url"]
        require(canonical == public_base + "/mcp/private_" + created["token"],
                "Ingress API connection URL differs from complete public base")
        frame.get_by_role("button", name="Copy URL", exact=True).click()
        require(page.evaluate("navigator.clipboard.readText()") == canonical,
                "Ingress copy differs from API connection URL")
        frame.get_by_role("button", name="Done", exact=True).click()
        expect(dialog).not_to_be_visible()
        require(canonical not in frame.locator("body").inner_text(), "one-time URL remained visible")

        # Expire only the add-on session, preserving HA's separate login.
        cookies = context.cookies()
        context.clear_cookies()
        context.add_cookies([c for c in cookies if c["name"] not in ("ap_session", "__Host-ap_session")])
        frame.goto(ingress_base + "/settings/mcp-server", wait_until="domcontentloaded")
        expect(frame.get_by_label("Password", exact=True)).to_be_visible(timeout=30000)
        require("/api/hassio_ingress/" in frame.url, "expired session escaped Ingress")
        frame.get_by_label("Username", exact=True).fill(username)
        frame.get_by_label("Password", exact=True).fill(password)
        frame.get_by_role("button", name="Sign in", exact=True).click()
        expect(frame.get_by_role("heading", name="Create profile", exact=True)).to_be_visible(timeout=30000)
        frame.get_by_role("button", name="Sign out", exact=True).click()
        expect(frame.get_by_label("Password", exact=True)).to_be_visible(timeout=30000)
        require("/api/hassio_ingress/" in frame.url, "logout escaped Ingress")
        require(not failures, "Ingress browser JavaScript or asset failure")
        context.close()
        browser.close()
        return created

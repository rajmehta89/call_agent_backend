"""Smoke-test every dashboard page in Chrome, as an admin and as a team member, at phone and laptop width.

Fails (exit code 1) on: a page error, an /api request answering 4xx/5xx, sideways scrolling, or a page that a
role should (or should not) be allowed to open. Needs the backend on :8000, the frontend on :3000, and
`pip install playwright` (it drives the installed Chrome; no browser download).

    python scripts/smoke_pages.py                       # admin = owner account, member = first active team member
    python scripts/smoke_pages.py --admin a@x.com --member b@y.com --widths 390,1366
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.CRITICAL)
from env_loader import load_project_env  # noqa: E402

load_project_env()
from auth_service import can_assign_conversations, issue_token  # noqa: E402
from mongo_client import mongo_client  # noqa: E402

FRONTEND = os.getenv("SMOKE_FRONTEND_URL", "http://localhost:3000")
# Pages only admins may open (frontend AppShell.requiredPermission, backend require_permission).
ADMIN_ONLY = {"/voice/agent", "/voice/pricing", "/voice/providers", "/brain/knowledge", "/integrations/shopify", "/settings/team"}


def pages(user):
    """Every page; the lead and call detail pages use a lead this user may open (team members: one they own)."""
    scope = {} if can_assign_conversations(user) else {"assigned_to": str(user["_id"])}
    lead = mongo_client.leads.find_one(scope, sort=[("_id", -1)])
    call = mongo_client.calls.find_one({"lead_id": {"$in": [lead["_id"], str(lead["_id"])]}} if lead and scope else {}, sort=[("call_date", -1)]) if lead or not scope else None
    found = ["/dashboard", "/tasks", "/voice", "/voice/agent", "/voice/browser-test", "/calls", "/voice/pricing", "/voice/providers", "/brain/knowledge",
             "/integrations/shopify", "/leads", "/settings/team", "/settings/account", "/inbox", "/inbox/unassigned", "/inbox/mine", "/inbox/assigned",
             "/inbox/mentioned", "/inbox/failed", "/inbox/channel", "/inbox/status/new_lead"]
    if call:
        found.append(f"/voice/calls/{call['_id']}")
    if lead:
        found.append(f"/leads/{lead['_id']}")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--admin")
    parser.add_argument("--member")
    parser.add_argument("--widths", default="390,1366")
    args = parser.parse_args()
    users = list(mongo_client.users.find({"active": {"$ne": False}}))
    admin = next((u for u in users if u.get("email") == args.admin), None) if args.admin else next((u for u in users if can_assign_conversations(u)), None)
    member = next((u for u in users if u.get("email") == args.member), None) if args.member else next((u for u in users if not can_assign_conversations(u)), None)
    roles = [(name, user) for name, user in (("admin", admin), ("member", member)) if user]

    from playwright.sync_api import sync_playwright

    failures = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        for width in [int(value) for value in args.widths.split(",")]:
            for role, user in roles:
                context = browser.new_context(viewport={"width": width, "height": 900})
                context.add_init_script(f"window.localStorage.setItem('agentflow_token', {issue_token(user)!r})")
                page = context.new_page()
                problems: list = []
                page.on("pageerror", lambda error: problems.append(f"page error: {str(error)[:160]}"))
                page.on("response", lambda r: "/api/" in r.url and r.status >= 400 and problems.append(f"HTTP {r.status} {r.request.method} {r.url.split(':8000')[-1][:90]}"))
                for path in pages(user):
                    problems.clear()
                    page.goto(f"{FRONTEND}{path}", wait_until="load", timeout=120000)
                    page.wait_for_timeout(4000)
                    blocked = "Access restricted" in page.inner_text("body")
                    should_block = role == "member" and path in ADMIN_ONLY
                    if blocked != should_block:
                        problems.append("opened, but this role should be blocked" if should_block else "blocked, but this role should get in")
                    if page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"):
                        problems.append("page scrolls sideways")
                    print(f"{'FAIL' if problems else 'ok  '} {width:>4}px {role:6} {path}")
                    for problem in dict.fromkeys(problems):
                        print(f"       {problem}")
                    failures += bool(problems)
                context.close()
        browser.close()
    print(f"\n{failures} page(s) failed" if failures else "\nall pages passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())


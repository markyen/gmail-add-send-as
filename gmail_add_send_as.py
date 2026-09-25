#!/usr/bin/env python3
"""
Add a "Send mail as" address to a personal Gmail account by driving Gmail's
own settings popup (the legacy HTML form at ?view=cf).

How it works (reverse-engineered from HAR captures):

  1. GET  /mail/u/N/?ui=2&ik=<ik>&view=cf&at=<at>          -> step 1 form
  2. POST (cfrp=1)  cfn=<name>, cfa=<address>, cfrt=<reply-to>  -> SMTP form
  3. POST (cfrp=3)  cfss/cfsp/cfsl/cfsw + sm587/sm465/sm25     -> "verification sent"
  4. Gmail emails a confirmation link (/mail/f-...) to the new address.
  5. GET  that link -> "Please confirm sending mail as X" + [Confirm]
  6. POST (empty body) to the same link -> "Confirmation Success!"

Instead of hand-crafting those POSTs, the script fills the real forms by their
field names, so hidden fields (at, cfrp, ...) are always correct.

Authentication: the script launches Chrome as a normal, non-automated
browser with a DEDICATED profile folder and attaches over the DevTools protocol.
The first run, you sign in by hand (incl. 2-step verification); later runs reuse
that session. Keep the profile folder private - it holds a live Google session.
Running over SSH works too; see launch_browser() for the macOS keychain detail
that makes the saved session survive.

Requirements:
    pip install playwright       # no need for `playwright install`; we use your browser

Example:
    export SMTP_PASSWORD='abcd efgh ijkl mnop'   # Gmail app password for smtp.gmail.com
    python3 gmail_add_send_as.py --name 'Your Name' --smtp-user you@gmail.com \
        alias@example.com
"""

import argparse
import getpass
import json
import os
import platform
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

MAIL = "https://mail.google.com"

DEFAULT_BROWSERS = {
    "Darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
    "Linux": ["/usr/bin/google-chrome", "/usr/bin/chromium"],
    "Windows": [r"C:\Program Files\Google\Chrome\Application\chrome.exe"],
}


def log(msg):
    print(f"[send-as] {msg}", flush=True)


# --------------------------------------------------------------------------- browser

def find_browser(explicit):
    if explicit:
        return explicit
    for p in DEFAULT_BROWSERS.get(platform.system(), []):
        if os.path.exists(p):
            return p
    sys.exit("Could not find Chrome; pass --browser /path/to/executable")


def has_gui_session():
    """True if this shell belongs to the macOS GUI (Aqua) login session."""
    if platform.system() != "Darwin":
        return True
    try:
        return subprocess.run(["launchctl", "managername"], capture_output=True,
                              text=True, timeout=5).stdout.strip() == "Aqua"
    except (OSError, subprocess.SubprocessError):
        return True


def macos_app_bundle(exe):
    """'/Applications/Foo.app/Contents/MacOS/Foo' -> '/Applications/Foo.app'."""
    marker = ".app/Contents/MacOS/"
    return exe[:exe.index(".app") + 4] if marker in exe else None


def launch_browser(exe, profile_dir, port):
    """Start the browser and wait for its DevTools port. Returns a Popen, or None
    when macOS launched it for us (see below) and there is no child to hold on to."""
    os.makedirs(profile_dir, exist_ok=True)
    flags = [f"--remote-debugging-port={port}", f"--user-data-dir={profile_dir}",
             "--no-first-run", "--no-default-browser-check", "about:blank"]

    bundle = macos_app_bundle(exe) if platform.system() == "Darwin" else None
    proc = None
    if bundle and not has_gui_session():
        # Over SSH (or from any shell outside the GUI login session) a directly
        # spawned Chrome has no macOS security session, so it cannot fetch the
        # cookie-encryption key from the keychain: the lookup fails with
        # errSecInteractionNotAllowed (-25308) because the keychain is not allowed
        # to prompt, Chrome reports "Encryption is not available", and it then
        # keeps cookies in memory only. The browser window still appears and you
        # can sign in, but nothing is saved and every run asks you to sign in
        # again. Handing the launch to LaunchServices instead starts Chrome inside
        # the console user's GUI session, where the key is available as usual.
        subprocess.run(["open", "-na", bundle, "--args"] + flags, check=True)
    else:
        proc = subprocess.Popen([exe] + flags,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1):
                return proc
        except OSError:
            time.sleep(0.5)
    stop_browser(None, proc, profile_dir, port)
    sys.exit("Browser did not expose the DevTools port. Is another instance using this profile?")


def stop_browser(browser, proc, profile_dir, port):
    """Shut the browser down *gracefully*. This matters: Chrome only commits its
    cookie store to disk on a batched timer (tens of seconds) or during a clean
    shutdown, so killing it outright right after signing in throws the session
    away. Browser.close runs the real shutdown path and flushes."""
    if browser is not None:
        try:
            browser.new_browser_cdp_session().send("Browser.close")
        except Exception:
            pass
    for _ in range(60):                      # wait for the port to go away
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1):
                time.sleep(0.5)
        except OSError:
            break
    else:                                    # still up: fall back to signalling it
        if proc is not None:
            proc.terminate()
        else:
            subprocess.run(["pkill", "-f", f"--user-data-dir={profile_dir}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if proc is not None:
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


# --------------------------------------------------------------------------- gmail session

def ensure_signed_in(page, account_index, expect_account):
    """Make sure we are looking at the inbox, prompting for a manual sign-in if
    needed. Returns the page showing the inbox, which is NOT necessarily the one
    passed in: Google's sign-in flow often finishes in a tab of its own and
    leaves the original one on a marketing or account-chooser page."""
    context = page.context
    inbox_re = re.compile(rf"https://mail\.google\.com/mail/u/{account_index}/")
    page.goto(f"{MAIL}/mail/u/{account_index}/")

    if not inbox_re.match(page.url):
        # There may well be other Chrome windows open; raise ours so it is obvious
        # which one to type into. Closing the wrong one kills this run.
        try:
            page.bring_to_front()
        except Exception:
            pass
        log("Not signed in. Sign in to Gmail in the browser window that just came to "
            "the front (title: 'Sign in - Google Accounts').")
        log("Leave that window open until this script finishes.")
        deadline = time.time() + 10 * 60
        while True:
            signed_in = next((p for p in context.pages if inbox_re.match(p.url)), None)
            if signed_in:
                page = signed_in
                break
            if time.time() > deadline:
                sys.exit("Timed out waiting for the sign-in to finish.")
            time.sleep(1)

    page.wait_for_load_state("domcontentloaded")
    # The title looks like "Inbox (3) - someone@gmail.com - Gmail".
    # Pass a function, not an expression: Google's Trusted Types policy blocks eval().
    page.wait_for_function("() => document.title.includes('@')", timeout=60_000)
    if expect_account and expect_account.lower() not in page.title().lower():
        sys.exit(f"/u/{account_index}/ is '{page.title()}', not {expect_account}. "
                 f"Fix --account-index.")
    log(f"Signed in: {page.title()}")
    return page


def get_session_tokens(page, context):
    """ik = per-account id, at = XSRF token. Both appear in Gmail's own request URLs."""
    ik = at = None
    try:
        ik = page.evaluate("() => (window.GLOBALS && window.GLOBALS[9]) || null")
    except Exception:
        pass
    urls = page.evaluate("() => performance.getEntriesByType('resource').map(e => e.name)")
    for u in urls + [page.url]:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
        ik = ik or (q.get("ik") or [None])[0]
        at = at or (q.get("at") or [None])[0]
    if not at:
        at = next((c["value"] for c in context.cookies(MAIL) if c["name"] == "GMAIL_AT"), None)
    if not (ik and re.fullmatch(r"[0-9a-f]{10}", ik)) or not at:
        sys.exit(f"Could not find Gmail session tokens (ik={ik!r}, at={'yes' if at else 'no'}).")
    return ik, at


# --------------------------------------------------------------------------- the popup flow

def page_error_text(page):
    """Gmail shows problems in red (.r/.rb) or in #smErr."""
    texts = page.eval_on_selector_all(
        "#smErr, .r, .rb", "els => els.map(e => e.innerText.trim()).filter(Boolean)")
    return " | ".join(texts)


def add_send_as(context, args, ik, at):
    page = context.new_page()
    url = (f"{MAIL}/mail/u/{args.account_index}/?ui=2&ik={ik}&view=cf&at={urllib.parse.quote(at)}")
    page.goto(url)
    page.wait_for_selector("input[name=cfa]", timeout=30_000)

    # ---- step 1: name + address
    page.fill("input[name=cfn]", args.name)
    page.fill("input[name=cfa]", args.address)
    if args.reply_to and page.locator("input[name=cfrt]").is_visible():
        page.fill("input[name=cfrt]", args.reply_to)
    alias_box = page.locator("form input[type=checkbox]")
    if alias_box.count() == 1:
        alias_box.set_checked(args.treat_as_alias)
    with page.expect_navigation():
        page.locator("form input[type=submit]").first.click()

    if page.locator("input[name=cfss]").count() == 0:
        err = page_error_text(page) or page.inner_text("body")[:500]
        sys.exit(f"Step 1 did not reach the SMTP form. Gmail said: {err}")
    log("Step 1 accepted; configuring SMTP.")

    # ---- step 2: SMTP settings
    page.fill("input[name=cfss]", args.smtp_server)
    page.select_option("select[name=cfsp]", str(args.smtp_port))
    page.fill("input[name=cfsl]", args.smtp_user)
    page.fill("input[name=cfsw]", args.smtp_password)
    # Security radios: value 2 = TLS, 1 = SSL, 0 = unsecured (port 25 only)
    sec = {"tls": "2", "ssl": "1", "none": "0"}[args.security]
    radio = page.locator(f"input[name=sm{args.smtp_port}][value='{sec}']")
    if radio.count():
        radio.check()
    with page.expect_navigation(timeout=90_000):   # Gmail logs in to the SMTP server here
        page.click("#focus")

    body = page.inner_text("body")
    if "confirmation" not in body.lower() and "verif" not in body.lower():
        sys.exit(f"SMTP step failed. Gmail said: {page_error_text(page) or body[:500]}")
    log(f"Gmail accepted the SMTP settings and sent a confirmation email to {args.address}.")
    page.close()


# --------------------------------------------------------------------------- verification

def find_confirmation_link(context, args, timeout_s=180):
    """Look for Gmail's confirmation email in this same inbox (works when the new
    address forwards to this Gmail account)."""
    page = context.new_page()
    q = f'from:gmail-noreply@google.com "Send Mail as {args.address}" newer_than:1d'
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        page.goto(f"{MAIL}/mail/u/{args.account_index}/#search/{urllib.parse.quote_plus(q)}")
        try:
            page.wait_for_selector("tr.zA", timeout=15_000)
            page.locator("tr.zA").first.click()
            page.wait_for_selector("a[href*='/mail/f-']", timeout=15_000)
            href = page.locator("a[href*='/mail/f-']").last.get_attribute("href")
            page.close()
            return href
        except PWTimeout:
            log("Confirmation email not found yet; retrying...")
            time.sleep(10)
    page.close()
    return None


def confirm(context, link, address):
    """The link (GET) shows a 'Confirmation' page: "Please confirm sending mail as
    <address>" plus a form with one <input type=submit value=Confirm>. Submitting
    it (empty-body POST to the same URL) returns 'Confirmation Success!' with
    "The Gmail user may now send mail as <address>"."""
    page = context.new_page()
    page.goto(link)
    page.wait_for_load_state("domcontentloaded")

    if page.title().startswith("Confirmation Success"):          # link already used
        return address.lower() in page.inner_text("body").lower(), page

    shown = page.locator("strong").first.inner_text().strip().lower()
    if shown != address.lower():
        log(f"Confirmation page is for {shown!r}, not {address!r}; not confirming.")
        return False, page

    with page.expect_navigation():
        page.click("form input[type=submit][value='Confirm']")

    body = page.inner_text("body").lower()
    ok = (page.title().startswith("Confirmation Success")
          and f"may now send mail as {address.lower()}" in body)
    return ok, page


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("address", help="the new send-as address, e.g. alias@example.com")
    ap.add_argument("--account-index", type=int, default=0,
                    help="the N in mail.google.com/mail/u/N/")
    ap.add_argument("--expect-account", default=None,
                    help="abort unless /u/N/ is this Gmail address "
                         "(defaults to --smtp-user; '' to skip the check)")
    ap.add_argument("--name", default="",
                    help="the display name shown on mail sent from the new address")
    ap.add_argument("--reply-to", default="")
    ap.add_argument("--treat-as-alias", action="store_true", help="tick 'Treat as an alias'")
    ap.add_argument("--smtp-server", default="smtp.gmail.com")
    ap.add_argument("--smtp-port", type=int, choices=[25, 465, 587], default=587)
    ap.add_argument("--smtp-user", default="",
                    help="the Gmail address used to authenticate to the SMTP server")
    ap.add_argument("--security", choices=["tls", "ssl", "none"], default="tls")
    ap.add_argument("--verify-only", action="store_true",
                    help="skip adding the address; just find and click the confirmation link")
    ap.add_argument("--auto-verify", action=argparse.BooleanOptionalAction, default=True,
                    help="find the confirmation email in this inbox and click it "
                         "(default: on; --no-auto-verify to skip)")
    ap.add_argument("--browser", help="path to a Chrome executable")
    ap.add_argument("--profile-dir", default=os.path.expanduser("~/.gmail-send-as-profile"))
    ap.add_argument("--port", type=int, default=9333)
    args = ap.parse_args()

    # Guard against pointing at the wrong signed-in account unless told otherwise.
    if args.expect_account is None:
        args.expect_account = args.smtp_user

    # Adding an address needs to know who you are; verifying an existing one does not.
    if not args.verify_only:
        missing = [flag for flag, value in (("--name", args.name),
                                            ("--smtp-user", args.smtp_user)) if not value]
        if missing:
            sys.exit(f"Missing required option(s): {', '.join(missing)}.")
    if args.verify_only:
        args.smtp_password = ""
    else:
        args.smtp_password = os.environ.get("SMTP_PASSWORD") or getpass.getpass("SMTP password: ")

    proc = launch_browser(find_browser(args.browser), args.profile_dir, args.port)
    browser = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()

            page = ensure_signed_in(page, args.account_index, args.expect_account)
            if not args.verify_only:
                ik, at = get_session_tokens(page, context)
                add_send_as(context, args, ik, at)

            if args.auto_verify:
                link = find_confirmation_link(context, args)
                if not link:
                    log("No confirmation email arrived. Click the link manually when it does.")
                else:
                    ok, p = confirm(context, link, args.address)
                    if ok:
                        log(f"Confirmed: {args.address} is ready to use.")
                    else:
                        log("Opened the confirmation page but couldn't confirm success; "
                            "check the browser window.")
                        input("Press Enter to close the browser...")
            else:
                log("Click the link in the confirmation email to finish.")
    finally:
        # The signed-in profile stays on disk for the next run.
        stop_browser(browser, proc, args.profile_dir, args.port)


if __name__ == "__main__":
    main()
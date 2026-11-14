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
Google's sign-in page refuses a browser that DevTools is driving ("This browser
or app may not be secure"), so when the profile has no session the script closes
that browser and opens the profile in a plain Chrome with no DevTools port. You
sign in there by hand (incl. 2-step verification) and quit it with Cmd-Q; the
script then attaches again. Later runs reuse the session. Running over SSH works
too; see start_chrome() for the macOS keychain detail that makes the saved
session survive.

That saved session is the sharp edge here. A Google cookie cannot be scoped -
there is no Gmail-only session, so the profile is full access to the account,
and Google offers nothing narrower for a personal account (the Gmail API can add
a send-as alias, but only for service accounts with domain-wide delegation,
which consumer accounts cannot have). Since the permission cannot be reduced,
this script reduces exposure instead:

  * --profile-image keeps the profile inside an encrypted sparsebundle that is
    mounted for the run and detached after, so it is ciphertext at rest. The
    passphrase must come from off this machine to be worth anything.
  * --port defaults to 0, so the DevTools port - which is unauthenticated for as
    long as it is open - is random rather than a documented number.
  * Teardown always ends by confirming no Chrome is left on the profile, and a
    dropped SSH connection unwinds through it rather than orphaning the browser.

Requirements:
    pip install playwright       # no need for `playwright install`; we use your browser

Example:
    export SMTP_PASSWORD='abcd efgh ijkl mnop'   # Gmail app password for smtp.gmail.com
    python3 gmail_add_send_as.py --name 'Your Name' --smtp-user you@gmail.com \
        alias@example.com
"""

import argparse
import getpass
import os
import platform
import re
import select
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from profile_volume import (MountError, detach, end_profile_processes, is_mounted,
                            mount, profile_pids)

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


def devtools_port(profile_dir, deadline):
    """Chrome writes the port it actually bound on line 1 of DevToolsActivePort.
    Returns it once it answers, else None. Needed because we ask for port 0."""
    path = os.path.join(profile_dir, "DevToolsActivePort")
    while time.time() < deadline:
        try:
            with open(path) as fh:
                first = fh.readline().strip()
        except OSError:
            first = ""
        if first.isdigit():
            port = int(first)
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/version", timeout=1):
                    return port
            except OSError:
                pass
        time.sleep(0.5)
    return None


def start_chrome(exe, flags):
    """Start Chrome with these flags. Returns the child process, or None when
    macOS launched it for us."""
    bundle = macos_app_bundle(exe) if platform.system() == "Darwin" else None
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
        return None
    return subprocess.Popen([exe] + flags,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def sign_in_plain(exe, profile_dir, account_index, timeout_s=8 * 60):
    """Open the profile in a plain Chrome - no DevTools port, nothing attached -
    and wait for you to sign in and quit it. Google's sign-in page refuses a
    browser under DevTools control, and quitting is what flushes the new session
    to disk and hands the profile back. Exits, after closing Chrome, if it is
    still open at the deadline; the broker's grant (15-20 minutes) has to cover
    this wait and the run after it."""
    proc = start_chrome(exe, [f"--user-data-dir={profile_dir}", "--no-first-run",
                              "--no-default-browser-check",
                              f"{MAIL}/mail/u/{account_index}/"])
    started = time.time() + 30
    while not profile_pids(profile_dir):
        if time.time() > started:
            sys.exit("Chrome did not start for the sign-in.")
        time.sleep(0.5)

    log("Not signed in. Sign in to Gmail in the Chrome window that just opened, "
        "then quit Chrome with Cmd-Q (closing the window is not enough).")
    log(f"The run continues once Chrome has quit; it gives up after {timeout_s // 60} minutes.")
    deadline = time.time() + timeout_s
    while profile_pids(profile_dir):
        if proc is not None:
            proc.poll()                      # reap it, so it leaves the process table
        if time.time() > deadline:
            end_profile_processes(profile_dir)
            sys.exit("Timed out waiting for Chrome to quit after the sign-in.")
        time.sleep(1)


def launch_browser(exe, profile_dir, port):
    """Start the browser and wait for its DevTools port. Returns (proc, port).
    proc is None when macOS launched it for us (see below), or when we adopted an
    instance that was already running, and there is no child to hold on to.

    port=0 (the default) asks Chrome for an ephemeral port instead of a published
    one. The DevTools port is unauthenticated: anything that can open a local
    socket can drive a signed-in browser through it, so it should not sit on a
    number that is written down in --help."""
    os.makedirs(profile_dir, exist_ok=True)

    # A previous run may have left Chrome up holding a live Google session. Two
    # Chromes cannot share a profile - the second silently defers to the first -
    # so without this we would attach to whatever was already there and never say
    # so. Adopt it when it is healthy, clear it out when it is not.
    if profile_pids(profile_dir):
        found = devtools_port(profile_dir, time.time() + 5)
        if found:
            log(f"Adopting the browser already running on this profile (port {found}).")
            return None, found
        log("Stale browser on this profile with no usable DevTools port; closing it.")
        end_profile_processes(profile_dir)

    # Chrome rewrites this only at startup; a leftover would name a dead port.
    try:
        os.remove(os.path.join(profile_dir, "DevToolsActivePort"))
    except OSError:
        pass

    proc = start_chrome(exe, [f"--remote-debugging-port={port}",
                              f"--user-data-dir={profile_dir}", "--no-first-run",
                              "--no-default-browser-check", "about:blank"])

    bound = devtools_port(profile_dir, time.time() + 30)
    if bound is None:
        stop_browser(None, proc, profile_dir, None)
        sys.exit("Browser did not expose a DevTools port. "
                 "Is another instance using this profile?")
    return proc, bound


def stop_browser(browser, proc, profile_dir, port):
    """Shut the browser down *gracefully*. This matters: Chrome only commits its
    cookie store to disk on a batched timer (tens of seconds) or during a clean
    shutdown, so killing it outright right after signing in throws the session
    away. Browser.close runs the real shutdown path and flushes.

    Always finishes with end_profile_processes: a graceful close that quietly
    fails used to leave a signed-in Chrome, and its unauthenticated DevTools
    port, running indefinitely."""
    if browser is not None:
        try:
            browser.new_browser_cdp_session().send("Browser.close")
        except Exception:
            pass
    if port is not None:
        for _ in range(60):                  # wait for the port to go away
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/version", timeout=1):
                    time.sleep(0.5)
            except OSError:
                break
    if proc is not None:
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.terminate()
    if not end_profile_processes(profile_dir):
        log(f"WARNING: Chrome is still running on {profile_dir}; "
            f"it holds a live Google session. PIDs: {profile_pids(profile_dir)}")
        return False
    return True


# --------------------------------------------------------------------------- encrypted profile

def read_passphrase(from_stdin):
    """Bytes, from a pipe or from the terminal.

    Note the honest limit: CPython cannot reliably scrub a str, so the prompted
    path leaves a copy in the heap until GC. Piping it in (--passphrase-stdin)
    keeps it in a bytearray we can zero, and keeps it out of shell history."""
    if from_stdin:
        buf = bytearray(sys.stdin.buffer.readline())
    else:
        buf = bytearray(getpass.getpass("Profile image passphrase: ").encode())
    while buf and buf[-1] in b"\r\n":
        buf.pop()
    if not buf:
        sys.exit("Empty passphrase.")
    return buf


# --------------------------------------------------------------------------- gmail session

def open_inbox(page, account_index, expect_account):
    """Open the inbox. Returns the page showing it, or None when the profile is
    not signed in. Signing in is not done here: Google refuses it in a browser
    under DevTools control, so main() closes this one and calls sign_in_plain()."""
    inbox_re = re.compile(rf"https://mail\.google\.com/mail/u/{account_index}/")
    page.goto(f"{MAIL}/mail/u/{account_index}/")
    if not inbox_re.match(page.url):
        return None

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

def wait_for_enter(prompt, timeout_s=300):
    """A pause a human may never come back to. Unbounded input() here once left a
    signed-in Chrome up for two days; anything holding a live Google session has
    to have a deadline."""
    if not sys.stdin.isatty():
        log("Not a terminal, so not pausing; closing the browser.")
        return
    print(prompt, end="", flush=True)
    if select.select([sys.stdin], [], [], timeout_s)[0]:
        sys.stdin.readline()
    else:
        print()
        log(f"No input after {timeout_s}s; closing the browser anyway.")


def install_signal_handlers():
    """Turn the signals that end a remote session into a normal unwind, so the
    finally: blocks below still close the browser and unmount the volume.
    SIGHUP is the one that matters: it is what a dropped SSH connection sends."""
    def bail(signum, _frame):
        sys.exit(f"Received signal {signum}; shutting down.")
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, bail)


def attached_run(exe, args, state):
    """Launch Chrome with DevTools, attach, and do the work. Returns False, having
    done nothing, when the profile is not signed in. Records the browser in
    `state` so main() can still close it if this raises."""
    state["proc"], state["port"] = launch_browser(exe, args.profile_dir, args.port)
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{state['port']}")
        try:
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()

            page = open_inbox(page, args.account_index, args.expect_account)
            if page is None:
                return False
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
                        wait_for_enter("Press Enter to close the browser...")
            else:
                log("Click the link in the confirmation email to finish.")
            return True
        finally:
            # Shut down while the CDP connection is still alive. Once this
            # `with` exits, playwright has stopped and Browser.close can no
            # longer be sent - which is how, for a long time, it never was:
            # every run fell through to waiting out the DevTools port, then
            # to a pkill that matched nothing, and Chrome stayed up.
            state["stopped"] = stop_browser(browser, state["proc"], args.profile_dir,
                                            state["port"])


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
    ap.add_argument("--profile-dir", default=os.path.expanduser("~/.gmail-send-as-profile"),
                    help="unencrypted profile location (ignored with --profile-image)")
    ap.add_argument("--profile-image", default=None,
                    help="encrypted sparsebundle holding the profile; mounted for "
                         "the run and detached afterwards. See make-profile-image.sh")
    ap.add_argument("--mountpoint", default="/Volumes/gmail-send-as",
                    help="where --profile-image is attached")
    ap.add_argument("--passphrase-stdin", action="store_true",
                    help="read the image passphrase from stdin instead of prompting, "
                         "so it stays out of shell history and the terminal")
    ap.add_argument("--port", type=int, default=0,
                    help="DevTools port; 0 (the default) asks Chrome for a random "
                         "one, since the port is unauthenticated while it is open")
    args = ap.parse_args()
    install_signal_handlers()

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

    # With --profile-image the profile lives inside an encrypted volume that is
    # ciphertext at rest. An already-mounted volume is left alone and, crucially,
    # left mounted: something else put it there and owns detaching it.
    mounted_here = False
    if args.profile_image:
        if is_mounted(args.mountpoint):
            # Something else put it there - a broker, or you by hand - so it
            # owns detaching it. We use it and leave it alone.
            log(f"{args.mountpoint} is already mounted; using it as is.")
        else:
            passphrase = read_passphrase(args.passphrase_stdin)
            try:
                # The bytearray goes in as-is; bytes() would make an immutable
                # copy we could not scrub afterwards.
                mount(args.profile_image, args.mountpoint, passphrase)
            except MountError as e:
                sys.exit(f"Could not mount {args.profile_image}: {e}")
            finally:
                for i in range(len(passphrase)):
                    passphrase[i] = 0
            mounted_here = True
            log(f"Mounted {args.profile_image} at {args.mountpoint}.")
        args.profile_dir = os.path.join(args.mountpoint, "profile")

    exe = find_browser(args.browser)
    # The browser now on the profile: its child process, DevTools port, and
    # stop_browser's verdict once it has run (None while one may still be up)
    state = {"proc": None, "port": None, "stopped": None}
    try:
        if not attached_run(exe, args, state):
            if not state["stopped"]:
                sys.exit("Chrome is still running on the profile; not opening another to sign in.")
            state.update(proc=None, port=None, stopped=None)
            sign_in_plain(exe, args.profile_dir, args.account_index)
            if not attached_run(exe, args, state):
                sys.exit("Still not signed in after the sign-in window was closed.")
    finally:
        if state["stopped"] is None:  # nothing closed it gracefully
            state["stopped"] = stop_browser(None, state["proc"], args.profile_dir, state["port"])
        stopped = state["stopped"]
        # The signed-in profile stays on disk for the next run - as ciphertext,
        # once the volume below is detached.
        if mounted_here:
            if not stopped:
                log("Not detaching the volume while Chrome still holds it open.")
            elif detach(args.mountpoint):
                log(f"Detached {args.mountpoint}; the profile is encrypted at rest again.")
            else:
                log(f"WARNING: could not detach {args.mountpoint}. The profile is "
                    f"readable until you run: hdiutil detach {args.mountpoint}")


if __name__ == "__main__":
    main()
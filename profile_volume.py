"""The encrypted volume that holds the browser profile, and what has to be true
before it can be detached.

gmail_add_send_as.py mounts the volume itself with --profile-image, or runs on
a profile a broker mounted and passes in with --profile-dir. Either way its
teardown has to leave nothing running on the profile, so that whoever owns the
mount can detach it. That logic lives here.
"""

import os
import signal
import subprocess
import time


class MountError(Exception):
    """hdiutil refused to attach - usually a wrong passphrase."""


def is_mounted(mountpoint):
    return os.path.ismount(mountpoint)


def mount(image, mountpoint, passphrase):
    """Attach the sparsebundle at mountpoint.

    passphrase is bytes or bytearray and goes in on stdin (-stdinpass) rather
    than in argv, where any local user could read it out of `ps`. A bytearray
    is written through as-is so the caller can scrub it afterwards.
    """
    # Only where we can: under /Volumes (root-owned) mkdir fails, but hdiutil
    # creates the mountpoint itself; anywhere else hdiutil needs it to exist.
    if not os.path.exists(mountpoint):
        try:
            os.makedirs(mountpoint)
        except PermissionError:
            pass
    r = subprocess.run(
        ["hdiutil", "attach", image, "-mountpoint", mountpoint,
         "-stdinpass", "-nobrowse", "-noautoopen"],
        input=passphrase, capture_output=True)
    if r.returncode != 0:
        raise MountError(r.stderr.decode(errors="replace").strip() or "wrong passphrase?")


def detach(mountpoint, attempts=12):
    """Detach, escalating to -force halfway through: Chrome can take a moment to
    let go of its files even after the process is gone. Callers should stop
    Chrome first (see end_profile_processes) rather than rely on -force."""
    for attempt in range(attempts):
        args = ["hdiutil", "detach", mountpoint] + (["-force"] if attempt >= attempts // 2 else [])
        if subprocess.run(args, capture_output=True).returncode == 0:
            return True
        time.sleep(1)
    return not is_mounted(mountpoint)


def _ps_column(pids, column):
    """{pid: value} for one ps column. pid is printed first, so a value that
    contains spaces - Chrome's executable path does - still parses."""
    if not pids:
        return {}
    r = subprocess.run(["ps", "-p", ",".join(map(str, pids)),
                        "-o", "pid=", "-o", f"{column}="],
                       capture_output=True, text=True)
    out = {}
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if parts and parts[0].isdigit():
            out[int(parts[0])] = parts[1] if len(parts) > 1 else ""
    return out


def profile_pids(profile_dir):
    """PIDs of the browser processes running on this profile.

    pgrep only finds candidates. Matching the command line alone is not enough,
    because any process whose arguments merely *mention* the flag matches too -
    a shell loop polling for the browser, an editor, a grep - and
    end_profile_processes would then signal it. That happened: a monitoring
    loop was taken for a stale browser and killed. So a candidate counts only
    if its executable is Chrome or Chromium and the flag is a whole argument.

    Note also the "--" before the pattern. Without it pgrep reads a pattern
    beginning with "--user-data-dir" as an option and fails with "illegal
    option -- -", matching nothing. An earlier teardown was silently a no-op
    for exactly that reason, which is how a signed-in browser once stayed up
    for two days.
    """
    flag = f"--user-data-dir={profile_dir}"
    r = subprocess.run(["pgrep", "-f", "--", flag], capture_output=True, text=True)
    candidates = [int(x) for x in r.stdout.split() if x.isdigit()]
    exes = _ps_column(candidates, "comm")
    args = _ps_column(candidates, "args")
    return [pid for pid in candidates
            if any(name in exes.get(pid, "").lower() for name in ("chrome", "chromium"))
            and f" {flag} " in f" {args.get(pid, '')} "]


def end_profile_processes(profile_dir, grace_s=20):
    """Make sure nothing is left running on this profile, escalating TERM ->
    KILL. Called after the graceful path has had its chance. Returning with
    Chrome still alive would both leak a signed-in session and pin the volume
    busy, so this is the backstop that lets a detach succeed.

    Signals go to the exact PIDs profile_pids vetted, never to a pkill pattern,
    for the reason given there."""
    for sig, wait_s in ((signal.SIGTERM, grace_s), (signal.SIGKILL, 5)):
        pids = profile_pids(profile_dir)
        if not pids:
            return True
        for pid in pids:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if not profile_pids(profile_dir):
                return True
            time.sleep(0.5)
    return not profile_pids(profile_dir)

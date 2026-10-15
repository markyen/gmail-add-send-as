# gmail-add-send-as

Add a **"Send mail as"** address to a personal Gmail account from the command line.

Gmail lets you send mail from another address you own, but only through a settings
dialog that has to be clicked through by hand, once per address. This script does
that for you: it drives Gmail's own settings form in a real browser, then finds the
confirmation email Gmail sends to the new address and clicks the confirmation link.

```
$ python3 gmail_add_send_as.py --name 'Your Name' --smtp-user you@gmail.com alias@example.com
[send-as] Signed in: Inbox - you@gmail.com - Gmail
[send-as] Step 1 accepted; configuring SMTP.
[send-as] Gmail accepted the SMTP settings and sent a confirmation email to alias@example.com.
[send-as] Confirmed: alias@example.com is ready to use.
```

## How it works

Gmail's "Add another email address" popup is still the legacy HTML form at
`?view=cf`. The script fills in the real form fields rather than hand-crafting
POSTs, so hidden fields (`at`, `cfrp`, …) are always correct:

1. `GET /mail/u/N/?ui=2&ik=<ik>&view=cf&at=<at>` — name + address form
2. Submit it — SMTP settings form
3. Submit that — Gmail logs in to the SMTP server and emails a confirmation link
4. Find that email in the inbox and follow the link — *Confirmation Success*

Step 4 assumes the new address delivers back to the same Gmail inbox (the usual
case for a domain that forwards to Gmail). If it doesn't, pass `--no-auto-verify`
and click the link yourself.

## Requirements

- Python 3 and `pip install playwright` — the browser you already have is used, so
  there's no need for `playwright install`
- Chrome
- A Gmail [app password](https://myaccount.google.com/apppasswords) for
  `smtp.gmail.com` (Gmail authenticates to the relay as you when sending)

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install playwright
```

## Usage

```sh
export SMTP_PASSWORD='abcd efgh ijkl mnop'
python3 gmail_add_send_as.py --name 'Your Name' --smtp-user you@gmail.com alias@example.com
```

`--name` and `--smtp-user` are the only required options; everything else has a
sensible default for a Gmail account. The password is read from `$SMTP_PASSWORD`,
or prompted for. To keep it in a password manager, pass it in for the one command:

```sh
SMTP_PASSWORD="$(pass show passwords/smtp)" python3 gmail_add_send_as.py \
    --name 'Your Name' --smtp-user you@gmail.com alias@example.com
```

If you add addresses often, a small shell wrapper holding your name, address and
password lookup saves repeating them.

Useful flags (`--help` lists them all):

| Flag | Meaning |
| --- | --- |
| `--name`, `--smtp-user` | Display name, and the Gmail address that authenticates to SMTP |
| `--verify-only` | Skip adding the address; just find and click the confirmation link |
| `--no-auto-verify` | Add the address but don't try to confirm it |
| `--treat-as-alias` | Tick Gmail's "Treat as an alias" |
| `--reply-to` | Reply-To for the new address |
| `--account-index N` | The `N` in `mail.google.com/mail/u/N/`, for multiple signed-in accounts |
| `--browser` | Path to a Chrome executable |
| `--profile-image` | Encrypted sparsebundle holding the profile (see below) |
| `--passphrase-stdin` | Read the image passphrase from stdin rather than prompting |
| `--port` | DevTools port; `0` (default) picks a random one |

## Signing in

The script starts your browser as a normal, non-automated instance with a
**dedicated profile folder** (`~/.gmail-send-as-profile` by default) and attaches
over the DevTools protocol. The first run stops and waits while you sign in by
hand, 2-step verification included; later runs reuse that session.

That session is the most dangerous thing this repo touches, so it gets its own
section below.

## Keeping the saved session contained

A Google session cookie **cannot be scoped**. There is no Gmail-only cookie:
`__Secure-1PSID` and friends authenticate you to Drive, Photos, Takeout and
account settings alike, so the profile folder is, by construction, full access
to the account. Nor is there a narrower credential to switch to — the Gmail API
*does* have `users.settings.sendAs.create`, but it is
[restricted to service accounts with domain-wide delegation](https://developers.google.com/workspace/gmail/api/v1/reference/users/settings/sendAs/create),
which a personal account cannot have. Driving the HTML form is the only way in,
and a full session is the price.

Since the permission cannot be narrowed, the exposure is narrowed instead.

**Encrypt the profile at rest.** `make-profile-image.sh` creates an encrypted
sparsebundle; the script mounts it for the run and detaches it afterwards:

```sh
./make-profile-image.sh                 # once
PROFILE_IMAGE=~/gmail-send-as.sparsebundle ./add-send-as.sh alias@example.com
```

At rest that leaves ciphertext and no running browser. The passphrase **must
come from somewhere this machine is not** — your phone's password manager, or
piped in over SSH. Keeping it in `pass` on the same disk as the image is a lock
with the key taped to the door.

```sh
pass show sendas/profile | ssh mac workspace/gmail-add-send-as/add-send-as.sh alias@example.com
```

Encrypting at rest beats signing out between runs, at least if you work
remotely: Google's risk engine treats every fresh profile as a new device, so
repeated sign-outs buy escalating device-verification challenges precisely when
you are on a phone and least able to answer them.

**The DevTools port is unauthenticated.** While it is open, anything that can
open a local socket can drive the signed-in browser through it. It now defaults
to a random port (`--port 0`, read back from `DevToolsActivePort`) rather than a
number published in `--help`. That shrinks the window; it does not close it, so
runs should be as short as they can be.

**Teardown is not optional.** Chrome left running is a live session plus an open
port. `stop_browser` tries `Browser.close` first (which flushes the cookie
store), then confirms with `pgrep` that nothing survived, escalating to
`SIGTERM` and `SIGKILL`. `SIGHUP`, `SIGTERM` and `SIGINT` unwind through the
same path, so a dropped SSH connection cleans up instead of orphaning the
browser. If a previous run did leave one behind, the next run says so and
adopts or clears it rather than silently attaching to it.

**Revoking is not deleting.** Removing the profile folder invalidates nothing:
the session stays live on Google's side, and any copy of the cookies taken
beforehand still works. Sign out in the browser, or use
[Your devices](https://myaccount.google.com/device-activity), to actually kill it.

### Unlocking it remotely: run it under a broker

If the passphrase lives on your phone and you work over SSH, something has to
carry it to the Mac without it passing through your terminal — and in
particular without it passing through an agent, where it would land in a
transcript on this same disk and stay there.

That is a broker's job, not this script's. An agent does not need the
passphrase; it needs the volume mounted, and those are different grants. A
broker takes a request with a justification, lets you approve it from your
phone by entering the passphrase over your tailnet, mounts the volume, hands
back the profile directory, and detaches the volume on its own timer. The
passphrase travels phone → broker and never through the agent; what the agent
holds afterwards (a grant id, a path) is worthless once the grant ends, so there
is nothing to rotate.

The script needs nothing special to run that way:

```sh
profile_dir=$(my-broker request …)    # blocks until you approve on your phone
python3 gmail_add_send_as.py --profile-dir "$profile_dir" … alias@example.com
my-broker release …
```

- `--profile-dir` points at the mounted profile. The script never mounts or
  detaches it, so the broker's timer stays the one in charge.
- Teardown stops Chrome on the profile before the script exits, so the broker
  can detach as soon as the grant is released.
- Ending a grant that outlives the run is the broker's job, not the script's:
  the consumer is exactly the thing that crashes before cleaning up.

**The limit, stated plainly:** while the volume is mounted, anything running as
your user can read it. That is unavoidable — the point is to let a program use
the profile. A broker protects the long-lived secret, and so every future
session, even if a transcript leaks in full. It does not protect the current
session from a process that is already hostile. Keep grants short.

**No broker, same property:** mount the image from a second SSH session and pass
`--profile-image`; the script uses an existing mount and leaves it mounted. That
has to be a genuinely separate session, not Claude Code's `!` prefix, whose
output is captured into the transcript.

**The app password is the other unscopable credential.** It is full IMAP/SMTP
access and bypasses 2FA, so the same logic applies — generate it when you need
it, revoke it at [App passwords](https://myaccount.google.com/apppasswords)
afterwards.

Two details make the saved session survive on macOS, both handled automatically:

- A process started outside the GUI login session (over SSH, say) has no macOS
  security session, so Chrome cannot fetch its cookie-encryption key from the
  keychain — the lookup fails with `errSecInteractionNotAllowed`, Chrome reports
  *"Encryption is not available"* and silently keeps cookies in memory only. The
  script hands the launch to LaunchServices (`open -na`) in that case, which starts
  the browser inside the console user's session. **Running over SSH works.**
- Chrome only commits its cookie store on a batched timer or during a clean
  shutdown, so the script closes the browser through CDP `Browser.close` rather
  than killing it.

## Caveats

- Reverse-engineered from Gmail's current HTML. If Google changes that form, this
  breaks; the failure messages quote what Gmail actually said.
- Personal Gmail accounts only. Google Workspace admins can do this centrally.
- Nothing is stored except the browser profile. The SMTP password is passed through
  the environment and never written to disk.
- The profile is only encrypted while Chrome is not running. Anything with code
  execution as your user, during a run, can read the mounted volume. This
  defends against a stolen disk, a backup sweep and a cold profile folder — not
  against a compromised machine while the work is happening.

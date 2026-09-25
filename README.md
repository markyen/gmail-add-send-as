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

## Signing in

The script starts your browser as a normal, non-automated instance with a
**dedicated profile folder** (`~/.gmail-send-as-profile` by default) and attaches
over the DevTools protocol. The first run stops and waits while you sign in by
hand, 2-step verification included; later runs reuse that session.

Keep the profile folder private — it holds a live Google session.

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

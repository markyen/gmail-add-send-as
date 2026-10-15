#!/bin/bash
# Create the encrypted sparsebundle that holds the browser profile.
#
#   ./make-profile-image.sh [image-path]
#
# Run once. Afterwards the profile is ciphertext at rest and is mounted only for
# the duration of a run:
#
#   ./add-send-as.sh alias@example.com
#
# Choose a passphrase that lives ONLY in your phone's password manager. Putting
# it in `pass` on this machine defeats the point: the profile and the key to it
# would then sit side by side on the same disk, and anything running as you
# could reach both. The passphrase has to come from somewhere this machine is
# not, which is what makes "encrypted at rest" mean anything here.

set -euo pipefail

IMAGE="${1:-$HOME/gmail-send-as.sparsebundle}"
SIZE="${SIZE:-2g}"          # sparse: allocated on demand, not up front
MOUNTPOINT="${MOUNTPOINT:-/Volumes/gmail-send-as}"

if [[ -e "$IMAGE" ]]; then
    echo "$IMAGE already exists; refusing to overwrite it." >&2
    exit 1
fi

# -stdinpass keeps the passphrase out of argv, where `ps` would show it to every
# other user on the box.
read -r -s -p "New passphrase for $IMAGE: " pass1; echo
read -r -s -p "Again: " pass2; echo
[[ "$pass1" == "$pass2" ]] || { echo "Passphrases differ." >&2; exit 1; }
[[ -n "$pass1" ]] || { echo "Empty passphrase." >&2; exit 1; }

printf '%s' "$pass1" | hdiutil create \
    -type SPARSEBUNDLE \
    -encryption AES-256 \
    -stdinpass \
    -size "$SIZE" \
    -fs APFS \
    -volname "gmail-send-as" \
    "$IMAGE"

# Do NOT let hdiutil stash the passphrase in the login keychain: that would put
# the key back on this machine and undo the whole arrangement.
printf '%s' "$pass1" | hdiutil attach "$IMAGE" \
    -mountpoint "$MOUNTPOINT" -stdinpass -nobrowse -noautoopen
unset pass1 pass2

mkdir -p "$MOUNTPOINT/profile"
hdiutil detach "$MOUNTPOINT"

# Time Machine and friends would otherwise sweep the bundle's changed bands into
# backups. They are ciphertext, so this is belt and braces rather than a fix.
tmutil addexclusion "$IMAGE" 2>/dev/null \
    && echo "Excluded $IMAGE from Time Machine." \
    || echo "Could not add a Time Machine exclusion (not fatal)."

cat <<DONE

Created $IMAGE

Next:
  1. Sign in once, with a GUI (see "Signing in" in the README):
       ./add-send-as.sh --profile-image "$IMAGE" --verify-only --no-auto-verify dummy@example.invalid
     or just run a real add and complete the sign-in when it pauses.
  2. Point your wrapper at it by setting PROFILE_IMAGE=$IMAGE
  3. Delete the old plaintext profile once you have confirmed the new one works:
       rm -rf ~/.gmail-send-as-profile
     Signing out of that session in the browser first is what actually revokes
     it server-side; deleting the folder alone leaves it valid to anyone who
     already copied the cookies.
DONE

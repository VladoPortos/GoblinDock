#!/bin/sh
# GoblinDock container entrypoint.
#
# The container runs non-root with a uid chosen by the operator at runtime
# (compose `user: "${GD_UID}:${GD_GID}"`) so the bind-mounted /data stays
# writable. That uid usually has no /etc/passwd entry, which makes OpenSSH — and
# anything else calling getpwuid(3) — fail hard with "No user exists for uid N".
# ansible shells out to `ssh` to reach VMs, so before launching the app we fake a
# passwd/group entry for the current uid using nss_wrapper (an LD_PRELOAD shim),
# which avoids having to write to /etc/passwd at all.
set -e

export HOME=/home/appuser

if ! getent passwd "$(id -u)" >/dev/null 2>&1; then
  WRAP="$(mktemp -d /tmp/gd-nss.XXXXXX)"
  printf 'goblin:x:%s:%s:GoblinDock:%s:/bin/sh\n' "$(id -u)" "$(id -g)" "$HOME" > "$WRAP/passwd"
  printf 'goblin:x:%s:\n' "$(id -g)" > "$WRAP/group"
  LIB="$(find /usr/lib /lib -name 'libnss_wrapper.so*' 2>/dev/null | head -n1)"
  if [ -n "$LIB" ]; then
    export LD_PRELOAD="$LIB"
    export NSS_WRAPPER_PASSWD="$WRAP/passwd"
    export NSS_WRAPPER_GROUP="$WRAP/group"
  fi
fi

exec "$@"

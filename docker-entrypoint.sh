#!/bin/sh
# Drop to PUID:PGID before the daemon starts.
#
# There was no USER directive, so python3 and every ffmpeg child ran as root:
# each revealed file and each trashed source landed as root:root inside a
# library an arr owns as 1000, and that arr then could not upgrade or delete
# its own media days later, nowhere near the cause. PUID/PGID is the
# linuxserver.io/*arr contract, so a stack that already sets them needs no new
# knobs to get this right.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
# Same variable and same default as main.py, and the only path this script is
# allowed to chown.
CONFIG_DIR="${CONFIG_DIR:-/config}"

# Something already dropped us (compose `user:`, a k8s securityContext). There
# is nothing left to do and every command below would fail on permissions, so
# an upgrade must not turn one of those stacks into a restart loop.
if [ "$(id -u)" != "0" ]; then
  exec "$@"
fi

# 0 means "stay root", for hosts where nothing else can write the mount - an
# NFS export without no_root_squash, a share with fixed ownership.
if [ "$PUID" = "0" ] && [ "$PGID" = "0" ]; then
  exec "$@"
fi

# Reuse whatever already answers to the id rather than adding a second name for
# it: PGID=100 on a QNAP is the stock "users" group, and groupadd would fail on
# the duplicate gid.
group="$(getent group "$PGID" | cut -d: -f1)"
if [ -z "$group" ]; then
  group=transcodearr
  groupadd -g "$PGID" "$group"
fi
user="$(getent passwd "$PUID" | cut -d: -f1)"
if [ -z "$user" ]; then
  user=transcodearr
  useradd -u "$PUID" -g "$PGID" -M -d "$CONFIG_DIR" -s /usr/sbin/nologin "$user"
fi

# The dropped user still has to open /dev/dri/renderD128, which is mode 660
# owned by a render/video group whose gid differs on every host. Skipping this
# does not fail loudly: QSV and VAAPI just probe as unavailable and every job
# quietly re-encodes on CPU instead. --init-groups below reads /etc/group at
# exec time, so a group added here is in effect for ffmpeg. NVIDIA needs none
# of this, its device nodes are world-readable.
for dev in /dev/dri/*; do
  [ -c "$dev" ] || continue
  gid="$(stat -c %g "$dev")"
  gname="$(getent group "$gid" | cut -d: -f1)"
  [ -n "$gname" ] || { gname="dri$gid"; groupadd -g "$gid" "$gname"; }
  usermod -aG "$gname" "$user"
done

# Recursive, and ONLY here. Recursive because containers that ran before this
# entrypoint existed left transcodearr.db and /config/trash owned by root, and
# a database the new uid cannot write is a daemon that starts and then fails
# every job. Only here because the media tree belongs to someone else: it is
# terabytes we mount read-write, not ownership we get to rewrite.
mkdir -p "$CONFIG_DIR"
chown -R "$PUID:$PGID" "$CONFIG_DIR"

echo "transcodearr: starting as ${user}:${group} (${PUID}:${PGID})"
# exec, never a wrapper that waits: python3 has to inherit PID 1 so that
# `docker stop` delivers SIGTERM to the daemon itself and it can shut a running
# ffmpeg down cleanly instead of being killed 10 seconds later mid-write.
exec setpriv --reuid "$PUID" --regid "$PGID" --init-groups -- "$@"

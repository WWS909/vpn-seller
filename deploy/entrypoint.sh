#!/bin/sh
# Starts as root only to make the mounted ./data writable, then drops to
# the unprivileged "app" user for the actual server.
set -e
# The database and backups hold panel passwords and customer data: files
# the shop creates are readable by their owner only (root on the host can
# still read them).
umask 077
mkdir -p /app/data
if [ "$(id -u)" = "0" ]; then
    chown -R app:app /app/data
    chmod 700 /app/data
    exec setpriv --reuid=app --regid=app --init-groups "$@"
fi
exec "$@"

#!/bin/sh
set -eu
umask 077
mkdir -p var/backups
dest="var/backups/dispatch-$(date -u +%Y%m%dT%H%M%SZ).dump"
trap 'rm -f "$dest.partial"' EXIT
docker compose exec -T db pg_dump -U dispatch -d dispatch -Fc > "$dest.partial"
docker compose exec -T db pg_restore --list < "$dest.partial" > /dev/null
mv "$dest.partial" "$dest"
echo "$dest"

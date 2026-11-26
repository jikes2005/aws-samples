#!/bin/sh
is_ip() { case "$1" in ""|127.*) return 1 ;; *[!0-9.]*) return 1 ;; *) return 0 ;; esac; }

HOSTNAME_NOW=$(cat /proc/sys/kernel/hostname 2>/dev/null || hostname 2>/dev/null)
HOST_I=$(hostname -i 2>/dev/null | tr ' ' '\n' | grep -v '^127\.' | head -n 1)
GETENT_IP=$(getent hosts "$HOSTNAME_NOW" 2>/dev/null | awk '{print $1}' | grep -v '^127\.' | head -n 1)

META_IP=""
if [ -n "$ECS_CONTAINER_METADATA_URI_V4" ]; then
  if command -v curl >/dev/null 2>&1; then META_JSON=$(curl -s --max-time 3 "$ECS_CONTAINER_METADATA_URI_V4/task")
  elif command -v wget >/dev/null 2>&1; then META_JSON=$(wget -qO- -T 3 "$ECS_CONTAINER_METADATA_URI_V4/task")
  elif command -v python >/dev/null 2>&1; then META_JSON=$(python -c "import os,urllib.request as u; print(u.urlopen(os.environ['ECS_CONTAINER_METADATA_URI_V4']+'/task',timeout=3).read().decode())")
  fi
  META_IP=$(printf '%s' "$META_JSON" | tr -d ' \n\r\t' | sed -n 's/.*"IPv4Addresses":\["\([0-9.]*\)".*/\1/p' | head -n 1)
fi

if is_ip "$HOST_I"; then ADDR="$HOST_I"; SOURCE="hostname -i"
elif is_ip "$GETENT_IP"; then ADDR="$GETENT_IP"; SOURCE="getent hosts"
elif is_ip "$META_IP"; then ADDR="$META_IP"; SOURCE="task metadata"
else ADDR=""; SOURCE="none"
fi

SET=""
for VAR in ${ADDRESS_VARS:-NG_SERVER_NAME GUARD_EXTERNAL_IP}; do
  eval "CURRENT=\${$VAR:-}"
  if [ -z "$CURRENT" ] && [ -n "$ADDR" ]; then
    export "$VAR=$ADDR"
    SET="$SET $VAR"
  fi
done

MATCH=no; [ -n "$META_IP" ] && [ "$ADDR" = "$META_IP" ] && MATCH=yes
echo "{\"event\":\"address_resolved\",\"role\":\"${ROLE:-}\",\"hostname\":\"$HOSTNAME_NOW\",\"hostname_i\":\"$HOST_I\",\"getent_ip\":\"$GETENT_IP\",\"metadata_ip\":\"$META_IP\",\"address\":\"$ADDR\",\"source\":\"$SOURCE\",\"exported\":\"${SET# }\",\"match\":\"$MATCH\"}"

exec "$@"   # start the original (vendor) process with the variables set

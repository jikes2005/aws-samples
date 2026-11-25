set -e

HOST_I=$(hostname -i 2>/dev/null | awk '{print $1}' || true)
META_IP=""
if [ -n "$ECS_CONTAINER_METADATA_URI_V4" ]; then
  META_IP=$(python -c "import json,os,urllib.request as u; m=json.load(u.urlopen(os.environ['ECS_CONTAINER_METADATA_URI_V4']+'/task',timeout=3)); print(next(n['IPv4Addresses'][0] for c in m['Containers'] for n in c.get('Networks',[]) if n.get('IPv4Addresses')))" 2>/dev/null || true)
fi
case "$HOST_I" in
  ""|127.*) ADDR="$META_IP" ;;
  *)        ADDR="$HOST_I" ;;
esac

if [ "$ROLE" = "server" ] && [ -z "$NG_SERVER_NAME" ]; then export NG_SERVER_NAME="$ADDR"; fi
if [ "$ROLE" = "guard" ]  && [ -z "$GUARD_EXTERNAL_IP" ]; then export GUARD_EXTERNAL_IP="$ADDR"; fi

echo "{\"event\":\"address_resolved\",\"role\":\"$ROLE\",\"hostname\":\"$(hostname)\",\"hostname_i\":\"$HOST_I\",\"metadata_ip\":\"$META_IP\",\"ng_server_name\":\"$NG_SERVER_NAME\",\"guard_external_ip\":\"$GUARD_EXTERNAL_IP\",\"match\":\"$([ "$HOST_I" = "$META_IP" ] && echo yes || echo no)\"}"

exec python /app/app.py

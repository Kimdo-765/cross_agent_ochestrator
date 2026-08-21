#!/usr/bin/env bash
# Start the cao web UI on a free local port and (optionally) expose it through a Cloudflare tunnel.
#
#   ./scripts/start.sh                # docker compose: web + cloudflared quick tunnel
#   ./scripts/start.sh --no-tunnel    # docker compose: web only (localhost)
#   ./scripts/start.sh --native       # no docker: run `cao web --tunnel` from this checkout
#   ./scripts/start.sh --stop         # stop the compose stack
#
# Reads .env (see .env.example). Prints the local URL and the tunnel URL once it is up.
set -euo pipefail
cd "$(dirname "$0")/.."

MODE=docker
TUNNEL=1
for arg in "$@"; do
  case "$arg" in
    --native) MODE=native ;;
    --no-tunnel) TUNNEL=0 ;;
    --stop) docker compose --profile tunnel --profile tunnel-named down; exit 0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

[ -f .env ] && set -a && . ./.env && set +a

free_port() {
  python3 - "$@" <<'PY'
import socket, sys
pref = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] else 0
for cand in ([pref] if pref else []) + [0]:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", cand)); print(s.getsockname()[1]); break
    except OSError:
        continue
    finally:
        s.close()
PY
}

if [ "$MODE" = native ]; then
  command -v cao >/dev/null || pip install -e ".[web]"
  ARGS=(--no-open)
  [ "$TUNNEL" = 1 ] && ARGS+=(--tunnel)
  exec cao web --port "${CAO_HOST_PORT:-0}" "${ARGS[@]}"
fi

command -v docker >/dev/null || { echo "docker not found. Use --native, or install Docker (Desktop + WSL integration on Windows)." >&2; exit 1; }
export CAO_HOST_PORT="$(free_port "${CAO_HOST_PORT:-}")"
export CAO_WORKSPACE="${CAO_WORKSPACE:-./workspace}"
mkdir -p "$CAO_WORKSPACE" "$HOME/.claude" "$HOME/.codex" "$HOME/.ssh"   # bind-mount sources must exist

PROFILES=()
TUNNEL_SVC=cloudflared
if [ "$TUNNEL" = 1 ]; then
  if [ -n "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]; then PROFILES=(--profile tunnel-named); TUNNEL_SVC=cloudflared-named
  else PROFILES=(--profile tunnel); fi
fi
docker compose "${PROFILES[@]}" up -d --build

echo
echo "cao web UI (local):  http://127.0.0.1:${CAO_HOST_PORT}"
TOKEN="${CAO_AUTH_TOKEN:-}"
if [ -z "$TOKEN" ]; then
  for _ in $(seq 1 30); do
    TOKEN="$(docker compose logs web 2>/dev/null | grep -oE 'access token: [A-Za-z0-9_-]+' | tail -1 | awk '{print $3}' || true)"
    [ -n "$TOKEN" ] && break
    sleep 1
  done
fi
[ -n "$TOKEN" ] && echo "access token:        $TOKEN   (sign in: http://127.0.0.1:${CAO_HOST_PORT}/login?token=$TOKEN)"
if [ "$TUNNEL" = 1 ]; then
  if [ -n "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]; then
    echo "cloudflare tunnel:   named tunnel (${CAO_TUNNEL_URL:-see your Cloudflare dashboard})"
  else
    echo -n "cloudflare tunnel:   waiting for URL"
    for _ in $(seq 1 60); do
      URL="$(docker compose "${PROFILES[@]}" logs "$TUNNEL_SVC" 2>/dev/null | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 || true)"
      if [ -n "$URL" ]; then
        echo; echo "cloudflare tunnel:   $URL"
        [ -n "$TOKEN" ] && echo "remote sign-in:      $URL/login?token=$TOKEN"
        break
      fi
      echo -n "."; sleep 1
    done
    [ -z "${URL:-}" ] && echo " (not yet; run: docker compose ${PROFILES[*]} logs -f $TUNNEL_SVC)"
  fi
fi
echo
echo "logs:  docker compose ${PROFILES[*]:-} logs -f"
echo "stop:  ./scripts/start.sh --stop"

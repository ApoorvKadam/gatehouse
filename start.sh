#!/usr/bin/env bash
# One container, one public port. The FMS simulator listens on loopback only; Gatehouse and its
# reconciliation worker listen on $PORT. If either process stops, the other is stopped too and the
# container exits, so the host restarts the whole thing instead of serving half a system.
set -uo pipefail

port="${PORT:-8000}"

# The public preview resets the simulator when it builds its synthetic world, so it gets its own simulator
# database and never touches one kept on a volume.
if [ "${DESK_PUBLIC_VIEW:-0}" = "1" ]; then
  SIM_DATABASE_URL="sqlite:///$(mktemp -d)/fmssim.db"
  export SIM_DATABASE_URL
fi

uvicorn fmssim.main:app --host 127.0.0.1 --port 8100 --log-level warning &
sim=$!
trap 'kill -TERM "$sim" ${desk:-} 2>/dev/null' TERM INT

# Gatehouse checks which FMS account it belongs to at startup, so the simulator has to answer first.
# A slow free instance can take more than a minute to import and seed.
for _ in $(seq 1 180); do
  (exec 3<>/dev/tcp/127.0.0.1/8100) 2>/dev/null && break
  kill -0 "$sim" 2>/dev/null || { echo "simulator exited during startup" >&2; exit 1; }
  sleep 1
done

uvicorn desk.main:app --host 0.0.0.0 --port "$port" --proxy-headers --forwarded-allow-ips '*' &
desk=$!

wait -n "$sim" "$desk"
status=$?
kill -TERM "$sim" "$desk" 2>/dev/null
wait
exit "$status"

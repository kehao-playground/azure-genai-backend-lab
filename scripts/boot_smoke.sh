#!/usr/bin/env bash
# Boot smoke test for a built azgenai-lab image: proves the image not only
# builds but runs. A build that succeeds is not proof of that -- this
# milestone's worst bug was exactly a build that succeeded and an image
# that would not start with zero configuration, the same way the Quick
# start in docs/docker.md runs it (Day 23 review).
#
# Two different assertions, both needed (Day 23 review r03 R3): polling
# `docker inspect` proves the Dockerfile's own declared HEALTHCHECK --
# its command, URL, user and flags -- actually reports healthy; a
# separate `docker exec` probe with no involvement from HEALTHCHECK at
# all would pass even if that instruction were broken (confirmed by
# deliberately pointing HEALTHCHECK at the wrong port: the exec probe
# below still succeeds while the inspect poll goes unhealthy -- Day 23
# review r03 R3, second wave). The exact-body `docker exec` check
# afterwards proves what /health actually returns, against the
# container's own loopback instead of a published port, so this never
# needs a port on the runner.
#
# Usage: scripts/boot_smoke.sh <image-ref>
set -euo pipefail

IMAGE="${1:?usage: scripts/boot_smoke.sh <image-ref>}"

BOOT_SMOKE_ATTEMPTS="${BOOT_SMOKE_ATTEMPTS:-60}"
BOOT_SMOKE_INTERVAL_SECONDS="${BOOT_SMOKE_INTERVAL_SECONDS:-2}"

if [ -z "${BOOT_SMOKE_CONTAINER_NAME:-}" ]; then
  # Cryptographically unpredictable, not $RANDOM (seeded, guessable) and not
  # a timestamp -- two runs of this script on the same host must not pick
  # the same container name, and cleanup below must key on the name this
  # run actually chose, never on a name it merely guessed.
  SUFFIX=$(od -An -vN4 -tx1 /dev/urandom | tr -d " \n") \
    || {
      echo "Failed to read random bytes for the per-run container-name suffix; aborting rather than falling back to a predictable value." >&2
      exit 1
    }
  BOOT_SMOKE_CONTAINER_NAME="azgenai-lab-boot-smoke-${SUFFIX}"
fi
echo "boot smoke container: $BOOT_SMOKE_CONTAINER_NAME"

trap 'docker rm -f "$BOOT_SMOKE_CONTAINER_NAME" >/dev/null 2>&1 || true' EXIT
docker run -d --name "$BOOT_SMOKE_CONTAINER_NAME" "$IMAGE"

# The image's HEALTHCHECK has --start-period=5s and an unset (so
# default 5s) --start-interval, meaning the first probe fires
# around 5s in and resolves to healthy within the first couple of
# attempts on the happy path. A genuinely broken HEALTHCHECK is a
# slower story: start-period failures don't count toward
# --retries=3, so it takes one start-period probe plus three
# --interval=30s retries -- on the order of 95-100s -- before
# Docker reports unhealthy. The default 60 attempts at 2s (120s)
# covers that with some room, not a generous margin.
status=unknown
for attempt in $(seq 1 "$BOOT_SMOKE_ATTEMPTS"); do
  if ! status="$(docker inspect --format '{{.State.Health.Status}}' "$BOOT_SMOKE_CONTAINER_NAME" 2>&1)"; then
    echo "docker inspect failed on attempt $attempt: $status" >&2
    status="inspect-error"
    break
  fi
  if [ "$status" = healthy ] || [ "$status" = unhealthy ]; then
    break
  fi
  sleep "$BOOT_SMOKE_INTERVAL_SECONDS"
done
if [ "$status" != healthy ]; then
  echo "container HEALTHCHECK never reported healthy (status: $status); docker inspect health output and container log follow" >&2
  docker inspect --format '{{json .State.Health}}' "$BOOT_SMOKE_CONTAINER_NAME" >&2 || true
  docker logs "$BOOT_SMOKE_CONTAINER_NAME" >&2
  exit 1
fi
echo "HEALTHCHECK status: $status"

body="$(docker exec "$BOOT_SMOKE_CONTAINER_NAME" python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read().decode())")"
echo "health body: $body"
test "$body" = '{"status":"ok","service":"azure-genai-backend-lab"}'

#!/usr/bin/env bash
# Run a command on disposable synthetic PostgreSQL; embedding calls stay on the host.
set -euo pipefail
fixture_dir=$(mktemp -d /tmp/core-pg.XXXXXXXX)
container_name="core-pg-${fixture_dir##*.}"
image='postgres:18-bookworm@sha256:3725f4e2499eef5134592b3b4ab79a543ed7f8e533b05b5b637af926630f6650'
cleanup() {
  docker rm --force "$container_name" >/dev/null 2>&1 || true
  # Only the two socket files in this invocation's private temporary directory.
  rm -f -- "$fixture_dir/socket/.s.PGSQL.5432" "$fixture_dir/socket/.s.PGSQL.5432.lock"
  rmdir -- "$fixture_dir/socket" "$fixture_dir"
}
trap cleanup EXIT
mkdir -m 700 "$fixture_dir/socket"
docker run --detach --name "$container_name" --network none --cpus 1 --memory 512m \
  --user "$(id -u):$(id -g)" \
  --tmpfs "/var/lib/postgresql:uid=$(id -u),gid=$(id -g),mode=0700" \
  --mount "type=bind,source=$fixture_dir/socket,target=/var/run/postgresql" \
  --env POSTGRES_HOST_AUTH_METHOD=trust --env POSTGRES_DB=core_synthetic \
  --env POSTGRES_USER=core_synthetic "$image" \
  postgres -c listen_addresses= -c unix_socket_permissions=0700 >/dev/null
ready=false
for attempt in {1..50}; do
  # PID 1 becomes postgres only after entrypoint's temporary init server exits.
  if docker exec "$container_name" sh -c 'test "$(cat /proc/1/comm)" = postgres' \
    && docker exec "$container_name" pg_isready -q -U core_synthetic -d core_synthetic; then
    ready=true
    break
  fi
  sleep 1
done
if [ "$ready" != true ]; then
  docker logs "$container_name"
  exit 1
fi
export DSC_TEST_POSTGRES_SOCKET="$fixture_dir/socket"
export DSC_TEST_POSTGRES_PORT=5432
export DSC_TEST_POSTGRES_DATABASE=core_synthetic
export DSC_TEST_POSTGRES_USER=core_synthetic
export LITELLM_LOCAL_MODEL_COST_MAP=True
"$@"

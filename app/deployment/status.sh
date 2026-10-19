#!/bin/sh

set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
# shellcheck source=deployment/common.sh
. "$script_dir/common.sh"
select_target "${1:?usage: $0 dev|prod}"

compose ps
http_port=$(sed -n 's/^HTTP_PORT=//p' "$env_file")
case "$http_port" in
  ''|*[!0-9]*)
    echo "error: HTTP_PORT in $env_file is not a port number" >&2
    exit 1
    ;;
esac
curl --fail --silent --show-error "http://127.0.0.1:$http_port/health/ready"
printf '\n'

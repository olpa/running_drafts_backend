#!/bin/sh

set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
# shellcheck source=deployment/common.sh
. "$script_dir/common.sh"
select_target "${1:?usage: $0 dev|prod}"

compose run --rm --no-deps db-admin migrate

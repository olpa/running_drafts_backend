#!/bin/sh

set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
# shellcheck source=deployment/common.sh
. "$script_dir/common.sh"
select_target "${1:?usage: $0 dev|prod}"

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_name="running-drafts-$target-$timestamp.sqlite3"
compose run \
  --rm \
  --no-deps \
  --entrypoint python \
  --env "BACKUP_PATH=/backups/$backup_name" \
  db-admin \
  -c 'import os, sqlite3; source = sqlite3.connect("file:/data/running-drafts.sqlite3?mode=ro", uri=True); destination = sqlite3.connect(os.environ["BACKUP_PATH"]); source.backup(destination); destination.close(); source.close()'
echo "$deployment_root/backups-$target/$backup_name"

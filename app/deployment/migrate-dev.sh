#!/bin/sh
exec "$(dirname -- "$0")/migrate.sh" dev "$@"

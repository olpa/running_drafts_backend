#!/bin/sh
exec "$(dirname -- "$0")/backup.sh" dev "$@"

#!/bin/sh
exec "$(dirname -- "$0")/status.sh" prod "$@"

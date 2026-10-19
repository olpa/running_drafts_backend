#!/bin/sh
exec "$(dirname -- "$0")/logs.sh" dev "$@"

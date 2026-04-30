#!/usr/bin/env bash
set -euo pipefail
mkdir -p "${ZG_WORKSPACE:-/var/zerograph/ws}/repos" "${ZG_WORKSPACE:-/var/zerograph/ws}/graphs"
exec tail -f /dev/null

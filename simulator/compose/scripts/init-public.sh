#!/usr/bin/env sh
set -eu

mkdir -p /public/home/alice /public/home/bob /public/app /pilot107/evidence-derived

if id alice >/dev/null 2>&1; then
  chown alice:alice /public/home/alice
fi

if id bob >/dev/null 2>&1; then
  chown bob:bob /public/home/bob
fi

chmod 0700 /public/home/alice /public/home/bob
chmod 0755 /public/app

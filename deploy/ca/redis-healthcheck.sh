#!/bin/sh
set -eu
IFS= read -r REDISCLI_AUTH < /run/secrets/redis.password
export REDISCLI_AUTH
redis-cli --raw PING | grep -qx PONG

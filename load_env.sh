#!/bin/sh
# Load API credentials for kolaBitMEXBot
# Usage: ./load_env.sh [prod|dev]
FILE=".env-dev"
[ "$1" = "prod" ] && FILE=".env-prod"
if [ -f "$FILE" ]; then
  set -a
  . "$FILE"
  set +a
else
  echo "Environment file $FILE not found" >&2
  exit 1
fi

#!/bin/bash
# Load environment variables for kolaBot
# Usage: source env.sh [prod|dev]
ENV_FILE=".env-dev"
if [ "$1" = "prod" ]; then
  ENV_FILE=".env-prod"
fi
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
else
  echo "Missing $ENV_FILE" >&2
fi

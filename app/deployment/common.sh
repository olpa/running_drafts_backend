#!/bin/sh

set -eu

deployment_root=/rnotes/RUNNING_DRAFTS_BACKEND
source_root=/mnt/HC_Volume_103849597/home/olpa/running_drafts_backend/app
compose_file="$source_root/compose.deployment.yaml"

select_target() {
  target=${1:?usage: $0 dev|prod}
  case "$target" in
    dev)
      compose_project=running-drafts-backend-dev
      ;;
    prod)
      compose_project=running-drafts-backend-prod
      ;;
    *)
      echo "error: target must be dev or prod" >&2
      exit 2
      ;;
  esac
  env_file="$deployment_root/.env.$target"

  if [ ! -f "$compose_file" ]; then
    echo "error: deployment Compose file not found: $compose_file" >&2
    exit 1
  fi
  if [ ! -f "$env_file" ]; then
    echo "error: target environment file not found: $env_file" >&2
    exit 1
  fi
  if [ "${IMAGE_TAG+x}" = x ]; then
    echo "error: IMAGE_TAG must be selected only in $env_file" >&2
    exit 1
  fi
}

compose() {
  docker compose \
    --project-name "$compose_project" \
    --project-directory "$deployment_root" \
    --env-file "$env_file" \
    --file "$compose_file" \
    "$@"
}

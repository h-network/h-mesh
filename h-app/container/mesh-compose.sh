#!/usr/bin/env bash
# Resolve the complete tenant-specific compose file list. Source this file.
#
#   . container/mesh-compose.sh
#   resolve_compose_files hq      # populates tenant context and file list
#   COMPOSE=(docker compose -p "$MESH_COMPOSE_PROJECT" --env-file "$MESH_TENANT_ENV_PATH")
#   for file in "${MESH_COMPOSE_FILES[@]}"; do COMPOSE+=(-f "$file"); done
#   "${COMPOSE[@]}" ...
# The base has no ports; optional publication and mini-app fragments must be
# included consistently by every caller.

MESH_REPO_ROOT="${MESH_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

resolve_tenant_context() {
  local tenant="${1:-}"
  if [[ ! "$tenant" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] \
     || [[ "$tenant" =~ ^[0-9]+$ ]]; then
    echo "mesh-compose: invalid tenant '$tenant'" >&2
    return 2
  fi
  case "$tenant" in pod|tenant|agent|all)
    echo "mesh-compose: reserved tenant '$tenant'" >&2
    return 2
  esac

  TENANT="$tenant"
  MESH_TENANT_DIR="$MESH_REPO_ROOT/tenants/$tenant"
  MESH_TENANT_ENV_PATH="$MESH_TENANT_DIR/.env"
  MESH_COMPOSE_PROJECT="h-mesh-$tenant"
  MESH_TENANT_CONTAINER="$MESH_COMPOSE_PROJECT-tenant-1"
  export TENANT MESH_TENANT_DIR MESH_TENANT_ENV_PATH MESH_COMPOSE_PROJECT MESH_TENANT_CONTAINER
}

resolve_compose_files() {
  resolve_tenant_context "${1:-${TENANT:-}}" || return
  MESH_COMPOSE_FILES=("$MESH_REPO_ROOT/container/compose.yaml")
  if [ -f "$MESH_TENANT_DIR/compose.ports.yaml" ]; then
    MESH_COMPOSE_FILES+=("$MESH_TENANT_DIR/compose.ports.yaml")
  fi
  if [ -f "$MESH_TENANT_DIR/compose.mini-app.yaml" ]; then
    MESH_COMPOSE_FILES+=("$MESH_TENANT_DIR/compose.mini-app.yaml")
  fi
}

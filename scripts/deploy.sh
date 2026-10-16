#!/usr/bin/env bash
# Deploy the framework to one target: bootstrap the governance objects, then deploy the bundle.
#
#   scripts/deploy.sh <target> <warehouse_id> [profile]
#
# In CI, omit the profile and authenticate with DATABRICKS_HOST plus OAuth M2M
# (DATABRICKS_CLIENT_ID / DATABRICKS_CLIENT_SECRET). Override any bundle variable with
# BUNDLE_VAR_<name> environment variables.
set -euo pipefail

TARGET="${1:?target (dev|qa|prod) required}"
WAREHOUSE_ID="${2:?SQL warehouse id required}"
PROFILE="${3:-}"
PROFILE_ARGS=()
[[ -n "$PROFILE" ]] && PROFILE_ARGS=(-p "$PROFILE")

cd "$(dirname "$0")/.."

echo "==> Validating bundle (target: $TARGET)"
databricks bundle validate --strict -t "$TARGET" "${PROFILE_ARGS[@]}"

echo "==> Bootstrapping governance schema, Volume, tables, and constraints"
uv run --quiet python tools/bootstrap.py -t "$TARGET" --warehouse-id "$WAREHOUSE_ID" "${PROFILE_ARGS[@]}"

echo "==> Deploying bundle"
databricks bundle deploy -t "$TARGET" "${PROFILE_ARGS[@]}"

echo "Done. The setup job (<env>_phi_framework_setup) can be rerun at any time; it is idempotent."

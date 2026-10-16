# Declarative Automation Bundles Project

This project uses Declarative Automation Bundles (formerly Databricks Asset Bundles) for deployment.

## Prerequisites

Install the Databricks CLI (>= v0.288.0) if not already installed:
- macOS: `brew tap databricks/tap && brew install databricks`
- Linux: `curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh`
- Windows: `winget install Databricks.DatabricksCLI`

Verify: `databricks -v`

## For AI Agents

Read the `databricks-core` skill for CLI basics, authentication, and deployment workflow.
Read the `databricks-jobs` skill for job-specific guidance.

If skills are not available, install them: `databricks aitools install`

## Project rules

- Deploy with `scripts/deploy.sh <target> <warehouse_id> [profile]`, not bare `bundle deploy`
  (triggered jobs need the Volume and plan table to exist first).
- Controlled values live in `src/phi_framework/constants.py` and are rendered into CHECK
  constraints by `ddl.py`; change both sides together and rerun setup.
- Never write to governance tables outside the framework jobs, except the scope registry and
  suggestion mapping (via `tools/load_reference_data.py`).
- Run `uv run --with pytest pytest` and `uv run ruff check src tests tools` before committing.

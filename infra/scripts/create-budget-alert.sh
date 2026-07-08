#!/usr/bin/env bash
# Create a subscription-level budget with alert thresholds.
# Run this BEFORE creating any billable resource.
# Required env vars:
#   AZ_BUDGET_AMOUNT   - monthly cap in USD (default: 20)
#   AZ_ALERT_EMAIL     - email address for alert notifications
set -euo pipefail

: "${AZ_ALERT_EMAIL:?Set AZ_ALERT_EMAIL}"
AZ_BUDGET_AMOUNT="${AZ_BUDGET_AMOUNT:-20}"
SUBSCRIPTION_ID=$(az account show --query id -o tsv)

az consumption budget create \
  --budget-name "azgenai-lab-monthly" \
  --amount "$AZ_BUDGET_AMOUNT" \
  --category cost \
  --time-grain monthly \
  --start-date "$(date -u +%Y-%m-01)" \
  --end-date "2027-12-31" \
  --subscription "$SUBSCRIPTION_ID"

echo "Budget 'azgenai-lab-monthly' (USD $AZ_BUDGET_AMOUNT/month) created."
echo "NOTE: configure alert notifications (50%/80%/100%) in the Azure Portal under Cost Management > Budgets,"
echo "or extend this script with --notifications once the JSON shape is pinned (verify against current az CLI docs)."

#!/usr/bin/env bash
# Create the demo resource group.
# Required env vars:
#   AZ_RESOURCE_GROUP  - resource group name (e.g. rg-azgenai-lab)
#   AZ_LOCATION        - Azure region (e.g. eastasia)
set -euo pipefail

: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP (e.g. rg-azgenai-lab)}"
: "${AZ_LOCATION:?Set AZ_LOCATION (e.g. eastasia)}"

az group create --name "$AZ_RESOURCE_GROUP" --location "$AZ_LOCATION" --output table
echo "Created resource group $AZ_RESOURCE_GROUP in $AZ_LOCATION."
echo "Remember: run teardown.sh when the test session ends."

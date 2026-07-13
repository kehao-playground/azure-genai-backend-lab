# Scripts

| Script | Purpose | Status |
|---|---|---|
| `create-budget-alert.sh` | Subscription budget (US$20) with alerts — run first | working |
| `create-resource-group.sh` | Create the demo resource group | working |
| `create-openai.sh` | Azure OpenAI account + mini-model deployment (token-billed, may persist) | working |
| `delete-openai.sh` | Delete and purge the Azure OpenAI account | working |
| `teardown.sh` | Delete the demo resource group and everything in it | working |
| `deploy-container-app.sh` | Deploy to Azure Container Apps | placeholder (Day 24) |
| `configure-apim.sh` | APIM Consumption tier setup | placeholder (Day 10) |

All scripts read configuration from environment variables, fail fast, and never hardcode subscription IDs or secrets.

Every script requires `AZ_SUBSCRIPTION_ID` and passes `--subscription` explicitly on each az call. The default az context is shared mutable state — an `az login` in another terminal can silently repoint it, which is exactly how you delete resources in the wrong subscription.

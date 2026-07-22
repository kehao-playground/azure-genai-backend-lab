# Infra

Azure CLI scripts first, Bicep later — see the infra evolution article (Day 26).

**Cost policy (binding):** this is a self-funded lab with a US$20/month self-imposed ceiling — enforced by discipline and teardown scripts, not by Azure (the budget alert only notifies; it never stops resources or consumption). Demo resources are ephemeral by default — every create script has a matching teardown path, and `create-budget-alert.sh` must run before the first billable resource is created. Azure AI Search is only created for test sessions and deleted afterward. APIM uses the Consumption tier only.

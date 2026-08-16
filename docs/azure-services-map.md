# Azure Services Map

Placeholder — engineering needs mapped to Azure services, expanded alongside the series.

| Engineering need | Azure service | Since |
|---|---|---|
| Model inference (Responses API, v1 GA endpoint) | Azure OpenAI in Microsoft Foundry | Day 4 |
| Spend detection backstop (delayed notification, not a cap) | Azure Cost Management budget alert | Day 4 |
| Credential custody + per-client throttling for the model endpoint | Azure API Management (Consumption tier) — see [api-management.md](api-management.md) | Day 10 |
| Document retrieval (hybrid search, vector + BM25 + RRF) | Azure AI Search | Day 11 |
| Text embeddings for indexing and query vectors | Azure OpenAI embeddings deployment | Day 12 |
| Caller identity verification (OIDC discovery, JWKS, 401/403 contract) | Microsoft Entra ID — see [entra-id-auth.md](entra-id-auth.md) | Day 19 |
| Secret custody for credentials that must remain secrets | Azure Key Vault — see [key-vault-config.md](key-vault-config.md) | Day 20 |
| Keyless data-plane auth (no API key to store, rotate, or leak) | Managed identity + Azure RBAC — see [managed-identity.md](managed-identity.md) | Day 20 |
| Container runtime with HTTPS ingress, revisions and usage-based scaling | Azure Container Apps — see [container-apps.md](container-apps.md) | Day 24 |
| Image storage the runtime can pull from with an identity, not credentials | Azure Container Registry (Basic, ephemeral) — see [container-apps.md](container-apps.md) | Day 24 |

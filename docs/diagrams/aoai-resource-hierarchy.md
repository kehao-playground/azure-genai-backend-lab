# Azure OpenAI Resource Hierarchy

How the names in your config map to Azure OpenAI's structure (Day 4).
The deployment name is the only indirection you control: your code points at it,
it points at a model version.

```mermaid
flowchart TB
    subgraph Sub["Subscription"]
        subgraph Res["Resource: aoai-&lt;project&gt; (kind=OpenAI)"]
            direction TB
            Meta["endpoint: https://&lt;name&gt;.openai.azure.com/<br/>region: japaneast (fixed at creation)<br/>keys / RBAC"]
            subgraph Dep["Deployment: chat-mini (name chosen by you)"]
                Model["Model: gpt-5-mini (version 2025-08-07)<br/>SKU: GlobalStandard · capacity: 50K TPM"]
            end
        end
    end

    App["Your backend config<br/>AZURE_OPENAI_ENDPOINT<br/>AZURE_OPENAI_DEPLOYMENT_NAME"]
    App -- "base_url = endpoint + /openai/v1/" --> Meta
    App -- "model = 'chat-mini' (deployment, not model name)" --> Dep

    Quota["Quota pool: subscription × region × model<br/>(all deployments in a region share it)"]
    Dep -.-> Quota
```

Solid arrows: what your config points at. Dotted arrow: where the TPM quota is drawn from.

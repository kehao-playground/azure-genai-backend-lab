# Azure OpenAI Resource Hierarchy

How the names in your config map to Azure OpenAI's structure (Day 4).
The deployment name is the indirection between application runtime and the
model version: your code points at it, it points at a model.

```mermaid
flowchart TB
    subgraph Sub["Subscription"]
        direction TB
        Quota["Quota pool: region × model × deployment type<br/>deployments allocate TPM from it at creation"]
        subgraph Res["Resource: aoai-&lt;project&gt; (kind=OpenAI)"]
            direction TB
            Meta["endpoint: https://&lt;name&gt;.openai.azure.com/<br/>region: japaneast (fixed at creation)<br/>keys / RBAC"]
            subgraph Dep["Deployment: chat-mini (name chosen by you)"]
                Model["Model: gpt-5-mini (version 2025-08-07)<br/>type: GlobalStandard"]
            end
        end
    end

    App["Your backend config<br/>AZURE_OPENAI_ENDPOINT<br/>AZURE_OPENAI_DEPLOYMENT_NAME"]
    App -- "base_url = endpoint + /openai/v1/" --> Meta
    App -- "model = 'chat-mini' (deployment, not model name)" --> Dep

    Dep -. "allocates 50K TPM from" .-> Quota
```

Solid arrows: what your config points at. Dotted arrow: capacity allocated at
deployment creation (the sum across deployments in the same scope cannot exceed
the pool). The pool belongs to the subscription, not to one resource —
deployments in other resources in the same region/model/type draw from it too.

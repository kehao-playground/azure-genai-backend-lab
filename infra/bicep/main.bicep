// The Azure OpenAI account and its two model deployments, declared.
//
// This is a translation of infra/scripts/create-openai.sh: the same resources
// with the same properties, and nothing else. It takes *provisioning*
// ownership only. `az deployment group create` can create or update these
// resources; tearing them down stays with delete-openai.sh, because a deleted
// Cognitive Services account is soft-deleted and must then be purged, and
// purge is not something a template can express. infra/bicep/README.md
// explains where that line comes from and what would have to be true to move
// it.
//
// API version: for Microsoft.CognitiveServices/accounts, 2026-05-01 is the
// newest *stable* version for which Bicep CLI 0.46.1 ships types (checked
// 2026-08-20 against the provider's version list for that resource type;
// 2026-05-15-preview is newer and also typed, but this series pins stable
// versions). That "newest typed stable" ordering is an accounts-only claim:
// the child type accounts/deployments has no provider-advertised version list
// of its own (the provider manifest does not list it -- checked 2026-08-21).
// What is verified for the child type is narrower: a per-version sweep found
// six untyped versions, 2026-05-01 not among them, and this file builds with
// zero diagnostics. The two newest versions the provider advertises for
// accounts -- 2026-07-15-preview and 2026-07-01 -- have no types in this
// toolchain. That matters more than it looks: an untyped version still
// compiles, with BCP081 and no property checking whatsoever, which throws away
// the reason to write this file instead of the script.

targetScope = 'resourceGroup'

@description('Azure OpenAI account name; also used as the custom subdomain. Script: AZ_OPENAI_NAME.')
param openAiName string

@description('Region. Script: AZ_LOCATION. The series uses japaneast.')
param location string = resourceGroup().location

@description('Chat deployment name. Script: AZ_OPENAI_DEPLOYMENT.')
param chatDeploymentName string = 'chat-mini'

@description('Chat model name. Script: AZ_OPENAI_MODEL.')
param chatModelName string = 'gpt-5-mini'

@description('Chat model version. Script: AZ_OPENAI_MODEL_VERSION.')
param chatModelVersion string = '2025-08-07'

// create-openai.sh refuses to run if this equals the chat deployment name,
// because `az ... deployment create` is an upsert: the second call would
// reconfigure the first deployment to serve the embedding model while both
// success lines still printed. That guard does *not* carry over here. Bicep
// rejects two resources sharing a name only when the names are literals
// (BCP121); when both come from parameters, as they do below, the build is
// clean -- verified locally 2026-08-21 with Bicep CLI 0.46.1. What ARM does at
// deployment time with two identical child identities is untested here.
@description('Embedding deployment name. Script: AZ_EMBED_DEPLOYMENT. Must differ from chatDeploymentName.')
param embedDeploymentName string = 'embed-small'

@description('Embedding model name. Script: AZ_EMBED_MODEL.')
param embedModelName string = 'text-embedding-3-small'

@description('Embedding model version. Script: AZ_EMBED_MODEL_VERSION.')
param embedModelVersion string = '1'

@description('Deployment SKU for both deployments. Script: AZ_OPENAI_SKU.')
param deploymentSku string = 'GlobalStandard'

// The script has two capacity variables, not one, so a single run can give the
// chat and embedding deployments different quota. Collapsing them into one
// parameter here would be a quieter template that no longer says what the
// script says.
@description('Chat deployment capacity in K TPM. Script: AZ_OPENAI_CAPACITY.')
param chatCapacity int = 50

@description('Embedding deployment capacity in K TPM. Script: AZ_EMBED_CAPACITY.')
param embedCapacity int = 50

resource account 'Microsoft.CognitiveServices/accounts@2026-05-01' = {
  name: openAiName
  location: location
  kind: 'OpenAI'
  sku: {
    name: 'S0'
  }
  properties: {
    // `az cognitiveservices account create --custom-domain` sets this. The
    // keyless (Entra) path needs a custom subdomain, so it is not optional
    // here -- see docs/managed-identity.md.
    customSubDomainName: openAiName
  }
}

resource chat 'Microsoft.CognitiveServices/accounts/deployments@2026-05-01' = {
  parent: account
  name: chatDeploymentName
  sku: {
    name: deploymentSku
    capacity: chatCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: chatModelName
      version: chatModelVersion
    }
  }
}

resource embed 'Microsoft.CognitiveServices/accounts/deployments@2026-05-01' = {
  parent: account
  name: embedDeploymentName
  sku: {
    name: deploymentSku
    capacity: embedCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: embedModelName
      version: embedModelVersion
    }
  }
  // ARM deploys independent resources in parallel, and these two are
  // independent as far as the template can tell. The scripts have always
  // written these deployments one after the other, and this keeps that shape
  // rather than discovering what the control plane does with two concurrent
  // writes to the same account. Not verified by deployment from this repo.
  dependsOn: [
    chat
  ]
}

// The script prints this at the end, so the template returns it.
output endpoint string = account.properties.endpoint

// Deliberately not returned: the account keys. `listKeys()` would work, but a
// template output is stored in the deployment history, and the keyless path in
// docs/managed-identity.md is where this series is going anyway.

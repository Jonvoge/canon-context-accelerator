#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Deploy Canon MCP server and Teams bot to Azure Container Apps.

.DESCRIPTION
    1. Create resource group, ACR, and Container Apps environment (idempotent)
    2. Build and push both images via ACR Tasks (no local Docker required)
    3. Create or update the Container Apps

.EXAMPLE
    .\deploy.ps1 -ResourceGroup rg-canon -Location northeurope
#>

[CmdletBinding()]
param(
    [string]$ResourceGroup  = "",
    [string]$Location       = "",
    [string]$AcrName        = "",
    [string]$AcaEnv         = "",
    [string]$McpApp         = "",
    [string]$BotApp         = "",
    [string]$SubscriptionId = "",
    [switch]$SkipBuild,
    [switch]$McpOnly,
    [switch]$BotOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Fix Windows cp1252 encoding crash in az CLI log streaming
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# Load .env file if present (never committed — secrets live here)
$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
            $name  = $Matches[1].Trim()
            $value = $Matches[2].Trim().Trim('"').Trim("'")
            [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
    Write-Host "Loaded .env"
}

# Apply parameter defaults after .env is loaded (PS5.1 has no ?? in param blocks)
if (-not $ResourceGroup)  { $ResourceGroup  = if ($env:AZURE_RESOURCE_GROUP) { $env:AZURE_RESOURCE_GROUP } else { "rg-canon" } }
if (-not $Location)       { $Location       = if ($env:AZURE_LOCATION)       { $env:AZURE_LOCATION }       else { "northeurope" } }
if (-not $AcrName)        { $AcrName        = if ($env:AZURE_ACR_NAME)        { $env:AZURE_ACR_NAME }        else { "jvcanonacr" } }
if (-not $AcaEnv)         { $AcaEnv         = if ($env:CANON_ACA_ENV)         { $env:CANON_ACA_ENV }         else { "canon-env" } }
if (-not $McpApp)         { $McpApp         = if ($env:CANON_MCP_APP)         { $env:CANON_MCP_APP }         else { "canon-mcp" } }
if (-not $BotApp)         { $BotApp         = if ($env:CANON_BOT_APP)         { $env:CANON_BOT_APP }         else { "canon-bot" } }
if (-not $SubscriptionId) { $SubscriptionId = $env:AZURE_SUBSCRIPTION_ID }

# ─── Helpers ──────────────────────────────────────────────────────────────────
function Write-Step([string]$msg) {
    Write-Host "`n==> $msg" -ForegroundColor Cyan
}

function Require-EnvVar([string]$name) {
    $val = [System.Environment]::GetEnvironmentVariable($name)
    if (-not $val) { throw "Required env var '$name' is not set." }
    return $val
}

# ─── Validate required secrets ────────────────────────────────────────────────
$tenantId        = Require-EnvVar "CANON_FABRIC_TENANT_ID"
$clientId        = Require-EnvVar "CANON_FABRIC_CLIENT_ID"
$clientSecret    = Require-EnvVar "CANON_FABRIC_CLIENT_SECRET"
$workspaceId     = Require-EnvVar "CANON_FABRIC_WORKSPACE_ID"
$datasetName     = if ($env:CANON_FABRIC_DATASET_NAME) { $env:CANON_FABRIC_DATASET_NAME } else { "" }
$sqlServer       = if ($env:CANON_SQL_SERVER)           { $env:CANON_SQL_SERVER }           else { "" }
$sqlDatabase     = if ($env:CANON_SQL_DATABASE)         { $env:CANON_SQL_DATABASE }         else { "" }
$anthropicKey    = Require-EnvVar "ANTHROPIC_API_KEY"
$msAppId         = Require-EnvVar "MICROSOFT_APP_ID"
$msAppPassword   = Require-EnvVar "MICROSOFT_APP_PASSWORD"
$githubRepo      = Require-EnvVar "CANON_GITHUB_REPO"
$githubToken     = Require-EnvVar "CANON_GITHUB_TOKEN"

if ($SubscriptionId) {
    az account set --subscription $SubscriptionId
}

# ─── Resource group ───────────────────────────────────────────────────────────
Write-Step "Resource group: $ResourceGroup ($Location)"
az group create --name $ResourceGroup --location $Location | Out-Null

# ─── ACR ──────────────────────────────────────────────────────────────────────
Write-Step "Container Registry: $AcrName"
$acrExists = az acr show --name $AcrName --resource-group $ResourceGroup 2>$null
if (-not $acrExists) {
    az acr create --name $AcrName --resource-group $ResourceGroup `
        --location $Location --sku Basic --admin-enabled true | Out-Null
}
$acrLoginServer = az acr show --name $AcrName --resource-group $ResourceGroup --query "loginServer" -o tsv 2>$null
if (-not $acrLoginServer) {
    throw "ACR '$AcrName' not found in resource group '$ResourceGroup'. Check the name and try again."
}
Write-Host "ACR: $acrLoginServer"

# ─── Container Apps environment ───────────────────────────────────────────────
Write-Step "Container Apps environment: $AcaEnv"
$acaEnvExists = az containerapp env show --name $AcaEnv `
    --resource-group $ResourceGroup 2>$null
if (-not $acaEnvExists) {
    az containerapp env create --name $AcaEnv --resource-group $ResourceGroup `
        --location $Location | Out-Null
}

# ─── Build images ─────────────────────────────────────────────────────────────
$repoRoot = $PSScriptRoot
$gitHashRaw = git -C $repoRoot rev-parse --short HEAD 2>$null
$gitHash    = if ($gitHashRaw) { $gitHashRaw } else { "latest" }
$mcpImage = "${acrLoginServer}/${McpApp}:${gitHash}"
$botImage = "${acrLoginServer}/${BotApp}:${gitHash}"

if (-not $SkipBuild) {
    if (-not $BotOnly) {
        Write-Step "Building MCP server image (tag: $gitHash)"
        az acr build --registry $AcrName --resource-group $ResourceGroup `
            --image "${McpApp}:${gitHash}" `
            --file "$repoRoot\Dockerfile" `
            --no-logs `
            "$repoRoot"
        # Wait for image to appear in registry
        Write-Host "Waiting for image..."
        $deadline = (Get-Date).AddMinutes(15)
        do {
            Start-Sleep -Seconds 15
            $found = az acr repository show-tags --name $AcrName --repository $McpApp --query "[?@=='$gitHash']" -o tsv 2>$null
        } while (-not $found -and (Get-Date) -lt $deadline)
        if (-not $found) { throw "MCP image $mcpImage not found in ACR after 15 min" }
        Write-Host "Built: $mcpImage"
    }

    if (-not $McpOnly) {
        Write-Step "Building bot image (tag: $gitHash)"
        az acr build --registry $AcrName --resource-group $ResourceGroup `
            --image "${BotApp}:${gitHash}" `
            --file "$repoRoot\Dockerfile.bot" `
            --no-logs `
            "$repoRoot"
        Write-Host "Waiting for image..."
        $deadline = (Get-Date).AddMinutes(15)
        do {
            Start-Sleep -Seconds 15
            $found = az acr repository show-tags --name $AcrName --repository $BotApp --query "[?@=='$gitHash']" -o tsv 2>$null
        } while (-not $found -and (Get-Date) -lt $deadline)
        if (-not $found) { throw "Bot image $botImage not found in ACR after 15 min" }
        Write-Host "Built: $botImage"
    }
}

# ─── ACR credentials for Container Apps ──────────────────────────────────────
$acrUser     = az acr credential show --name $AcrName --query "username" -o tsv
$acrPassword = az acr credential show --name $AcrName --query "passwords[0].value" -o tsv

# ─── MCP server Container App ─────────────────────────────────────────────────
if (-not $BotOnly) {
    Write-Step "Deploying MCP server: $McpApp"

    $mcpExists = az containerapp show --name $McpApp `
        --resource-group $ResourceGroup 2>$null

    $mcpEnvVars = @(
        "CANON_FABRIC_TENANT_ID=$tenantId"
        "CANON_FABRIC_CLIENT_ID=$clientId"
        "CANON_FABRIC_CLIENT_SECRET=secretref:fabric-client-secret"
        "CANON_FABRIC_WORKSPACE_ID=$workspaceId"
        "CANON_FABRIC_DATASET_NAME=$datasetName"
        "CANON_SQL_SERVER=$sqlServer"
        "CANON_SQL_DATABASE=$sqlDatabase"
        "CANON_MCP_TRANSPORT=streamable-http"
        "CANON_MCP_PORT=8000"
        "CANON_REPO_ROOT=/app"
    )

    if (-not $mcpExists) {
        az containerapp create --name $McpApp `
            --resource-group $ResourceGroup `
            --environment $AcaEnv `
            --image $mcpImage `
            --registry-server $acrLoginServer `
            --registry-username $acrUser `
            --registry-password $acrPassword `
            --target-port 8000 `
            --ingress external `
            --min-replicas 1 --max-replicas 3 `
            --cpu 0.5 --memory 1.0Gi `
            --secrets "fabric-client-secret=$clientSecret" `
            --env-vars @mcpEnvVars | Out-Null
    } else {
        az containerapp update --name $McpApp `
            --resource-group $ResourceGroup `
            --image $mcpImage | Out-Null
    }

    $mcpUrl = az containerapp show --name $McpApp `
        --resource-group $ResourceGroup `
        --query "properties.configuration.ingress.fqdn" -o tsv
    Write-Host "MCP server: https://$mcpUrl"
    Write-Host "  Health:   https://$mcpUrl/healthz"
    Write-Host "  SSE:      https://$mcpUrl/sse"
}

# ─── Bot Container App ────────────────────────────────────────────────────────
if (-not $McpOnly) {
    Write-Step "Deploying Teams bot: $BotApp"

    $botExists = az containerapp show --name $BotApp `
        --resource-group $ResourceGroup 2>$null

    $botEnvVars = @(
        "MICROSOFT_APP_ID=$msAppId"
        "MICROSOFT_APP_PASSWORD=secretref:bot-app-password"
        "CANON_GITHUB_REPO=$githubRepo"
        "CANON_GITHUB_TOKEN=secretref:github-token"
        "ANTHROPIC_API_KEY=secretref:anthropic-key"
        "BOT_PORT=3978"
    )

    if (-not $botExists) {
        az containerapp create --name $BotApp `
            --resource-group $ResourceGroup `
            --environment $AcaEnv `
            --image $botImage `
            --registry-server $acrLoginServer `
            --registry-username $acrUser `
            --registry-password $acrPassword `
            --target-port 3978 `
            --ingress external `
            --min-replicas 1 --max-replicas 2 `
            --cpu 0.25 --memory 0.5Gi `
            --secrets `
                "bot-app-password=$msAppPassword" `
                "github-token=$githubToken" `
                "anthropic-key=$anthropicKey" `
            --env-vars @botEnvVars | Out-Null
    } else {
        az containerapp update --name $BotApp `
            --resource-group $ResourceGroup `
            --image $botImage | Out-Null
    }

    $botUrl = az containerapp show --name $BotApp `
        --resource-group $ResourceGroup `
        --query "properties.configuration.ingress.fqdn" -o tsv
    Write-Host "Bot endpoint: https://$botUrl/api/messages"
    Write-Host ""
    Write-Host "Configure Azure Bot Service messaging endpoint to:"
    Write-Host "  https://$botUrl/api/messages"
}

Write-Step "Done."

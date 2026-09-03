<# 
.SYNOPSIS
Setup script for Ollama.

.DESCRIPTION
Installs Ollama if not present, starts the Ollama server, and pulls the specified model.

.PARAMETER Model
The Ollama model to pull and use. Can also be set via the OLLAMA_MODEL environment variable.

.PARAMETER Host
The URL of the Ollama server to connect to. Can also be set via the OLLAMA_HOST environment variable.

.EXAMPLE
./scripts/setup_ollama.ps1 "llama3.1"

$env:OLLAMA_MODEL = "llama3.1"; ./scripts/setup_ollama.ps1

#>

param(
    [string]$Model = $(if ($env:OLLAMA_MODEL) { $env:OLLAMA_MODEL } else { "qwen2.5:7b" })
)

$ErrorActionPreference = "Stop"
$hostUrl = if ($env:OLLAMA_HOST) { $env:OLLAMA_HOST } else { "http://localhost:11434" }

function Test-OllamaReachable {
    try {
        Invoke-WebRequest -Uri "$hostUrl/api/tags" -UseBasicParsing -TimeoutSec 3 | Out-Null
        return $true
    } catch {
        return $false
    }
}

if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Host "Ollama not found. Installing via winget..."
    try {
        winget install --id Ollama.Ollama --silent --accept-package-agreements --accept-source-agreements
    } catch {
        [Console]::Error.WriteLine(
            "winget install failed. Download and install Ollama manually from " +
            "https://ollama.com/download, then re-run this script."
        )
        exit 1
    }

    # winget updates PATH for new shells only; re-resolve for this session.
    $ollamaDefaultPath = Join-Path $env:LOCALAPPDATA "Programs\Ollama"
    if (Test-Path $ollamaDefaultPath) {
        $env:PATH = "$ollamaDefaultPath;$env:PATH"
    }
}

if (-not (Test-OllamaReachable)) {
    Write-Host "Starting ollama serve..."
    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden

    Write-Host "Waiting for Ollama at $hostUrl ..."
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        if (Test-OllamaReachable) {
            $ready = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) {
        [Console]::Error.WriteLine("Ollama did not become reachable at $hostUrl.")
        exit 1
    }
}

Write-Host "Pulling model $Model ..."
ollama pull $Model

Write-Host "Ollama ready at $hostUrl with model $Model."

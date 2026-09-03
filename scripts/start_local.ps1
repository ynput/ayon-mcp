# Check if uv is installed
try {
    $null = uv --version
} catch {
    [Console]::Error.WriteLine("uv is not installed. Installing uv...")
    Invoke-WebRequest -Uri https://astral.sh/uv/install.ps1 -OutFile $env:TEMP\uv-install.ps1
    & $env:TEMP\uv-install.ps1
    Remove-Item $env:TEMP\uv-install.ps1
}

# Resolve paths relative to this script so it can be called from anywhere.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..")
$serviceRoot = Join-Path $repoRoot "services\mcp"

if (-Not (Test-Path $serviceRoot)) {
    [Console]::Error.WriteLine("Service root not found: $serviceRoot")
    exit 1
}

Push-Location $serviceRoot
try {
    # Install dependencies in the services/mcp project environment.
    [Console]::Error.WriteLine("Installing dependencies in $serviceRoot ...")
    uv sync --no-install-project | Out-Null

    # Execute the CLI from services/mcp so imports resolve consistently.
    [Console]::Error.WriteLine("Executing CLI...")
    if ($env:AYON_MCP_ENABLE_TELEMETRY -match "^(1|true|yes)$") {
        uv run opentelemetry-instrument `
            --traces_exporter otlp --metrics_exporter otlp --logs_exporter otlp `
            python -m ayon_mcp.cli @args
    } else {
        uv run python -m ayon_mcp.cli @args
    }
} finally {
    Pop-Location
}

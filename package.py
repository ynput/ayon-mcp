# Required: lower case addon name e.g. 'deadline', otherwise addon
#   will be invalid
name = "mcp"

# Optional: Addon title shown in UI, 'name' is used by default e.g. 'Deadline'
title = "AYON MCP server"

# Required: Valid semantic version (https://semver.org/)
version = "0.0.1"

# Name of client code directory imported in AYON launcher
# - do not specify if there is no client code
client_dir = None


services = {
    "mcp": {
        "image": "ynput/ayon-mcp:dev",
        "environment": {
            "OTEL_METRIC_EXPORT_INTERVAL": "5000",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://ayon-vector:4317",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
            "OTEL_EXPORTER_OTLP_INSECURE": "true",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://ayon-tempo:4317"
        }
    },
}

# Version compatibility with AYON server
# ayon_server_version = ">=1.0.7"
# Version compatibility with AYON launcher
# ayon_launcher_version = ">=1.0.2"

# Mapping of addon name to version requirements
# - addon with specified version range must exist to be able to use this addon
ayon_required_addons = {}
# Mapping of addon name to version requirements
# - if addon is used in the same bundle, the version range must be valid
ayon_compatible_addons = {}

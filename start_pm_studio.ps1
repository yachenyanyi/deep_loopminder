# Run from the dedicated directory so LangGraph dev's local data is isolated.
$studioVariables = @('PYTHONUTF8')
$previousValues = @{}
foreach ($name in $studioVariables) {
    $previousValues[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    $env:PYTHONUTF8 = '1'
    Push-Location (Join-Path $PSScriptRoot 'studio_runtime')
    langgraph dev --port 2025
} finally {
    Pop-Location
    foreach ($name in $studioVariables) {
        [Environment]::SetEnvironmentVariable($name, $previousValues[$name], 'Process')
    }
}

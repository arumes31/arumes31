# run_cleanup.ps1 - Powershell script to run clean_github_assets.py daily via Task Scheduler

$logFile = "C:\DR\cleanup.log"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# Create log entry header
"--------------------------------------------------" | Out-File -FilePath $logFile -Append -Encoding utf8
"Starting clean_github_assets execution at $timestamp" | Out-File -FilePath $logFile -Append -Encoding utf8
"--------------------------------------------------" | Out-File -FilePath $logFile -Append -Encoding utf8

try {
    # Check if gh CLI is installed
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw "GitHub CLI (gh) is not installed or not in PATH."
    }

    # Fetch GitHub token from gh CLI keyring
    $env:GITHUB_TOKEN = $null
    $token = (gh auth token).Trim()
    if (-not $token) {
        throw "Failed to retrieve GITHUB_TOKEN from gh CLI keyring. Ensure you are logged in via 'gh auth login'."
    }
    $env:GITHUB_TOKEN = $token

    # Set configuration environment variables
    $env:DRY_RUN = "false" # Set to false so the script actually performs deletions
    $env:KEEP_RUNS = "15"
    $env:KEEP_VERSIONS = "3"
    $env:KEEP_DAYS = "30"
    $env:DELAY_MS = "500"

    # Define Python script path
    $scriptPath = "C:\DR\clean_github_assets.py"
    if (-not (Test-Path $scriptPath)) {
        throw "Python script not found at $scriptPath"
    }

    # Run the python script in unbuffered mode (-u) and capture output
    & python -u $scriptPath 2>&1 | Out-File -FilePath $logFile -Append -Encoding utf8
}
catch {
    "ERROR: $_" | Out-File -FilePath $logFile -Append -Encoding utf8
}
finally {
    $endTimestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "Completed clean_github_assets execution at $endTimestamp`n" | Out-File -FilePath $logFile -Append -Encoding utf8
}

<#
.SYNOPSIS
  Turns on CloudWatch Transaction Search for this account and Region (PowerShell version of
  enable-transaction-search.sh).

.DESCRIPTION
  A one-time, ACCOUNT-WIDE setting that AgentCore / GenAI Observability needs before any spans
  appear. Safe to re-run.
    1. lets X-Ray write spans into CloudWatch Logs (log resource policy)
    2. sends X-Ray trace segments to CloudWatch Logs (Transaction Search)
    3. sets how many spans are indexed as searchable trace summaries (default 100% for a POC)
  Cost: spans are ingested as CloudWatch Logs; indexing above the free 1% is billed - lower it later.

.EXAMPLE
  .\scripts\enable-transaction-search.ps1 -Region ca-central-1
  .\scripts\enable-transaction-search.ps1 -Region ca-central-1 -IndexingPercent 10 -AwsProfile lab
#>
[CmdletBinding()]
param(
    [string] $Region = $(if ($env:AWS_REGION) { $env:AWS_REGION } else { "ca-central-1" }),
    [ValidateRange(0, 100)] [double] $IndexingPercent = 100,
    [string] $AwsProfile = ""
)
$ErrorActionPreference = "Stop"

function Invoke-Aws {
    $ErrorActionPreference = "Continue"
    $common = @("--region", $Region)
    if ($AwsProfile) { $common += @("--profile", $AwsProfile) }
    & aws @args @common
    if ($LASTEXITCODE -ne 0) { throw "aws $($args[0]) $($args[1]) failed (exit code $LASTEXITCODE)" }
}

$account   = (Invoke-Aws sts get-caller-identity --query Account --output text | Out-String).Trim()
$partition = ((Invoke-Aws sts get-caller-identity --query Arn --output text | Out-String).Trim() -split ":")[1]
Write-Host "Account $account, Region $Region"

$ErrorActionPreference = "Continue"
$current = & aws xray get-trace-segment-destination --region $Region --query "[Destination,Status]" --output text 2>$null
$ErrorActionPreference = "Stop"
$current = if ($current) { ($current | Out-String).Trim() } else { "unknown" }
Write-Host "Current trace segment destination: $current"

# JSON goes through temp files (file://) - Windows PowerShell 5.1 mangles quotes in native arguments
$tmp = New-Item -ItemType Directory -Path (Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid()))
try {
    if (-not $current.StartsWith("CloudWatchLogs")) {
        $policy = @{
            Version   = "2012-10-17"
            Statement = @(@{
                Sid       = "TransactionSearchXRayAccess"
                Effect    = "Allow"
                Principal = @{ Service = "xray.amazonaws.com" }
                Action    = "logs:PutLogEvents"
                Resource  = @("arn:${partition}:logs:${Region}:${account}:log-group:aws/spans:*",
                              "arn:${partition}:logs:${Region}:${account}:log-group:/aws/application-signals/data:*")
                Condition = @{
                    ArnLike      = @{ "aws:SourceArn" = "arn:${partition}:xray:${Region}:${account}:*" }
                    StringEquals = @{ "aws:SourceAccount" = $account }
                }
            })
        } | ConvertTo-Json -Depth 10 -Compress
        $policyFile = Join-Path $tmp "policy.json"
        [IO.File]::WriteAllText($policyFile, $policy)

        Write-Host "==> log resource policy for X-Ray" -ForegroundColor Cyan
        Invoke-Aws logs put-resource-policy --policy-name TransactionSearchXRayAccess `
            --policy-document "file://$policyFile" --query "resourcePolicy.policyName" --output text
        Write-Host "==> trace segments -> CloudWatch Logs" -ForegroundColor Cyan
        Invoke-Aws xray update-trace-segment-destination --destination CloudWatchLogs --query "[Destination,Status]" --output text
    } else {
        Write-Host "Transaction Search is already on"
    }

    Write-Host "==> index $IndexingPercent% of spans as trace summaries" -ForegroundColor Cyan
    $ruleFile = Join-Path $tmp "rule.json"
    [IO.File]::WriteAllText($ruleFile, (@{ Probabilistic = @{ DesiredSamplingPercentage = $IndexingPercent } } | ConvertTo-Json -Compress))
    Invoke-Aws xray update-indexing-rule --name Default --rule "file://$ruleFile" `
        --query "IndexingRule.Rule.Probabilistic.DesiredSamplingPercentage" --output text
} finally {
    Remove-Item -Recurse -Force $tmp
}

Write-Host "Done. It can take a few minutes before the destination shows ACTIVE:" -ForegroundColor Green
Write-Host "  aws xray get-trace-segment-destination --region $Region"

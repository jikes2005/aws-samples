<#
.SYNOPSIS
  Deploys or updates the br-ac-react-chat CloudFormation stack with the AWS CLI (PowerShell version
  of deploy.sh).

.DESCRIPTION
  Build the images first with build_images.ipynb (it also creates the ECR repositories) and pass the
  UiImageUri, ApiImageUri and CpuArchitecture it prints. On later runs the image parameters are
  optional: leaving them out keeps the stack's current images.

  The script:
    1. checks the API Gateway CloudWatch Logs role and reads the agent stack's outputs
       (AgentRuntimeArn, MemoryId, KmsKeyArn)
    2. deploys the template with "aws cloudformation deploy"
    3. redeploys the API stage, then prints the stack outputs

  Needs the AWS CLI v2 and credentials for the target account (aws configure / SSO / env vars).

.EXAMPLE
  .\scripts\deploy.ps1 -Region ca-central-1 -VpcId vpc-0abc -SubnetIds subnet-aaa,subnet-bbb `
      -ClientCidr 10.0.0.0/8 -ExecuteApiVpceId vpce-0123 `
      -UiImageUri 111122223333.dkr.ecr.ca-central-1.amazonaws.com/br-ac-react-chat-ui:v20261028-090000 `
      -ApiImageUri 111122223333.dkr.ecr.ca-central-1.amazonaws.com/br-ac-react-chat-api:v20261028-090000 `
      -CpuArchitecture X86_64

.EXAMPLE
  # Later update that keeps the current images
  .\scripts\deploy.ps1 -Region ca-central-1 -VpcId vpc-0abc -SubnetIds subnet-aaa,subnet-bbb `
      -ClientCidr 10.0.0.0/8 -ExecuteApiVpceId vpce-0123
#>
[CmdletBinding()]
param(
    # ---- Required ----
    [Parameter(Mandatory)] [string]   $VpcId,
    [Parameter(Mandatory)] [string[]] $SubnetIds,          # private subnets in 2+ AZs (NLB and tasks)
    [Parameter(Mandatory)]
    [ValidatePattern('^(\d{1,3}\.){3}\d{1,3}/\d{1,2}$')]
                           [string]   $ClientCidr,         # where users' browsers come from
    [Parameter(Mandatory)]
    [ValidatePattern('^vpce-[0-9a-f]+$')]
                           [string]   $ExecuteApiVpceId,   # existing execute-api endpoint (same as the agent stack)

    # ---- Images (from build_images.ipynb; required on the first deploy) ----
    [string] $UiImageUri = "",
    [string] $ApiImageUri = "",
    [ValidateSet("X86_64", "ARM64")] [string] $CpuArchitecture = "X86_64",

    # ---- Optional ----
    [string]   $Region = $(if ($env:AWS_REGION) { $env:AWS_REGION } else { "ca-central-1" }),
    [string]   $AwsProfile = "",                           # AWS CLI profile; blank = default credentials
    [string]   $StackName = "br-ac-react-chat",            # also the ProjectName parameter (max 23 chars)
    [string]   $AgentStackName = "agentcore-poc",
    [string]   $StageName = "dev",
    [string[]] $ExtraTlsNames = @(),                       # other names/IPs users browse the UI by
    [string]   $KmsKeyArn = "",                            # existing CMK; blank = the stack creates one
    [ValidateRange(0, 10)] [int] $UiDesiredCount = 1,
    [ValidateRange(0, 10)] [int] $ApiDesiredCount = 1,
    [string]   $TemplateFile = (Join-Path $PSScriptRoot "..\cfn\br-ac-react-chat.yaml"),

    # ---- Required tags (defaults match the template) ----
    [string] $AppCatID = "12345",
    [string] $ServiceName = "AgentCore POC",
    [string] $SupportTeam = "CLOUD",
    [string] $Environment = "dev",

    # Delete a stack left in ROLLBACK_COMPLETE (a failed first create) before deploying
    [switch] $DeleteFailedStack
)

$ErrorActionPreference = "Stop"

# Run the AWS CLI with the region/profile; stop on a non-zero exit code. Output streams through.
# (A simple function with $args, so "--option" tokens pass straight to aws. Native stderr isn't
# treated as an error here - the exit code decides - which keeps Windows PowerShell 5.1 happy.)
function Invoke-Aws {
    $ErrorActionPreference = "Continue"
    $common = @("--region", $Region)
    if ($AwsProfile) { $common += @("--profile", $AwsProfile) }
    & aws @args @common
    if ($LASTEXITCODE -ne 0) { throw "aws $($args[0]) $($args[1]) failed (exit code $LASTEXITCODE)" }
}

# Same, but returns $null instead of failing (for "does it exist?" checks).
function Test-Aws {
    $ErrorActionPreference = "Continue"
    $common = @("--region", $Region)
    if ($AwsProfile) { $common += @("--profile", $AwsProfile) }
    $out = & aws @args @common 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return $out
}

function Get-StackOutput([string] $Stack, [string] $Key) {
    $v = Invoke-Aws cloudformation describe-stacks --stack-name $Stack `
        --query "Stacks[0].Outputs[?OutputKey=='$Key'].OutputValue" --output text
    if (-not $v -or $v -eq "None") { return "" }
    return ($v | Out-String).Trim()
}

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) { throw "AWS CLI not found - install AWS CLI v2" }
if (-not (Test-Path $TemplateFile)) { throw "Template not found: $TemplateFile" }
if ($StackName.Length -gt 23) { throw "StackName (ProjectName) must be 23 characters or fewer" }
$TemplateFile = (Resolve-Path $TemplateFile).Path

# ---------------------------------------------------------------------------
Write-Host "==> 1/3 checks ($Region)" -ForegroundColor Cyan
# ---------------------------------------------------------------------------
$cwRole = Test-Aws apigateway get-account --query cloudwatchRoleArn --output text
if (-not $cwRole -or "$cwRole".Trim() -eq "None") {
    throw ("API Gateway has no CloudWatch Logs role in $Region, so the stage's logging would fail. " +
           "Set it once per account/region (the agent stack can: CREATE_APIGW_ACCOUNT_ROLE=true).")
}

$agentRuntimeArn = Get-StackOutput $AgentStackName "AgentRuntimeArn"
$memoryId        = Get-StackOutput $AgentStackName "MemoryId"
$agentKmsKeyArn  = Get-StackOutput $AgentStackName "KmsKeyArn"
if (-not $agentRuntimeArn -or -not $memoryId -or -not $agentKmsKeyArn) {
    throw "AgentRuntimeArn, MemoryId or KmsKeyArn missing from $AgentStackName outputs - deploy the agent stack (with AgentCore Memory) first"
}
Write-Host "    runtime: $agentRuntimeArn"
Write-Host "    memory : $memoryId"

$status = Test-Aws cloudformation describe-stacks --stack-name $StackName --query "Stacks[0].StackStatus" --output text
$status = if ($status) { ($status | Out-String).Trim() } else { "" }
if ($status -eq "ROLLBACK_COMPLETE") {
    if (-not $DeleteFailedStack) {
        throw "Stack $StackName is in ROLLBACK_COMPLETE (its first create failed) and can't be updated. Re-run with -DeleteFailedStack to delete it first."
    }
    Write-Host "    deleting failed stack $StackName ..."
    Invoke-Aws cloudformation delete-stack --stack-name $StackName | Out-Null
    Invoke-Aws cloudformation wait stack-delete-complete --stack-name $StackName | Out-Null
    $status = ""
}

$overrides = @(
    "ProjectName=$StackName",
    "StageName=$StageName",
    "VpcId=$VpcId",
    "PrivateSubnetIds=$($SubnetIds -join ',')",
    "ClientCidr=$ClientCidr",
    "ExecuteApiVpceId=$ExecuteApiVpceId",
    "AgentRuntimeArn=$agentRuntimeArn",
    "MemoryId=$memoryId",
    "AgentKmsKeyArn=$agentKmsKeyArn",
    "ExtraTlsNames=$($ExtraTlsNames -join ',')",
    "ExistingKmsKeyArn=$KmsKeyArn",
    "UiDesiredCount=$UiDesiredCount",
    "ApiDesiredCount=$ApiDesiredCount",
    "AppCatID=$AppCatID",
    "ServiceName=$ServiceName",
    "SupportTeam=$SupportTeam",
    "Environment=$Environment"
)

if ($UiImageUri -or $ApiImageUri) {
    if (-not ($UiImageUri -and $ApiImageUri)) { throw "Give both -UiImageUri and -ApiImageUri" }
    $overrides += @("UiImageUri=$UiImageUri", "ApiImageUri=$ApiImageUri", "CpuArchitecture=$CpuArchitecture")
} elseif (-not $status) {
    throw "First deploy: give -UiImageUri, -ApiImageUri and -CpuArchitecture (run build_images.ipynb first; it prints them)"
} else {
    Write-Host "    images : unchanged (give -UiImageUri and -ApiImageUri to change them)"
}

# ---------------------------------------------------------------------------
Write-Host "==> 2/3 deploy stack $StackName" -ForegroundColor Cyan
# ---------------------------------------------------------------------------
# Unspecified parameters (e.g. the images on a later run) keep their current values.
Invoke-Aws cloudformation deploy `
    --stack-name $StackName `
    --template-file $TemplateFile `
    --capabilities CAPABILITY_IAM `
    --no-fail-on-empty-changeset `
    --parameter-overrides @overrides | Write-Host

# ---------------------------------------------------------------------------
Write-Host "==> 3/3 redeploy the API stage" -ForegroundColor Cyan
# ---------------------------------------------------------------------------
# CloudFormation doesn't redeploy a stage when methods change; this makes sure it's current.
$apiId = Get-StackOutput $StackName "RestApiId"
Invoke-Aws apigateway create-deployment --rest-api-id $apiId --stage-name $StageName `
    --description "deploy.ps1 $((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))" | Out-Null

Invoke-Aws cloudformation describe-stacks --stack-name $StackName --query "Stacks[0].Outputs" --output table | Write-Host
Write-Host ""
Write-Host "Open: $(Get-StackOutput $StackName 'AppUrl')  (self-signed certificate - expect a browser warning)" -ForegroundColor Green

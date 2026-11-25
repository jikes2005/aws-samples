param(
  [Parameter(Mandatory)] [string] $VpcId,
  [Parameter(Mandatory)] [string] $SubnetIds,
  [Parameter(Mandatory)] [string] $ClientCidr,
  [string] $Region = "us-east-1",
  [string] $Project = "av-ecs-poc",
  [string] $Tag = "v1",
  [string] $AppCatId = "000000",
  [string] $SupportTeam = "cloud-architecture",
  [string] $Environment = "lab",
  [ValidateSet("base", "services", "all")] [string] $Stage = "all",
  [switch] $Build
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$baseStack = "$Project-base"
$svcStack = "$Project-services"
$tags = @("AppCatID=$AppCatId", "ServiceNameTag=$Project", "SupportTeam=$SupportTeam", "Environment=$Environment")

function Invoke-Aws { & aws @args; if ($LASTEXITCODE -ne 0) { throw "aws $($args[0]) $($args[1]) failed" } }

if ($Stage -in "base", "all") {
  Write-Host "== base stack $baseStack"
  Invoke-Aws cloudformation deploy --region $Region --stack-name $baseStack `
    --template-file "$root\cfn\01-base.yaml" --capabilities CAPABILITY_IAM `
    --parameter-overrides ProjectName=$Project VpcId=$VpcId SubnetIds=$SubnetIds ClientCidr=$ClientCidr @tags
}

if ($Build) {
  $repo = aws cloudformation describe-stacks --region $Region --stack-name $baseStack `
    --query "Stacks[0].Outputs[?OutputKey=='RepositoryUri'].OutputValue" --output text
  aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $repo.Split("/")[0]
  docker build --platform linux/amd64 -t "${repo}:$Tag" "$root\app"
  docker push "${repo}:$Tag"
} elseif ($Stage -eq "all") {
  Read-Host "Build and push image tag '$Tag' (notebooks\build_push_image.ipynb), then press Enter"
}

if ($Stage -in "services", "all") {
  Write-Host "== services stack $svcStack"
  Invoke-Aws cloudformation deploy --region $Region --stack-name $svcStack `
    --template-file "$root\cfn\02-services.yaml" `
    --parameter-overrides ProjectName=$Project BaseStackName=$baseStack ImageTag=$Tag @tags
  aws cloudformation describe-stacks --region $Region --stack-name $svcStack --query "Stacks[0].Outputs" --output table
}

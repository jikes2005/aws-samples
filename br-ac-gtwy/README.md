# AgentCore MCP POC

Strands agent on **Bedrock AgentCore Runtime** → **AgentCore Gateway** (MCP) → **Lambda** tool → **private API Gateway** (Cognito-protected) → Lambda → **DynamoDB**.

Everything is in **one CloudFormation template** (`cfn/agentcore-mcp-poc.yaml`) and runs in an existing VPC using existing subnets and VPC endpoints. Nothing is internet-facing.

```
 invoke_agent_runtime
        │
┌───────▼────────────── existing VPC (private subnets) ──────────────────────────────┐
│ AgentCore Runtime (VPC mode)                                                       │
│   Strands agent ──SigV4 MCP──► AgentCore Gateway (MCP, AWS_IAM inbound auth)        │
│        │                              │ GATEWAY_IAM_ROLE                            │
│        └──► Bedrock (model)           ▼                                             │
│                       Lambda: customer-tools ──GetClientToken──► Cognito user pool  │
│                              │  Bearer <access token>            (M2M, no domain)   │
│                              ▼                                                      │
│              Private REST API + Cognito authorizer (scopes)                         │
│                GET  /customers               customer-api/customers.read            │
│                GET  /customers/{customerId}  customer-api/customers.read            │
│                POST /customers               customer-api/customers.write           │
│                              │ AWS_PROXY                                            │
│                              ▼                                                      │
│              Lambda: customer-api ──► DynamoDB  <project>-customers                 │
│                                                                                     │
│  Deploy time only: Lambda: deploy-helper (custom resources)                         │
│     Custom::CognitoM2MClient → creates the M2M app client                           │
│     Custom::SeedCustomers    → loads 4 sample customers                             │
└────────────────────────────────────────────────────────────────────────────────────┘
```

## Layout

| Path | What it is |
|---|---|
| `cfn/agentcore-mcp-poc.yaml` | The whole POC: CMK, workload SG, DynamoDB (PITR + on-demand backup), Cognito (pool, resource server, custom-resource app client), seed custom resource, private REST API + Cognito authorizer, API Lambda, tool Lambda, AgentCore Gateway + target, AgentCore Runtime |
| `agent/main.py` | Strands agent; loads tools from the gateway over MCP with SigV4 auth |
| `agent/requirements.txt` | Agent dependencies, pinned to tested major versions (`mcp` stays on 1.x) |
| `agent/build_agent.ipynb` | Builds the arm64 agent zip in SageMaker AI, uploads it to S3, optionally updates the stack and tests the agent |
| `scripts/deploy.sh` | Packages the agent for linux/arm64, uploads it to S3, deploys the stack, redeploys the API stage |
| `scripts/invoke.py` | Calls the runtime with a prompt |
| `scripts/test-api.sh` | Gets a Cognito token and curls the API (run from inside the VPC) |

## Prerequisites — what must already exist

**VPC endpoints** (interface endpoints with private DNS unless noted). Their security groups must allow inbound 443 from the workload SG the stack creates, or from the VPC CIDR.

| Endpoint | Used by |
|---|---|
| `com.amazonaws.<region>.execute-api` | Tool Lambda → private API (its ID is a stack parameter) |
| `com.amazonaws.<region>.cognito-idp` | Tool Lambda (token), deploy helper (app client) |
| `com.amazonaws.<region>.dynamodb` (gateway endpoint on the subnets' route tables) | API Lambda, deploy helper (seed) |
| `com.amazonaws.<region>.s3` (gateway endpoint on the subnets' route tables) | Deploy helper → sends its result to CloudFormation's pre-signed S3 URL. **Required** (the helper always runs in the VPC) |
| `com.amazonaws.<region>.bedrock-runtime` | Agent → model |
| `com.amazonaws.<region>.bedrock-agentcore.gateway` | Agent → MCP gateway |
| `com.amazonaws.<region>.logs` | Agent runtime logging |

Also needed:
- AWS CLI v2, Python 3.12 + pip, `zip`
- A region where AgentCore Runtime, Gateway and their PrivateLink endpoints are available (us-east-1 / us-west-2 / eu-central-1 are safest)
- Bedrock model access for `MODEL_ID` (default is a Claude Sonnet inference profile; swap for one your account has enabled)
- An existing S3 bucket for the agent zip
- Private subnets in 2+ AZs that AgentCore Runtime supports (if runtime creation fails with a subnet/AZ error, pick subnets in other AZs)

## Deploy

```bash
export AWS_REGION=us-east-1
export VPC_ID=vpc-xxxxxxxx
export SUBNET_IDS=subnet-aaaa,subnet-bbbb
export EXECUTE_API_VPCE=vpce-xxxxxxxx
export ARTIFACT_BUCKET=my-artifacts-bucket
# Tag values (defaults shown):
# export APP_CAT_ID=12345
# export SERVICE_NAME="AgentCore POC"
# export SUPPORT_TEAM=CLOUD
# export ENVIRONMENT=dev
# export KMS_KEY_ARN=arn:aws:kms:...:key/...   # use an existing CMK (blank = stack creates one)
# export CREATE_APIGW_ACCOUNT_ROLE=true        # only if the account has no API Gateway CloudWatch role
./scripts/deploy.sh
```

The stack name is `agentcore-mcp-poc` (override with `PROJECT`). Re-running uploads a new agent zip under a new key, which updates the runtime, and redeploys the API stage.

You can also deploy the template from the console or a pipeline. Upload the agent zip first and pass its key as `AgentCodeKey`.

### Build the agent in SageMaker AI (alternative to `deploy.sh` packaging)

Upload the `agent/` folder to a SageMaker AI notebook instance or Studio JupyterLab space, open `build_agent.ipynb` from inside that folder, and run all cells.

1. **Configure:** set `PROJECT` and `ARTIFACT_BUCKET` (blank uses the SageMaker default bucket).
2. **Install:** installs dependencies for linux/arm64, Python 3.12, using arm64 wheels only. The SageMaker instance is usually x86_64, so nothing is compiled locally.
3. **Check:** syntax-checks `main.py`, then confirms every native extension is an aarch64 build and `mcp` is 1.x.
4. **Zip and upload:** writes `agent.zip` with `main.py` at the root and uploads it under a new versioned key. It prints the key to use as `AgentCodeKey`.
5. **Update stack** (`DEPLOY_STACK = True`): updates the existing stack, changing only `ArtifactBucket` and `AgentCodeKey`. Every other parameter keeps its previous value.
6. **Test** (`RUN_TEST = True`): invokes the agent. You can also call `ask("...")` from any cell.

For the very first deploy, either run `deploy.sh` or deploy the template from the console using the key printed in step 4.

The notebook's execution role needs `s3:PutObject` on the bucket. Steps 5–6 also need `cloudformation:DescribeStacks` and `UpdateStack`, `iam:PassRole` on the stack's roles, and `bedrock-agentcore:InvokeAgentRuntime`.

## Test

**Agent end to end** (from your laptop):

```bash
python scripts/invoke.py "List active customers in Ontario"
python scripts/invoke.py "What accounts does C-1002 have and what's the total balance?"
python scripts/invoke.py "Onboard Dana Lee, dana.lee@example.com, Ottawa ON"
python scripts/invoke.py "Onboard Dana Lee again with dana.lee@example.com"   # expect duplicate-email 409
```

New customers persist in DynamoDB, so a follow-up "show me Dana Lee's record" works.

**Tool Lambda directly** (simulates the gateway's client context):

```bash
CTX=$(printf '{"custom":{"bedrockAgentCoreToolName":"customer-tools___get_customer"}}' | base64 -w0)
aws lambda invoke --function-name agentcore-mcp-poc-customer-tools \
  --client-context "$CTX" --cli-binary-format raw-in-base64-out \
  --payload file://events/tool-get-customer.json out.json && cat out.json
```

**API directly** (from an instance in the VPC): `./scripts/test-api.sh` checks that a request without a token gets 401, then runs the GETs and a POST.

## Cognito custom resource

`AWS::Cognito::UserPoolClient` doesn't accept the `ALLOW_CLIENT_TOKEN_AUTH` flow yet, and Cognito won't add that flow to an existing client. So the app client is a `Custom::CognitoM2MClient` backed by the `deploy-helper` Lambda.

- **Create:** creates the client with a secret, `ALLOW_CLIENT_TOKEN_AUTH` as the only flow, `client_credentials`, the two scopes, and a 60-minute token. `Ref` returns the client ID.
- **Update:** changing the scopes or token validity updates the client in place. Changing `UserPoolId` or `ClientName` creates a new client, and CloudFormation then deletes the old one.
- **Delete:** deletes the client. It ignores a client that is already gone.
- **The client secret never leaves Cognito.** It isn't returned to CloudFormation, so it's not in stack events or outputs. The tool Lambda reads it with `DescribeUserPoolClient` at cold start (IAM-scoped to this pool).
- The resource `DependsOn` the resource server, so the scopes exist before the client references them.

`Custom::SeedCustomers` loads 4 sample customers on create. It uses conditional puts, so it never overwrites records. Bump `SeedVersion` to re-run it. Nothing happens on delete, because the table goes with the stack.

When CloudFormation adds the flow, replace the custom resource with a native `AWS::Cognito::UserPoolClient`.

**If a custom resource can't reach CloudFormation** (no S3 route from the subnets), the stack waits up to `ServiceTimeout` (5 minutes) and then fails. Fix the S3 gateway endpoint (route on the private subnets' route tables, and an endpoint policy that allows the CloudFormation response bucket).

## Organisation policy hook (CCF::PAC::LambdaHook)

The account runs a CloudFormation hook that scans **the template itself**, before parameters are resolved. Write any value the hook checks as a literal, never as `!Ref` or `!If`.

| Hook finding | Fix in the template |
|---|---|
| API.31 – stage tagged `ApplyCommonWaf` | Literal `"true"` tag on `ApiStage` |
| DDB.14 – deletion protection | `DeletionProtectionEnabled: true` on the table |
| Lam.23 – Lambda in a VPC | The deploy helper always runs in the VPC (the earlier opt-out was removed) |
| Required tags | `AppCatID`, `Description`, `ServiceName`, `Support-Team` and `Environment` on every taggable resource (24). `Description` is per resource. The others come from parameters |
| DDB.11 – on-demand backup | Literal tag `dynamodbbackup: "True"` on the table. The hook checks the tag, which enrols the table in the organisation's backup policy (confirm with your platform team) |

Resources that can't take tags are skipped: custom resources, API methods and resources, the authorizer, deployment, Lambda permission, KMS alias and gateway target.


| Requirement | How it's met | Scanner check (Checkov) |
|---|---|---|
| Customer managed KMS key everywhere it's supported | One CMK (rotation on), or bring your own via `ExistingKmsKeyArn`. See coverage below | CKV_AWS_119, 158, 173, 7 |
| DynamoDB encrypted with the CMK | `SSEType: KMS` + `KMSMasterKeyId` | CKV_AWS_119 |
| On-demand backup configured | `dynamodbbackup: "True"` tag (org backup policy), PITR, and a native DynamoDB on-demand backup taken at deploy (`Custom::DynamoDbOnDemandBackup`). The stack has no AWS Backup vault or plan of its own | CKV_AWS_28 |
| API stage tagged `ApplyCommonWaf` | Literal `"true"` on the stage's tags. It's not a parameter, because the org hook scans the template and can't resolve `!Ref`. Edit the value to `"false"` if needed | API.31 |
| X-Ray tracing on the API stage | `TracingEnabled: true` | CKV_AWS_73 |
| API Gateway access logging | JSON access logs to a CMK-encrypted log group, 90-day retention | CKV_AWS_76 |

Checkov 3.x result for the template: **82 passed, 7 failed**. None of the failures are in the list above. They are Lambda dead-letter queues and reserved concurrency (CKV_AWS_116/115) and API caching (CKV_AWS_120). Dead-letter queues only apply to async invocations, and these Lambdas are called synchronously.

**About "API Gateway V2" access logging:** this stack has no API Gateway V2 (HTTP/WebSocket) API. The customer API is a REST API (v1), because only REST APIs support the `PRIVATE` endpoint type. Access logging is enabled on that REST stage, which satisfies the v1 rule (CKV_AWS_76). The V2 rule (CKV_AWS_95) only applies to `AWS::ApiGatewayV2::Stage`, so it has nothing to evaluate here.

### CMK coverage

| Resource | Encryption |
|---|---|
| DynamoDB table | CMK |
| DynamoDB on-demand backups | CMK (inherits the table's key) |
| CloudWatch log groups (3 Lambdas + API access logs) | CMK |
| Lambda environment variables (3 functions) | CMK |
| AgentCore Gateway | CMK |
| AgentCore Runtime (serverless) | AWS-owned key. No CMK option for the serverless compute type |
| AgentCore Runtime log group | Created by the service at first run, so it has the default encryption. Add a CMK afterwards with `aws logs associate-kms-key` if needed |
| Cognito user pool | AWS-owned key. Cognito has no CMK option for user pool data |
| API Gateway | No data at rest (no cache configured) |
| Agent zip in S3 | Your existing bucket's encryption settings |

The key policy has two statements:

- **Account root:** enables IAM policies on the key.
- **CloudWatch Logs:** scoped to this account's log groups.

Every role gets key use through its own IAM policy on this one key:

- **Gateway service role** (`GatewayKmsPolicy`): `DescribeKey`, `Decrypt`, `GenerateDataKey`, `CreateGrant`. This is required at gateway creation. A key-policy grant with `kms:ViaService` and encryption-context conditions was rejected with "no identity-based policy allows kms:GenerateDataKey".
- **The two roles that access the table:** through `dynamodb.<region>.amazonaws.com` only.
- **The three Lambda roles:** `Decrypt`, for their environment variables.

There's no wildcard principal. None of these calls go from the VPC to KMS, so you don't need a KMS endpoint.

**Using an existing CMK (`ExistingKmsKeyArn`):** its key policy must enable IAM policies (the standard account-root statement) and allow CloudWatch Logs for this account's log groups. The stack's IAM policies cover everything else.

### Access-logging prerequisite

API Gateway access logging needs a CloudWatch Logs role set at the **account level** for each region. `deploy.sh` checks this first and stops if none is set. If your account doesn't have one, set `CREATE_APIGW_ACCOUNT_ROLE=true` and the stack creates it. That overwrites the account-wide setting, so in a shared account check with your platform team first.

### Backups

DynamoDB's own backups don't need AWS Backup or a vault.

- **Scheduled:** handled by your organisation's AWS Backup policy, via the `dynamodbbackup: "True"` tag. Those recovery points live in the org's vault, not in this stack.
- **Continuous:** PITR lets you restore to any second in the last 35 days.
- **On demand:** to take another native on-demand backup, bump `BackupVersion` on the `OnDemandBackup` resource and redeploy, or run `aws dynamodb create-backup` at any time.
- **Retention:** native on-demand backups don't expire; delete them when they're no longer needed.

## Design notes

- **Cognito without a domain.** A user pool with a domain can't be reached through the cognito-idp VPC endpoint, and `/oauth2/token` isn't available over PrivateLink. So the pool has no domain, and the tool Lambda uses Cognito's `GetClientToken` API (client-credentials M2M), which goes through the cognito-idp endpoint.
  - It's called as a raw JSON request, so it doesn't depend on the boto3 version bundled in the Lambda runtime.
  - Tokens are cached until a minute before expiry; a 401 forces a refresh.
- **Scopes per method.** GET requires `customer-api/customers.read`, POST requires `customer-api/customers.write`. The API Lambda logs the caller's `client_id`.
- **Private API** keeps the resource policy that denies anything not arriving through your execute-api endpoint, so a request needs both the right network path and a valid token.
- **Gateway auth:** inbound `AWS_IAM` (only the runtime role can invoke it; the agent SigV4-signs MCP calls), outbound `GATEWAY_IAM_ROLE` (the gateway role can invoke only the tool Lambda).
- **DynamoDB:** on-demand, PITR and encryption on.
  - `list_customers` uses a Scan with filters, which is fine at POC size. Add GSIs for real access patterns.
  - POST rejects duplicate emails via the `email-index` GSI and uses a conditional put on `customerId`.
  - The table has `DeletionPolicy: Delete`; switch to `Retain` before putting real data in it.
- **Tool input is whitelisted.** The tool Lambda forwards only known fields to POST.
- `ExceptionLevel: DEBUG` on the gateway returns detailed tool errors to the agent. Useful for a POC; remove it later.

## Cleanup

```bash
# Deletion protection is on - turn it off first or the delete fails
aws dynamodb update-table --table-name agentcore-mcp-poc-customers --no-deletion-protection-enabled
aws cloudformation delete-stack --stack-name agentcore-mcp-poc
aws cloudformation wait stack-delete-complete --stack-name agentcore-mcp-poc
```

The custom resource deletes the app client before the user pool goes. Lambda VPC ENIs can take several minutes to release. Your VPC, subnets, endpoints and the agent zip in S3 are untouched.

**Retained on purpose**, so backups stay usable after the stack is gone:

- **The KMS key and its alias:** schedule deletion with `aws kms schedule-key-deletion` when you're done.
- **Native DynamoDB on-demand backups:** `aws dynamodb list-backups`, then `delete-backup`.
- **The account-level API Gateway CloudWatch role setting,** if the stack created it.

Delete the backups before the key, or they become unreadable.

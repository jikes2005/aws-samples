# br-ac-react-chat

A React chat UI for the customer agent in **agentcore-poc**, with a REST backend:

- the **React UI** is served by its own ECS container behind an internal Network Load Balancer;
- the browser calls a **private API Gateway REST API**;
- API Gateway forwards to a **FastAPI service on ECS** through a VPC link;
- the API calls the **AgentCore Runtime** agent and reads chat history from its **AgentCore Memory**.

Answers stream into the page as they're written.

It's a separate application with its own CloudFormation stack (`cfn/br-ac-react-chat.yaml`), in the same existing VPC. It replaces nothing: `br-ac-chat` (Streamlit) can keep running alongside.

> **Lab version: no sign-in.** Users type a name, and the API uses it to keep each person's chats and the agent's memory of them separate. Anyone who can reach the app can type any name and see that name's chats, so use it with demo data only.

```
 Browser (corporate network / VPN)
   │ 1. https://<NLB DNS>/            the React app (self-signed certificate - browser warning)
   │ 2. https://<api-id>-<vpce-id>.execute-api.<region>.amazonaws.com/dev/api/...
   │                                  REST calls from the page (AWS certificate, CORS)
┌──┼─────────────────── existing VPC (private subnets) ──────────────────────────┐
│  │                                                                             │
│  ├─1─► Internal NLB :443 ──TLS passthrough──► ECS: UI (static React + /config) │
│  │                                                                             │
│  └─2─► execute-api VPC endpoint ──► Private REST API (ANY /api/{proxy+},       │
│                                      response streaming, org WAF)              │
│                                          │ VPC link                            │
│                                          ▼                                     │
│                                     NLB :8080 ──HTTP──► ECS: API (FastAPI)     │
│                                                            │ stream            │
│                                    bedrock-agentcore endpoint│                 │
│                                                            ▼                   │
│              AgentCore Runtime (agent stack)  +  AgentCore Memory (history)    │
│                                                                                │
│ SageMaker notebook: build_images.ipynb - ECR repos (AWS CLI), npm build,       │
│   both images (Python base image via ECR pull-through cache - no internet)     │
└────────────────────────────────────────────────────────────────────────────────┘
```

## Layout

| Path | What it is |
|---|---|
| `cfn/br-ac-react-chat.yaml` | The infrastructure: CMK, security groups, internal NLB (2 listeners), VPC link, private REST API + stage, ECS cluster, UI and API services |
| `build_images.ipynb` | In a SageMaker notebook: creates the ECR repositories and pull-through cache (AWS CLI), builds the React app with npm, builds and pushes both images, optionally updates the stack |
| `scripts/deploy.sh`, `scripts/deploy.ps1` | Read the agent stack's outputs and deploy or update the stack with the image URIs (bash / PowerShell) |
| `ui/web/` | The React app (Vite + TypeScript). `dist/` is a prebuilt copy, used when `BUILD_UI = False` |
| `ui/server.py` | UI server: serves `dist/` over HTTPS with a self-signed certificate made at start-up, plus `/config.json` (the API address) and `/healthz` |
| `api/main.py` | The REST API (FastAPI) |
| `ui/Dockerfile`, `api/Dockerfile`, `*/requirements.txt`, `*/run/` | Images: Python 3.12 slim, non-root, no `RUN` steps; `run/` becomes each app's writable volume |

## The API

All routes are under `/api`. The user's name goes in the `X-Chat-User` header.

| Method and path | Does |
|---|---|
| `GET /api/health` | Health check (NLB) |
| `GET /api/chats` | The user's 20 most recent chats, titled with their first question |
| `GET /api/chats/{chatId}` | One chat's messages |
| `POST /api/chats/{chatId}/messages` | Body `{"prompt": "..."}`. Replies with server-sent events: `{"type":"text","text":...}` as the answer is written, `{"type":"tool","name":...}` when the agent calls a tool, then `{"type":"done","toolsUsed":[...]}` or `{"type":"error","error":...}` |

- **Chat IDs** look like `chat-<uuid>`. The browser makes a new one for each new chat, and it's also the runtime session ID, so later messages continue that conversation (short-term memory).
- **Interactive docs:** `/api/docs`.

## Why these choices

- **API Gateway response streaming** (`ResponseTransferMode: STREAM`):
  - text reaches the browser as the agent writes it;
  - an answer can take up to 15 minutes, instead of being cut off by API Gateway's usual 29-second limit (5-minute idle limit on private APIs).
- **One NLB, two listeners.** Port 443 serves browsers the UI. Port 8080 is the API, reachable only through API Gateway's VPC link: the NLB's security group has no rule for 8080, and it doesn't filter the VPC link's PrivateLink traffic.
- **The browser calls API Gateway directly**, not via the UI server, so the API is used the way any client on your network would use it. The cost is CORS: the API only accepts calls from the UI's address (the NLB name plus `ExtraTlsNames`).
- **The API stage is tagged `ApplyCommonWaf: "false"`**, so the org's common WAF isn't applied. If the access log still shows `WAF_BLOCK`, Firewall Manager has a web ACL attached anyway - check with `aws wafv2 get-web-acl-for-resource --resource-arn arn:aws:apigateway:<region>::/restapis/<api-id>/stages/<stage>`.

## Prerequisites

- **The agent stack** (`agentcore-poc`) deployed with AgentCore Memory and the agent build that streams. `deploy.sh` reads its `AgentRuntimeArn`, `MemoryId` and `KmsKeyArn` outputs.
- **VPC endpoints**, with security groups allowing 443 from the VPC range (`172.16.0.0/20`): `ecr.api`, `ecr.dkr`, `s3` (gateway), `logs`, `bedrock-agentcore`, and `execute-api` (pass its ID as `EXECUTE_API_VPCE`, as for the agent stack).
- **Users' browsers must reach:**
  - the NLB on 443 (`CLIENT_CIDR`);
  - the execute-api endpoint through its `<api-id>-<vpce-id>.execute-api...` name.

  That name is in public AWS DNS and resolves to the endpoint's private IPs. If browsers can't resolve or reach it, the page loads but every call fails with "Can't reach the API".
- **The API Gateway CloudWatch Logs role** set for the account and region. The agent stack already needed it, and `deploy.sh` checks for it.
- **A SageMaker AI notebook instance** with Docker, the AWS CLI, `pip` and `npm` access (Node.js 20+; `conda install -y -c conda-forge nodejs` if missing).

## Deploy

**1. Build the images** - upload the project folder to the notebook instance, open `build_images.ipynb` from inside it and run all cells:

1. **Configure:** `TARGET_ARCH` (`auto` = the notebook's CPU, usually x86_64), tag values, optional `ECR_KMS_KEY_ARN` (a CMK for the repositories; blank uses ECR's AWS managed key, and it can't be changed later), `BUILD_UI`.
2. **ECR (AWS CLI, only if missing):** repositories `br-ac-react-chat-ui` and `br-ac-react-chat-api` (KMS, immutable tags, scan on push, last 10 images, tags) and the pull-through cache rule `br-ac-react-chat-public` → `public.ecr.aws`.
3. **React build:** `npm ci` + `npm run build` in `ui/web`.
4. **Python packages:** binary wheels for Python 3.12 on the target CPU, for both apps. pip may print "dependency resolver" warnings about the notebook's own environment - they don't affect the images.
5. **Build and push** both images (new timestamp tag). Prints `UI_IMAGE_URI`, `API_IMAGE_URI` and `CPU_ARCHITECTURE`.
6. **Update the stack** (`DEPLOY_STACK = True`, once it exists) with the new images.

**2. Deploy the stack** with the printed values:

```bash
export AWS_REGION=ca-central-1
export VPC_ID=vpc-xxxxxxxx
export SUBNET_IDS=subnet-aaaa,subnet-bbbb   # private, 2+ AZs (NLB and tasks)
export CLIENT_CIDR=10.0.0.0/8                # where users' browsers come from
export EXECUTE_API_VPCE=vpce-xxxxxxxx        # existing execute-api endpoint (same as the agent stack)
export UI_IMAGE_URI=111122223333.dkr.ecr.ca-central-1.amazonaws.com/br-ac-react-chat-ui:v20261028-090000
export API_IMAGE_URI=111122223333.dkr.ecr.ca-central-1.amazonaws.com/br-ac-react-chat-api:v20261028-090000
export CPU_ARCHITECTURE=X86_64
# Optional:
# export AGENT_STACK=agentcore-poc
# export EXTRA_TLS_NAMES=10.1.2.3,10.1.4.5    # other names/IPs users browse the UI by (certificate + CORS)
# export STAGE=dev
# export KMS_KEY_ARN=arn:aws:kms:...
# Tag values: APP_CAT_ID, SERVICE_NAME, SUPPORT_TEAM, ENVIRONMENT
./scripts/deploy.sh
```

Re-running `deploy.sh` without the image variables keeps the current images.

**From Windows (PowerShell):** `scripts/deploy.ps1` does the same with named parameters (AWS CLI v2; Windows PowerShell 5.1 or PowerShell 7). `Get-Help .\scripts\deploy.ps1 -Full` lists them all.

```powershell
.\scripts\deploy.ps1 -Region ca-central-1 -VpcId vpc-xxxxxxxx -SubnetIds subnet-aaaa,subnet-bbbb `
    -ClientCidr 10.0.0.0/8 -ExecuteApiVpceId vpce-xxxxxxxx `
    -UiImageUri 111122223333.dkr.ecr.ca-central-1.amazonaws.com/br-ac-react-chat-ui:v20261028-090000 `
    -ApiImageUri 111122223333.dkr.ecr.ca-central-1.amazonaws.com/br-ac-react-chat-api:v20261028-090000 `
    -CpuArchitecture X86_64
# Optional: -AwsProfile lab -ExtraTlsNames 10.1.2.3,10.1.4.5 -KmsKeyArn arn:aws:kms:... -StageName dev
#           -AppCatID / -ServiceName / -SupportTeam / -Environment (tags)
#           -DeleteFailedStack  (deletes a stack left in ROLLBACK_COMPLETE by a failed first create)
```

If scripts are blocked, run it once with `powershell -ExecutionPolicy Bypass -File .\scripts\deploy.ps1 ...`. **To change the app**, edit `ui/` or `api/` and run the notebook again; step 6 rolls both services onto the new images. After changing the template, use `deploy.sh`.

## Opening the app

Open the `AppUrl` output, `https://<project>-<id>.elb.<region>.amazonaws.com/`, from the corporate network or VPN.

- **Certificate warning:** expected, the UI's certificate is self-signed. Choose *Advanced → Continue*. To confirm you're on your app, compare the certificate's SHA-256 fingerprint with the one in `/ecs/br-ac-react-chat/ui`, which is logged when each task starts. A new task makes a new certificate.
- **The NLB name doesn't resolve:** browse by the NLB's private IP instead, and add those IPs to `EXTRA_TLS_NAMES` (they go in the certificate and the API's allowed origins), then run `deploy.sh`. To find the IPs:
  ```bash
  aws ec2 describe-network-interfaces --filters "Name=description,Values=ELB net/br-ac-react-chat/*" \
    --query "NetworkInterfaces[].PrivateIpAddress" --output text
  ```

## Using it

- **Your name:** type it on the first screen. It's remembered in the browser and the URL (`?user=jane.doe`). **Change** in the sidebar switches user.
- **New chat** starts a conversation. **Recent chats** lists your last 20; click one to see it and carry on.
- **Suggestions** on an empty chat send a sample question.
- While the agent works you'll see "Thinking..." then "Using list customers...". The answer streams in with markdown tables, and the tools it used appear underneath.
- **Stop** cancels the wait in the browser. The agent may still finish, and that answer appears in the chat's history.
- Preferences and facts you mention are remembered across chats (long-term memory), about a minute after you say them.

## Security controls

| Area | Control |
|---|---|
| Access | Internal NLB only; UI on 443 from `ClientCidr`; API port only through API Gateway's VPC link; no public IPs anywhere |
| API | Private REST API: only through your execute-api endpoint (resource policy); org common WAF off (`ApplyCommonWaf: "false"`); throttling 20 req/s, burst 50; access + execution logs; X-Ray |
| Browser | CORS limited to the UI's addresses; the UI sends a Content-Security-Policy that only allows its own files and the API |
| Transport | HTTPS browser → UI task (self-signed) and browser → API Gateway (AWS certificate). VPC link → NLB → API task is HTTP inside the VPC |
| Identity | **None** - a typed name (lab only) |
| Tasks | Private subnets, ingress from the NLB only, non-root user, read-only root filesystem (`/app/run` and `/tmp` volumes only) |
| Permissions | API: invoke this one agent runtime, list sessions/events in this one memory, decrypt with the agent's key. UI: no task role |
| Encryption at rest | This stack's CMK: log groups and Fargate ephemeral storage. ECR: `ECR_KMS_KEY_ARN` or the AWS managed `aws/ecr` key |

**Using an existing CMK (`KMS_KEY_ARN`):** its key policy needs the account-root statement, the CloudWatch Logs statement and the two `fargate.amazonaws.com` statements from `KmsKey` in the template (cluster name = `ProjectName`).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Banner "Can't reach the API" | The browser can't resolve or reach `<api-id>-<vpce-id>.execute-api...` (DNS or route to the endpoint), or the browser blocked it for CORS (the page's address isn't in the API's allowed origins - add it to `EXTRA_TLS_NAMES`) |
| Banner "The API refused the request (403)" | Check `/aws/apigateway/br-ac-react-chat-api/access`: `wafResponseCode: WAF_BLOCK` = the org WAF's IP set doesn't include the browser's network; otherwise the request didn't come through `EXECUTE_API_VPCE` |
| API calls return 500/504 | VPC link or API tasks. Check the access log's `integrationStatus` and `errorMessage`, the target group health, and `/ecs/br-ac-react-chat/api` |
| `VpcLink` stuck or failed | The NLB must exist in this account and region; VPC links take a few minutes |
| Answers arrive all at once | Something between the browser and API Gateway is buffering (a proxy). The API sends events as they come |
| "Sorry, something went wrong calling the agent" | `/ecs/br-ac-react-chat/api` has the error; the agent's runtime log has the details |
| Notebook: `npm ci` fails | No npm registry access, or Node.js older than 20. Set `BUILD_UI = False` to use the prebuilt `dist/` |
| Notebook: `docker build` can't pull the base image | `ecr.api`/`ecr.dkr`/`s3` endpoints, or the role lacks `CreateRepository`/`BatchImportUpstreamImage` on `repository/br-ac-react-chat-public/*` |
| Tasks stop with `exec format error` | `CPU_ARCHITECTURE` doesn't match the images - use the value the notebook printed |

## Developing locally

```bash
# API (needs AWS credentials that can invoke the runtime and read the memory)
cd api && pip install -r requirements.txt
AWS_REGION_NAME=ca-central-1 AGENT_RUNTIME_ARN=... MEMORY_ID=... ALLOWED_ORIGINS=http://localhost:5173 \
  uvicorn main:app --port 8000

# UI with hot reload, calling the local API
cd ui/web && npm install
VITE_API_BASE_URL=http://localhost:8000 npm run dev      # http://localhost:5173
```

## Cleanup

```bash
aws cloudformation delete-stack --stack-name br-ac-react-chat
aws cloudformation wait stack-delete-complete --stack-name br-ac-react-chat
```

That removes the NLB, VPC link, API, services, log groups and the stack's KMS key (after the 7-day KMS waiting period). The ECR resources were created by the notebook; remove them when you no longer need the images:

```bash
aws ecr delete-repository --repository-name br-ac-react-chat-ui --force
aws ecr delete-repository --repository-name br-ac-react-chat-api --force
aws ecr delete-pull-through-cache-rule --ecr-repository-prefix br-ac-react-chat-public
aws ecr delete-repository --repository-name br-ac-react-chat-public/docker/library/python --force
```

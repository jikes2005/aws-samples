# br-ac-chat

A Streamlit chat UI for the customer agent in **agentcore-poc**. Answers stream in as they're written, and past chats are listed in the sidebar.

It's a separate application with its own CloudFormation stack (`cfn/br-ac-chat.yaml`), in the same existing VPC, calling the agent stack's AgentCore Runtime and Memory.

> **Lab version: no sign-in.** Users type a name, and that name keeps their chats and the agent's memory of them separate. Anyone who can reach the app can type any name and see that name's chats, so use it with demo data only.

```
 Browser (corporate network / VPN, from ClientCidr)
    │ https://<NLB DNS name>/   (self-signed certificate - browser warning)
┌───▼───────────────── existing VPC (private subnets) ─────────────────────────┐
│ Internal Network Load Balancer   TCP 443 → 8501, TLS passed straight through │
│    │                                                                         │
│    ▼                                                                         │
│ ECS Fargate: Streamlit app (non-root, read-only root filesystem)             │
│    - serves HTTPS itself with a self-signed cert made at task start          │
│    - actor_id = the name the user typed (e.g. "Jane Doe" → jane-doe)         │
│    │ InvokeAgentRuntime (stream) ─┐                                          │
│    │ ListSessions / ListEvents ───┤  bedrock-agentcore VPC endpoint          │
│    ▼                              ▼                                          │
│ AgentCore Runtime (agent stack)  AgentCore Memory (agent stack)              │
│                                                                              │
│ SageMaker notebook: build_image.ipynb creates ECR (AWS CLI), builds, pushes  │
│   (Python base image via ECR pull-through cache - no internet needed)        │
└──────────────────────────────────────────────────────────────────────────────┘
```

Nothing needs the internet, including the image build.

## Why a Network Load Balancer

An Application Load Balancer is only needed for things this lab version doesn't use: Cognito/OIDC sign-in at the load balancer, path-based routing, an ACM certificate on the listener, or WAF. With none of those, an NLB is simpler:

- **TLS passthrough:** the NLB forwards TCP 443 to the task, and Streamlit serves HTTPS with its own self-signed certificate. No ACM certificate, domain or hosted zone is needed.
- **Websockets just work** (Streamlit uses one per browser tab). The NLB's TCP idle timeout (350 s) is longer than Streamlit's keep-alive pings.
- **Stickiness by client IP** keeps a browser on the same task when you run more than one.
- **Security group on the NLB:** only `ClientCidr` can reach it, and the tasks accept traffic from the NLB only.
- **No WAF:** Firewall Manager web ACLs can't attach to an NLB, so the org's IP-set rule that blocked the API doesn't apply. Check that's acceptable for your lab account.

To add Cognito sign-in later, swap the NLB for an ALB with `authenticate-cognito`. That needs a DNS name, an ACM certificate and an internet route from the ALB to Cognito (as in the earlier `agentcore-chat` version).

## Layout

| Path | What it is |
|---|---|
| `cfn/br-ac-chat.yaml` | The app infrastructure: CMK, security groups, internal NLB, ECS Fargate cluster/service. Runs the image given in `ImageUri` |
| `app/app.py` | The Streamlit app |
| `app/entrypoint.py` | Makes the self-signed certificate, then starts Streamlit on HTTPS |
| `app/build_image.ipynb` | In a SageMaker notebook: creates the ECR repository and pull-through cache (AWS CLI), builds and pushes the image, optionally updates the stack |
| `app/Dockerfile`, `app/requirements.txt` | Container image (Python 3.12 slim, non-root, no `RUN` steps) |
| `app/streamlit_config/config.toml` | Streamlit settings. The Dockerfile copies it to `/app/.streamlit/config.toml` (kept under a plain folder name because Jupyter won't create dot-folders) |
| `app/run/` | Becomes the app's writable volume at `/app/run` (see the Dockerfile) |
| `scripts/deploy.sh` | Reads the agent stack's outputs and deploys or updates the stack with `IMAGE_URI` |

## Prerequisites

- **The agent stack** (`agentcore-poc`) deployed with AgentCore Memory and the agent build that streams. `deploy.sh` reads its `AgentRuntimeArn`, `MemoryId` and `KmsKeyArn` outputs. An older agent build still works; replies arrive in one piece.
- **VPC endpoints** for the tasks, with security groups allowing 443 from the VPC range (`172.16.0.0/20`): `ecr.api`, `ecr.dkr`, `s3` (gateway, on the subnets' route tables), `logs`, `bedrock-agentcore`.
- **A SageMaker AI notebook instance with Docker** (notebook instances have it; in Studio, Docker must be enabled for the domain) that can `pip install` from your package source, as for the agent build. Its permissions are listed at the top of `build_image.ipynb`.
- A route from where users browse (`ClientCidr`) to the private subnets.

## Deploy

Two steps: the image (and its ECR repository) from a notebook, then the stack.

**1. Build the image in SageMaker AI.** Upload the `app/` folder to a notebook instance, open `build_image.ipynb` from inside it and run all cells:

1. **Configure:** `PROJECT`, `TARGET_ARCH` (`auto` builds for the notebook's own CPU, usually x86_64), the required tag values, and optionally `ECR_KMS_KEY_ARN`.
2. **Create ECR (AWS CLI, only if missing):**
   - repository `br-ac-chat`: KMS-encrypted, immutable tags, scan on push, keeps the last 10 images, required tags.
   - pull-through cache rule `br-ac-chat-public` → `public.ecr.aws`.
3. **Install packages:** binary wheels for Python 3.12 on the target CPU, into `packages/`, like the agent build.
4. **Build and push:** the Python base image comes from `<account>.dkr.ecr.<region>.amazonaws.com/br-ac-chat-public/docker/library/python:3.12-slim`. ECR fetches it from ECR Public on AWS's side, so it arrives through your `ecr.dkr` endpoint. The Dockerfile only copies files in, so the build downloads nothing, and an arm64 image can be built on an x86_64 notebook. The image gets a new timestamp tag, and the cell prints `IMAGE_URI` and `CPU_ARCHITECTURE`.
5. **Update the stack** (`DEPLOY_STACK = True`, once the stack exists): changes only `ImageUri` and `CpuArchitecture`. The service rolls onto the new image; a failed rollout rolls back automatically.

**Repository encryption:** set `ECR_KMS_KEY_ARN` to use a customer managed key. Blank uses ECR's AWS managed KMS key (`aws/ecr`). It can't be changed after the repository exists, and the key must outlive the repository. Don't use the app stack's key, which is deleted with that stack.

**2. Deploy the stack** with the values the notebook printed:

```bash
export AWS_REGION=ca-central-1
export VPC_ID=vpc-xxxxxxxx
export SUBNET_IDS=subnet-aaaa,subnet-bbbb   # private, 2+ AZs (load balancer and tasks)
export CLIENT_CIDR=10.0.0.0/8                # where users' browsers come from
export IMAGE_URI=111122223333.dkr.ecr.ca-central-1.amazonaws.com/br-ac-chat:v20261027-213000
export CPU_ARCHITECTURE=X86_64               # or ARM64, as printed by the notebook
# Optional:
# export AGENT_STACK=agentcore-poc            # agent stack name
# export EXTRA_TLS_NAMES=10.1.2.3,10.1.4.5    # extra certificate names/IPs (e.g. NLB IPs you browse by)
# export KMS_KEY_ARN=arn:aws:kms:...          # existing CMK instead of a new one
# Tag values: APP_CAT_ID, SERVICE_NAME, SUPPORT_TEAM, ENVIRONMENT (defaults match the template)
./scripts/deploy.sh
```

Re-running `deploy.sh` without `IMAGE_URI` keeps the current image.

**To change the app later:** edit `app/` and run the notebook again. Step 5 updates the stack, or pass the new `IMAGE_URI` to `deploy.sh`.

**Redeploy the agent first** so it supports streaming (`"stream": true` in the payload). Callers that don't ask for streaming get the same JSON as before.

## Opening the app

Open the `AppUrl` output, `https://internal-br-ac-chat-<id>.elb.<region>.amazonaws.com/`, from the corporate network or VPN.

- **Certificate warning:** expected, the certificate is self-signed. Choose *Advanced → Continue* (Chrome/Edge) or *Accept the risk* (Firefox). To check you're talking to your app, compare the certificate's SHA-256 fingerprint with the one in the app log (`/ecs/br-ac-chat`, printed when each task starts). A new task makes a new certificate, so you'll see the warning again after a deploy.
- **The name doesn't resolve:** the NLB's name is in public AWS DNS (it resolves to private IPs). If your DNS can't resolve `*.elb.amazonaws.com`, browse by the NLB's private IP instead:
  ```bash
  aws ec2 describe-network-interfaces --filters "Name=description,Values=ELB net/br-ac-chat/*" \
    --query "NetworkInterfaces[].PrivateIpAddress" --output text
  ```
  NLB IPs don't change while it exists. Add them to `EXTRA_TLS_NAMES` and redeploy to have them in the certificate (you'll still get the self-signed warning, but not a name mismatch).

## Using it

- **Your name:** type it on the first screen. It's kept in the URL (`?user=jane.doe`), so a refresh or bookmark keeps it. **Change name** in the sidebar switches user.
- **New chat** starts a conversation (a new runtime session). The agent keeps that conversation's context (short-term memory).
- **Recent chats** lists your last 15 conversations, read from AgentCore Memory, titled with their first question. Click one to carry on.
- Preferences and facts you mention are remembered across chats (long-term memory), about a minute after you say them.
- While the agent works you'll see "Thinking..." and "Using list customers...", then the answer streams in, with the tools it used underneath.

## Security controls

| Area | Control |
|---|---|
| Access | Internal NLB only; 443 from `ClientCidr`; no public IPs anywhere |
| Transport | HTTPS end to end to the task (self-signed, ECDSA P-256, TLS by Streamlit/uvicorn) |
| Identity | **None** - a typed name (lab only) |
| App tasks | Private subnets, ingress from the NLB only, non-root user, read-only root filesystem (only the `/app/run` and `/tmp` volumes are writable) |
| App permissions | Invoke this one agent runtime; list sessions/events in this one memory; decrypt with the agent's key |
| Images | ECR (created by the notebook): KMS-encrypted, immutable tags, scan on push, last 10 kept |
| Encryption at rest | This stack's CMK: CloudWatch log groups, Fargate task ephemeral storage. ECR: `ECR_KMS_KEY_ARN` or the AWS managed `aws/ecr` key |

**Using an existing CMK (`KMS_KEY_ARN`):** its key policy needs the account-root statement, the CloudWatch Logs statement and the two `fargate.amazonaws.com` statements from `KmsKey` in the template (cluster name = `ProjectName`).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Notebook: `docker build` can't pull the base image | `ecr.api`/`ecr.dkr`/`s3` endpoints from the notebook's subnet, or the notebook role lacks `ecr:CreateRepository` / `ecr:BatchImportUpstreamImage` on `repository/br-ac-chat-public/*` (the first pull through the cache creates that repository) |
| Notebook: pip can't find a wheel | The package source has no binary wheel for that CPU/Python 3.12. Try the other `TARGET_ARCH` |
| Notebook: `docker build --platform` is rejected | Old Docker without BuildKit. Set `TARGET_ARCH = "auto"` to build for the notebook's own CPU |
| Notebook: `AccessDenied` on `create-repository` / `create-pull-through-cache-rule` | The notebook role's ECR permissions (listed at the top of the notebook) |
| `deploy.sh`: "set IMAGE_URI" | First deploy - run the notebook first and pass the `IMAGE_URI` it printed |
| Task log: `PermissionError: [Errno 13] Permission denied: '/tmp/tls'` (or "Can't create /app/run/tls") | An image built before the `/app/run` volume fix, or a task definition without the `run` volume. Rebuild with the notebook and deploy the current template with the new `IMAGE_URI` |
| Tasks stop with `exec format error` | `CpuArchitecture` doesn't match the image. Use the value the notebook printed |
| `Service` stuck creating; tasks stop with `CannotPullContainerError` | `ecr.api`/`ecr.dkr`/`s3` endpoints or their security groups |
| Browser times out | `ClientCidr` doesn't cover your address, or no route from your network to the subnets |
| Connection reset / no healthy targets | Task not running or failing health checks. Check the service's events and `/ecs/br-ac-chat` |
| "Couldn't load history" in the sidebar | `bedrock-agentcore` endpoint, or the task role can't use the agent's KMS key |
| "Sorry, something went wrong calling the agent" | The app log has the error; the agent's runtime log has the details |

## Running locally

```bash
cd app
pip install -r requirements.txt
export AWS_REGION_NAME=ca-central-1 AGENT_RUNTIME_ARN=... MEMORY_ID=...
mkdir -p .streamlit && cp streamlit_config/config.toml .streamlit/   # where Streamlit looks for it
streamlit run app.py              # plain HTTP on :8501
# or, as in the container (HTTPS, self-signed):  python entrypoint.py
```

Your AWS credentials need `bedrock-agentcore:InvokeAgentRuntime` on the runtime and `ListSessions`/`ListEvents` on the memory.

## Cleanup

```bash
aws cloudformation delete-stack --stack-name br-ac-chat
aws cloudformation wait stack-delete-complete --stack-name br-ac-chat
```

That removes the load balancer, the service, log groups and the stack's KMS key (after the 7-day KMS waiting period).

The ECR resources were created by the notebook, so remove them with the CLI when you no longer need the images:

```bash
aws ecr delete-repository --repository-name br-ac-chat --force
aws ecr delete-pull-through-cache-rule --ecr-repository-prefix br-ac-chat-public
aws ecr delete-repository --repository-name br-ac-chat-public/docker/library/python --force   # the cached base image
```

# AI Expo Booth Scout — API316

> Coding an AI agent from scratch with AWS Lambda durable functions
> re:Invent Code Talk, Session **API316**, Level 300

There are 400+ expo booths at re:Invent and only a few hours to walk the floor.
The **Expo Booth Scout** is an AI agent that reads what you're building, matches
you to the most relevant booths, plans a walking route, and writes a hit list
for you to approve. Once you approve, it reserves the swag each booth offers and
hands you the confirmed guide.

The whole agent is **one AWS Lambda durable function**. Durable functions give
you checkpoint and replay: you write ordinary sequential code, completed steps
are not run again on replay, and waiting costs no compute.

## What it demonstrates

| Capability | What happens | Durable primitive |
|---|---|---|
| Tool-use loop | The agent reasons through its tools and builds a hit list | `context.step` per model turn and per tool call |
| Retries | A tool step that raises is retried with exponential backoff | `StepConfig(retry_strategy=...)` |
| Human approval | The agent suspends until you approve or reject, with no Lambda running | `context.wait_for_callback` |
| External polling | The agent reserves swag, then polls until every reservation is confirmed | `context.wait_for_condition` |

## One agent, one durable function

The Scout is a single tool-using agent. Each round, it calls Amazon Bedrock, the
model picks one or more tools, the function runs them and feeds the results
back. The loop ends when the model calls `finalize_guide`. There is no separate
orchestrator.

```
Expo Booth Scout agent (handler.py): one durable tool-use loop
  step                      → model turn (Bedrock Converse + tools)
  step                      → tool: profile_interests / search_catalog / get_booth_details / plan_route
  wait_for_callback         → tool: request_approval  (suspend until the attendee decides)
  step + wait_for_condition → tool: reserve_swags     (reserve swag, then poll until confirmed)
  … repeat until the model calls finalize_guide → confirmed guide
```

- **`context.step`**: every model turn and every data tool call is checkpointed.
  If the function is interrupted, it replays and picks up at the first
  unfinished step, without calling the model or the tools again for finished
  ones. Tool steps retry with backoff (4 attempts by default).
- **`context.wait_for_callback`**: the `request_approval` tool suspends the agent
  until the attendee approves or rejects. The attendee has 2 hours by default.
- **`context.wait_for_condition`**: the `reserve_swags` tool reserves swag with a
  simulated asynchronous service (`swag_service.py`), then polls it until every
  reservation is `CONFIRMED`. The function suspends between polls. Each
  reservation is `PENDING` on the first check and `CONFIRMED` on the second.

Step results are checkpointed as plain JSON (`JsonSerDes`, via
`json_formatter()` in `agent_support.py`). The SDK default wraps values in a
`{"t": ..., "v": ...}` type envelope, which these JSON-only results don't need.

## Project layout

```
src/scout/
  handler.py            # the agent: one durable tool-use loop
  agent_tools.py        # the agent's data tools and their Converse tool specs
  agent_support.py      # UI state publishing, the swag poll body, json_formatter
  swag_service.py       # simulated async swag-reservation API (PENDING → CONFIRMED)
  llm.py                # Bedrock Converse wrapper and the agent's system prompt
  catalog.py            # loads the exhibitor catalog
  durable_sdk.py        # resolves Durable Execution SDK symbols across SDK versions
  state_store.py        # DynamoDB per-execution state read by the web UI
  api.py                # control API Lambda (start / status / decision)
  data/exhibitor_catalog.json
web/index.html          # single-page web UI
template.yaml           # SAM: durable function, control API, DynamoDB, Cognito
requirements.txt
```

The business logic (`agent_tools.py`, `swag_service.py`, `catalog.py`) does not
depend on the durable SDK. Only `handler.py` wires it into steps, callbacks and
polls.

## How the web UI talks to the agent

The durable function runs asynchronously and suspends at the approval gate, so a
browser can't wait on a single request. A small control API sits in between:

```
Browser (web/index.html)
  │  POST /scout                → start a run, get an executionId
  │  GET  /scout/{id}           → poll status, hit list, confirmed guide
  │  POST /scout/{id}/decision  → approve or reject
  ▼
API Gateway (HTTP API) → ScoutApiFunction (api.py)
  │                           │ async invoke (qualified :live ARN)
  ▼                           ▼
DynamoDB (ScoutStateTable) ◀ ExpoBoothScoutFunction (handler.py)
                              publishes state at each phase
```

The agent writes its state to DynamoDB at each phase: `RUNNING`,
`AWAITING_APPROVAL` (with the callback id), `RESERVING` (with each reservation's
status), then one of `CONFIRMED`, `REJECTED`, `FAILED` or `INCOMPLETE`.

- `FAILED` comes from a guard in `handler`. It publishes the failure for the UI,
  then re-raises so the durable service still records it. The guard catches
  `Exception`, not `BaseException`. The SDK's `SuspendExecution` derives from
  `BaseException` so that user code can't swallow it, which keeps the approval
  gate working.
- `INCOMPLETE` means the agent used its whole turn budget (16 rounds by default)
  without calling `finalize_guide`.

The decision route looks up the stored callback id on the server and resumes the
function with `SendDurableExecutionCallbackSuccess`. The callback id is never
sent to the browser.

**Who can call it.** Every API route needs a Cognito access token, checked by
the HTTP API's JWT authorizer. Self sign-up is off, so only users you create can
start a run and spend Bedrock tokens. A run belongs to the user who started it:
anyone else gets a `404`. CORS allows only the `WebOrigin` the stack was
deployed with (default `http://localhost:8000`). Routes are throttled to 2
requests per second with a burst of 5.

## Deploy

You need:

- the [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- a region that supports Lambda durable functions and the `python3.14` runtime
- access to the Bedrock model in that region. The default is the inference
  profile `us.anthropic.claude-sonnet-4-5-20250929-v1:0`. Override it with the
  `BedrockModelId` parameter.

```bash
sam build
sam deploy --guided        # first time: choose the stack name, region and parameters
```

The stack name becomes part of the Cognito login domain
(`<stack-name>-<account-id>`). Keep it lowercase, and leave out `aws`, `amazon`
and `cognito`. To serve the UI from somewhere other than
`http://localhost:8000`, set the `WebOrigin` parameter to that https origin.

| Parameter | Default | Purpose |
|---|---|---|
| `BedrockModelId` | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | Bedrock model or inference-profile id |
| `ApprovalTimeoutHours` | `2` | How long the approval gate waits |
| `WebOrigin` | `http://localhost:8000` | The only origin allowed by CORS and as the sign-in callback |

**Create a user.** Self sign-up is off, so add each person who should use the
demo. `UserPoolId` is a stack output.

```bash
aws cognito-idp admin-create-user --user-pool-id <UserPoolId> \
  --username you@example.com \
  --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true
```

Cognito emails a temporary password, and the first sign-in asks for a new one.
Passwords need at least 12 characters with upper case, lower case, a number and
a symbol.

## Run the UI

Serve `web/` from the `WebOrigin` origin:

```bash
cd web && python3 -m http.server 8000 --bind 127.0.0.1
```

Then open **http://localhost:8000**. Use `localhost` exactly: `127.0.0.1` is a
different origin, so Cognito won't redirect back to it. Opening the file
directly (`file://`) doesn't work either.

1. Paste three stack outputs into the fields under the buttons. The page saves
   them in your browser.
   - `ApiBaseUrl` → **API base**
   - `LoginDomain` → **Login domain**
   - `UserPoolClientId` → **Client id**

   To print them:

   ```bash
   aws cloudformation describe-stacks --stack-name <stack-name> \
     --query "Stacks[0].Outputs[?OutputKey=='ApiBaseUrl'||OutputKey=='LoginDomain'||OutputKey=='UserPoolClientId'].[OutputKey,OutputValue]" \
     --output table
   ```
2. Click **Sign in** and sign in on the Cognito page.
3. Describe what you're building and click **Scout the floor**.
4. When the hit list appears, click **Approve** or **Reject**. After you approve,
   the page shows each swag item go from `PENDING` to `CONFIRMED`, then the
   confirmed guide.

The page signs in with the authorization code flow and PKCE, so it holds no
secret. The access token stays in `sessionStorage` for that tab, and no refresh
token is kept, so you sign in again after an hour.

## Invoke the function directly

You can also start a run without the UI. Durable functions must be invoked with
a **qualified** ARN, so use the `:live` alias the template publishes:

```bash
aws lambda invoke \
  --function-name expo-booth-scout:live \
  --invocation-type Event \
  --cli-binary-format raw-in-base64-out \
  --payload '{"attendee_name": "re:Invent Attendee", "description": "I am building AI agents with serverless and want durable workflows and observability", "max_booths": 5}' \
  response.json
```

When the run reaches the approval gate it suspends. To resume it, open the
Lambda console's **Durable executions** tab, select the execution, select the
pending callback, and use **Actions**. Or, if your AWS CLI has the durable
execution commands and you have the callback id:

```bash
aws lambda send-durable-execution-callback-success \
  --callback-id <callback-id> \
  --cli-binary-format raw-in-base64-out \
  --result '{"approved": true}'
```

Some AWS CLI builds don't have the `*-durable-execution-*` commands yet. If
yours doesn't, use the console or the web UI.

## Watch the durable operations

Each log line from the SDK carries the `operationName` of the step it belongs
to. After a resume, completed operations print nothing, because their results
come from checkpoints:

```bash
aws logs tail /aws/lambda/expo-booth-scout --since 10m --follow \
  | python3 -c 'import sys,json
for line in sys.stdin:
    i = line.find("{")
    if i < 0: continue
    try: d = json.loads(line[i:])
    except ValueError: continue
    print(d.get("operationName","-"), "|", d.get("level"), "|", d.get("message","")[:80])'
```

`executionArn` stays the same across a suspend while `requestId` changes: one
durable execution, several invocations.

After a redeploy, start a **new** run. A run keeps the code version it started
on, even across a suspend.

## Configuration

Environment variables (see `template.yaml` for what the stack sets):

| Variable | Default | Purpose |
|---|---|---|
| `SCOUT_BEDROCK_MODEL_ID` | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | Bedrock model or inference-profile id |
| `SCOUT_AGENT_MAX_TURNS` | `16` | Reasoning rounds before the run ends `INCOMPLETE` |
| `SCOUT_APPROVAL_TIMEOUT_HOURS` | `2` | Approval callback timeout |
| `SCOUT_TOOL_MAX_ATTEMPTS` | `4` | Retry attempts for every tool step |
| `SCOUT_POLL_SECONDS` | `5` | Wait between swag polls |
| `SCOUT_POLL_MAX_ATTEMPTS` | `10` | Polls before the swag reservation fails |
| `SCOUT_SWAG_CONFIRM_POLLS` | `2` | Status checks until a reservation confirms |
| `SCOUT_STATE_TABLE` | (set by the stack) | DynamoDB state table. If unset, state writes are skipped |
| `SCOUT_STATE_TTL_SECONDS` | `86400` | TTL on state items |

## Known limitations

- **Swag reservation is simulated.** Every reservation confirms. There is no
  out-of-stock case and no compensation step.
- **The runtime's durable SDK differs from the PyPI build.** In Lambda,
  `create_wait_strategy` returns a decision that `wait_for_condition` can't
  read, so the poll strategy in `handler.py` is hand-written. `durable_sdk.py`
  looks up each SDK symbol in several module paths for the same reason.
- **`sam build` bundles no dependencies.** `CodeUri` is `src/`, and
  `requirements.txt` sits at the repo root, where SAM's Python builder doesn't
  look. `boto3` and the durable SDK both come from the Lambda runtime. Only move
  `requirements.txt` into `src/` if you need a specific SDK version, and test it
  on AWS: a different major version of the durable SDK may not match what the
  service expects.
- **Cost.** Each run makes several Bedrock calls. Delete the stack when you're
  done: `sam delete`.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.

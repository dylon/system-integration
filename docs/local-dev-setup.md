# Local development stack (docker-first)

This guide brings up the full F1R3FLY stack for the Embers agent-teams demo with **docker
images as the delivery mechanism**: one standalone Rust node (cost-accounting branch), the
Embers backend with **F1r3drive co-located inside its container**, the Embers web frontend,
the F1R3Sky AT-proto backend, and (optionally) the F1R3Sky web frontend. Everything runs in
containers; the host needs Docker and nothing else — no Rust, Java, Node, FUSE, or Nix.

```text
browser ──▶ embers-frontend :8081 ──▶ embers :8080 ─┬─▶ rnode.rust-standalone :40401/:40403
                                                    ├─▶ f1r3sky (PDS :2583)
                                       f1r3drive ───┘   (inside the embers container,
                                                         FUSE mount /mnt/f1r3drive)
```

The complete topology, with every container, port, volume, and the in-container FUSE flow,
is drawn in [diagrams/local-dev-stack.svg](diagrams/local-dev-stack.svg)
(source: [diagrams/local-dev-stack.puml](diagrams/local-dev-stack.puml)).

## Prerequisites

- **Docker** with Compose v2.24 or newer (the port-override examples use the `!override`
  tag). Docker Desktop on macOS/Windows and Docker Engine on Linux both work — see the
  platform note below. Allow at least 8 GB RAM for the full set.
- **GitHub personal access token** with `read:packages`, available in an `.npmrc` that maps
  the `@f1r3fly-io` scope to GitHub Packages with a literal token (the embers-frontend and
  f1r3sky frontend builds consume it as a BuildKit secret):

  ```ini
  @f1r3fly-io:registry=https://npm.pkg.github.com
  //npm.pkg.github.com/:_authToken=<your token>
  ```

  Pass the file that holds the **literal** token. The embers-frontend repo's checked-in
  `.npmrc` uses an `${NPM_TOKEN}` placeholder, which BuildKit does not expand — point the
  secret at `~/.npmrc` (or export `NPM_TOKEN` and render the file first).
- **OpenAI API key** — optional. The stack boots and passes verification without it;
  it is required only to run agent teams whose activities call the `rho:ai:*` processes
  (GPT text, DALL-E, TTS). The **Rust node reads `OPENAI_API_KEY`**
  (`OPENAI_SCALA_CLIENT_API_KEY` is the Scala node's variable); set it in `.env.node`
  together with `OPENAI_ENABLED=true`. See
  [enable-openai-on-node.md](enable-openai-on-node.md). Beware precedence: compose lets
  shell-exported `OPENAI_*` variables silently override `.env.node`.

### Platform note: Linux, macOS, and Windows

Docker containers always run on a Linux kernel — natively on Linux, inside Docker Desktop's
VM on macOS, and inside the WSL2 kernel on Windows — and all three ship the `fuse` module.
The F1r3drive FUSE filesystem that Embers requires is mounted **inside** the embers
container and never crosses a container or host boundary, so the stack behaves identically
on all three platforms. No macFUSE, WinFsp, or host Java is needed. The embers service
carries the privileges FUSE-in-a-container needs, already declared in the compose file:

- `devices: [/dev/fuse]`
- `cap_add: [SYS_ADMIN]`
- `security_opt: ["apparmor:unconfined"]` (required on AppArmor hosts such as Ubuntu;
  harmless elsewhere)

## Repositories and branches

Clone everything as **siblings under one workspace directory**. The node image build
additionally needs the cost-accounting transpiler checkout next to the node repo.

| Repository | Branch | Needed for |
|---|---|---|
| `system-integration` | `feat/local-dev-scripts` | compose files, env, genesis, scripts (this repo) |
| `embers` | `dylon/embers-demo-fixes` | backend image (includes the F1r3drive build stages) |
| `embers-frontend` | `dylon/embers-demo-fixes` | frontend image |
| `f1r3node-rust` | `feature/cost-accounted-rho` | node image |
| `rholang-rs-cost-accounting-transpiler` | `main` | node image (path dependency of the branch above) |
| `f1r3drive` | `main` | embers image (local build-context override; the default is a git clone) |
| `f1r3sky-backend` | `main` | firesky-ts image |
| `f1r3sky` | `docs/known-issues-and-dockerfile` | optional F1R3Sky web frontend image |

```bash
WORKSPACE=~/f1r3fly && mkdir -p "$WORKSPACE" && cd "$WORKSPACE"
git clone -b feat/local-dev-scripts git@github.com:F1R3FLY-io/system-integration.git
git clone -b dylon/embers-demo-fixes git@github.com:F1R3FLY-io/embers.git
git clone -b dylon/embers-demo-fixes git@github.com:F1R3FLY-io/embers-frontend.git
git clone -b feature/cost-accounted-rho git@github.com:F1R3FLY-io/f1r3node-rust.git
git clone git@github.com:F1R3FLY-io/rholang-rs-cost-accounting-transpiler.git
git clone git@github.com:F1R3FLY-io/f1r3drive.git
git clone git@github.com:F1R3FLY-io/f1r3sky-backend.git
git clone -b docs/known-issues-and-dockerfile git@github.com:F1R3FLY-io/f1r3sky.git   # optional
```

The published registry images cannot serve this stack today: `f1r3flyindustries/f1r3fly-rust`
is built from `master`, which does not carry the cost-accounting transpiler the agent-teams
demo requires (the older `f1r3flyindustries/f1r3fly-rust-node` lineage is frozen), and the
published `f1r3flyio/embers:latest` predates the F1r3drive continuation store, without which
the current backend does not start. Until those branches merge and releases are cut, build
locally as below — the commands are copy-paste complete.

## Build the images

All commands run from the workspace root. Build the node first; it is the longest build.

**1. Node** — the branch's root `Cargo.toml` patches the Rholang crates to the sibling
transpiler checkout, which sits outside the docker build context, so the build injects it
as a named build context. Derive the one-line-extended Dockerfile and build:

```bash
cd f1r3node-rust
awk '{print} /^COPY --from=xx \/ \/$/ && !done {print "COPY --from=transpiler / /rholang-rs-cost-accounting-transpiler/"; done=1} /^COPY node\/ .\/node\/$/ {print "COPY formal/loom/cost_accounting/ ./formal/loom/cost_accounting/"}' \
    node/Dockerfile > /tmp/node-costacct.Dockerfile
docker buildx build --load -f /tmp/node-costacct.Dockerfile \
    --build-context transpiler=../rholang-rs-cost-accounting-transpiler \
    -t f1r3flyindustries/f1r3fly-rust-node:cost-accounted-local .
cd ..
```

**2. Embers backend (with F1r3drive inside)** — the dockerfile builds the F1r3drive fat JAR
in a Gradle stage; pass the local checkout to build offline against your exact tree
(omit `--build-context` to let the dockerfile clone from GitHub instead):

```bash
cd embers
docker buildx build --load -f docker/embers.dockerfile \
    --build-context f1r3drive-src=../f1r3drive \
    -t f1r3flyio/embers:local .
cd ..
```

**3. Embers frontend:**

```bash
cd embers-frontend
docker buildx build --load -f apps/embers/Dockerfile \
    --secret id=npmrc,src=$HOME/.npmrc \
    -t f1r3flyio/embers-frontend:local .
cd ..
```

**4. F1R3Sky backend:**

```bash
cd f1r3sky-backend
docker buildx build --load -f Dockerfile.dev -t f1r3flyindustries/firesky-ts:local .
cd ..
```

**5. F1R3Sky web frontend (optional, ~10-15 min):**

```bash
cd f1r3sky
docker buildx build --load -f Dockerfile \
    --build-arg NPM_TOKEN=<your token> \
    --build-arg EXPO_PUBLIC_EMBERS_API_URL=http://localhost:8080 \
    -t f1r3flyio/firesky-frontend:local .
cd ..
```

| Image | Source | Role |
|---|---|---|
| `f1r3flyindustries/f1r3fly-rust-node:cost-accounted-local` | f1r3node-rust + transpiler | standalone node |
| `f1r3flyio/embers:local` | embers + f1r3drive | backend + co-located FUSE store |
| `f1r3flyio/embers-frontend:local` | embers-frontend | web UI (nginx) |
| `f1r3flyindustries/firesky-ts:local` | f1r3sky-backend | AT-proto PDS/AppView/Ozone/PLC |
| `f1r3flyio/firesky-frontend:local` | f1r3sky (optional) | F1R3Sky web UI |
| `postgres:16-alpine`, `redis:7-alpine` | Docker Hub | stock infrastructure (pulled) |

## Configuration

- **`.env.node`** (repo root) — node keypairs and the OpenAI toggles
  (`OPENAI_ENABLED`, `OPENAI_SCALA_CLIENT_API_KEY`). The standalone container name
  defaults to `rnode.standalone`; every command below overrides it to
  `rnode.rust-standalone`, which is the DNS name the env file and scripts expect.
- **`env/embers.rust-standalone.env`** — the tracked Embers + F1r3drive environment for
  this stack (the compose file reads `../env/${EMBERS_ENV:-embers.rust-standalone.env}`).
  It mirrors the natively verified embers-local-stack configuration: the `EMBERS__*`
  variables configure the backend (both the mainnet and testnet modules point at the one
  standalone node: deploys over gRPC `:40401`, observer reads and WebSocket events over
  HTTP `:40403`), and the `F1R3DRIVE_*` variables configure the co-located drive process
  (both of its gRPC channels also target `:40401`). Every key in the file is a published,
  test-only local-development key — never reuse them beyond a private chain.
- **`genesis/standalone-wallets.txt`** — funds five wallets: the demo sign-in wallet, the
  three mainnet wallets (including the Embers service wallet, which is also the F1r3drive
  wallet), and the testnet service wallet. The single standalone validator serves **both**
  Embers networks, so both service wallets must be funded at genesis — an unfunded service
  wallet makes the backend's bootstrap init deploys fail. Changing genesis invalidates any
  existing node volume: tear down with `--clean` / `down -v` first.

## Start the stack

```bash
# 1. The node (compose project rust-standalone; network rust-standalone_f1r3fly-standalone)
STANDALONE_HOST=rnode.rust-standalone \
F1R3FLY_RUST_IMAGE=f1r3flyindustries/f1r3fly-rust-node:cost-accounted-local \
docker compose -p rust-standalone -f compose/f1r3node-rust-standalone.yml \
    --env-file .env.node up -d

# 2. Everything else (waits for the node, resolves F1R3SKY_IP, creates user1.test)
./scripts/start-all.sh --node rust-standalone
```

Stop, and optionally wipe volumes (required after a genesis change):

```bash
./scripts/stop-all.sh --node rust-standalone           # stop services
./scripts/stop-all.sh --node rust-standalone --clean   # stop + remove volumes
docker compose -p rust-standalone -f compose/f1r3node-rust-standalone.yml down       # node
docker compose -p rust-standalone -f compose/f1r3node-rust-standalone.yml down -v    # node + data
```

`scripts/status.sh` shows the state of every container and probes the Embers endpoints.

## How F1r3drive runs inside the embers container

Embers hard-requires its Agent Teams continuation store to sit on a FUSE mount whose
fsname contains `f1r3drive` (`verify_f1r3drive_mount` in the backend refuses ordinary
disk), and cross-container FUSE mount sharing is not portable — Docker Desktop cannot
propagate mounts between containers. The embers image therefore supervises both processes
in one container (`docker/embers-entrypoint.sh` in the embers repo):

1. wait for the node HTTP API (`NODE_HTTP_WAIT_URL`);
2. start `f1r3drive-app.jar` (JRE 17), which mounts `/mnt/f1r3drive` with
   `fsname=f1r3drive` and unlocks the service wallet's directory;
3. wait until the mount and `/mnt/f1r3drive/<service-address>` exist;
4. start `embers`; if either process exits, the other is stopped and the container exits
   (compose restarts it) — the containerized equivalent of the native stack's `BindsTo`
   coupling.

The drive's AES cipher key is generated on first run at
`/data/f1r3drive/f1r3drive-cipher.key`, persisted on the `embers-f1r3drive` volume.
The key encrypts everything F1r3drive writes to the chain: **wipe that volume only
together with the node volume**, otherwise previously written drive content becomes
undecryptable.

Propose semantics, preserved exactly as verified natively: the standalone validator runs
`--autopropose` with heartbeat proposing enabled, and f1r3drive runs `--manual-propose`
(it proposes after each of its own deploys and waits for finalization). Occasional
"propose already in progress" warnings in the node log are benign.

## Modes

`start-all.sh` / `stop-all.sh` accept `--node <mode>`:

| Mode | Node compose file | Embers env file | Status |
|---|---|---|---|
| `rust-standalone` | `compose/f1r3node-rust-standalone.yml` | `env/embers.rust-standalone.env` | **maintained and verified — use this** |
| `shard` (default when `--node` is omitted!) | `compose/f1r3node-rust.yml` | `services/embers/embers.env` (untracked legacy path) | legacy shardctl flow, not maintained for the demo |
| `scala-standalone` | `compose/f1r3node-standalone.yml` | `env/embers.scala-standalone.env` (not provided) | Scala node path, no maintained env file |

Always pass `--node rust-standalone` explicitly; omitting `--node` silently targets shard
mode.

## Verifying the setup

Run these after `start-all.sh` completes. Together they prove the FUSE co-location, the
full deploy → propose → finalize → observer-read chain, a real agent-teams read, a real
funded deploy, and both UIs.

```bash
# 1. F1r3drive mounted inside the embers container (fstype fuse.*, fsname f1r3drive)
docker exec compose-embers-1 cat /proc/mounts | grep f1r3drive
#    -> f1r3drive /mnt/f1r3drive fuse.f1r3drive rw,...

# 2. The supervisor reached "mount ready" and embers did not reject the store
docker logs compose-embers-1 2>&1 | grep '\[entrypoint\]'
#    -> ... f1r3drive FUSE mount ready at /mnt/f1r3drive/1111jyBB...

# 3. Readiness: 200 only after all five bootstrap init deploys finalized
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/api/service/ready
#    -> 200   (first boot takes a few minutes: genesis + init deploys)

# 4. Agent-teams read (the same probe start-all.sh polls)
curl -s http://localhost:8080/api/ai-agents-teams/1111AtahZeefej4tvVR6ti9TJtv8yxLebT31SCEVDCKMNikBk5r3g
#    -> 200 with a JSON body containing "agents_teams"

# 5. Write path: a real funded deploy through the testnet module
curl -s -X POST http://localhost:8080/api/testnet/wallet
#    -> 200 with a wallet address and key

# 6. Frontend serves with the injected API URL
curl -s http://localhost:8081/config.js
#    -> window.API_URL = "http://localhost:8080";

# 7. F1R3Sky PDS is alive and serving its configuration
curl -s http://localhost:2583/xrpc/com.atproto.server.describeServer
#    -> {"did":"did:web:localhost","availableUserDomains":[".test",".example"],...}
```

## Service URLs

| Service | URL | Credentials |
|---|---|---|
| Embers Frontend | http://localhost:8081 | Sign-in key: `5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657` |
| F1R3Sky Frontend | http://localhost:8100 | `user1.test` / `password123` (created by `start-all.sh`) |
| Embers API | http://localhost:8080 | — |
| Embers Swagger | http://localhost:8080/swagger-ui/index.html | — |
| F1R3Sky PDS | http://localhost:2583 | — |

Additional F1R3Sky accounts can be created via the PDS API (the frontend captcha does not
work locally):

```bash
curl -X POST http://localhost:2583/xrpc/com.atproto.server.createAccount \
  -H 'Content-Type: application/json' \
  -d '{"handle": "user2.test", "email": "user2@test.com", "password": "password123"}'
```

## Demo flow

1. **Embers frontend** (`http://localhost:8081`) → Sign in with the bootstrap key →
   Create agent team → Build graph (input → text model → output) → Save → Deploy
2. **Embers frontend** → Publish agent team:
   - PDS URL: `http://f1r3sky:2583`
   - Handle: `myagent.test`
   - Email: any (must be unique per publish)
   - Password: any
3. **F1R3Sky frontend** (`http://localhost:8100`) → Sign in as `user1.test` → Load wallet →
   Post tagging `@myagent.test <your prompt>`
4. The agent auto-replies with GPT text (~30-60 s). Requires the OpenAI key on the node.

A wallet must be loaded in the F1R3Sky frontend for the @mention trigger to work — it signs
the runOnFiresky transaction.

## Port reference

| Port | Service |
|---|---|
| 40400-40405 | standalone node (protocol, gRPC ext/int, HTTP, discovery, admin) |
| 8080 | Embers API (container port 3000) |
| 8081 | Embers frontend (container port 80) |
| 2581 | F1R3Sky (auxiliary) |
| 2582 | F1R3Sky DID PLC |
| 2583 | F1R3Sky PDS |
| 2584 | F1R3Sky AppView |
| 2587 | F1R3Sky Ozone |
| 8100 | F1R3Sky frontend (optional) |

**Coexisting with another stack** (for example the native embers-local-stack on the same
machine): give the compose projects different names and override only the published ports —
container-to-container traffic stays on the compose network and needs no host ports. With
Compose ≥ 2.24, an override file replaces port lists wholesale:

```yaml
# node.override.yml
services:
  standalone:
    ports: !override
      - "44401:40401"
      - "44403:40403"
```

```bash
docker compose -p my-test -f compose/f1r3node-rust-standalone.yml -f node.override.yml ... up -d
```

## Troubleshooting

- **Embers exits: `failed to open the F1r3drive Agent Teams continuation store` /
  "not inside the fsname=f1r3drive FUSE mount"** — the FUSE privileges are missing or the
  mount never appeared. Check `docker logs compose-embers-1` for the `[entrypoint]` lines:
  `/dev/fuse is missing` means the compose privileges (`devices`, `cap_add`,
  `security_opt`) were not applied (verify with `docker compose ... config`);
  `did not mount and unlock` with the java process alive means the drive could not reach
  the node or unlock the wallet — check the node is healthy and genesis is fresh.
- **Embers exits immediately with a configuration error** — the env file has drifted from
  the backend's configuration schema (`packages/embers/src/configuration.rs`). All
  `EMBERS__*` keys in `env/embers.rust-standalone.env` are required; the hex keys are
  64 hex chars; the continuation-store encryption and capability keys must differ.
- **`failed to deserialize intermediate model` or gRPC decode errors in embers** — the
  node image was not built from `feature/cost-accounted-rho` (wire-format mismatch).
  Rebuild the node image exactly as in "Build the images"; do not substitute
  `f1r3flyindustries/f1r3fly-rust:latest` or the frozen `f1r3fly-rust-node` tags.
- **Init deploys never finalize / `ready` stays non-200 / agent-teams reads return `Nil`**
  — genesis is missing a funded wallet or a stale volume predates the current
  `standalone-wallets.txt`. Tear down both projects with `-v`/`--clean` and start fresh;
  confirm the node command includes `--autopropose` (`docker inspect rnode.rust-standalone`).
- **Port already in use on `up`** — another stack holds the port. Use the coexistence
  override pattern above, or stop the other stack.
- **Node crash-loops at startup: `OpenAI API key is not configured ... when openai is
  enabled`** — `OPENAI_ENABLED=true` reached the container (often a shell-exported
  variable overriding `.env.node`; compose gives the OS environment precedence) without
  `OPENAI_API_KEY`. Either export the key or force-disable for the session:
  `OPENAI_ENABLED=false docker compose ... up -d --force-recreate`.
- **Agent replies are empty** — the OpenAI key is not loaded on the node:
  `docker exec rnode.rust-standalone env | grep -i openai` (the Rust node needs
  `OPENAI_API_KEY`), fix `.env.node`, and recreate the node container.
- **"Email already taken" when publishing** — a previous publish used the email; use a
  fresh one or wipe with `--clean`.
- **Docker build cache bloat after many rebuilds** — `docker builder prune -f`.

## Known issues

- `scripts/e2e-demo-test.sh` (the scripted 8-phase demo) currently cannot run from a fresh
  clone: `e2e-demo/demo-test.ts` imports `e2e-demo/lib/`, which was never committed. The
  verification checklist above covers the stack end-to-end in its place.
- Per-service known-issue documents live in the service repos: embers
  `docs/embers-rust-node-updates.md`, embers-frontend `docs/known-issues.md`, f1r3sky
  `docs/known-issues.md`.

## Related PRs

- embers#168, embers-frontend#196, system-integration#37 (this branch).

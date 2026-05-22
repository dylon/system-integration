# test_cost_accounting

## Purpose

Verifies cost-accounting invariants at the Docker/API boundary under the current permit-frontier design. Three regression scenarios — exact-charge on out-of-phlo deploys, deterministic cost across parallel permutations, and play/replay cost consistency for interacting COMM bodies — are exercised against a real shard.

## Background

The historical [concurrent-RSpace proposal](https://example.internal/concurrent-rspace-architecture.md) was rejected before the current cost-accounting work, but the three failure modes it identified (TOCTOU on phlo, COMM trigger-order sensitivity, and play/replay cost mismatch) are retained here as regression scenarios under the current permit-frontier design. Under permit-frontier accounting:

- Out-of-phlo deploys are charged exactly to `phlo_limit` (not 0, not the would-have-been cost), and the block they land in must still finalize on every node.
- The exploratory-deploy cost reported by `/api/explore-deploy` is a deterministic function of the program text — equivalent parallel permutations must return the same cost.
- A deploy that exercises interacting COMM bodies must produce the same cost during play and replay; otherwise the second-pass validator rejects the block.

## Tests (3)

### test_low_phlo_parallel_fanout_charges_to_limit_and_finalizes

1. Deploys an 8-way parallel fanout (`@0!(0) | @1!(1) | … | @7!(7)`) on V1 with `phlo_limit=20` (well below the ~8×14 = ~112 phlo needed for all sends)
2. Waits for inclusion via `wait_for_deploy_included`
3. Fetches the full block and calls `assert_deploy_errored(block_info, deploy_id)`
4. Pulls the deploy record out of `block_info.deploys` and asserts `deploy.cost == 20` (exact phlo-limit boundary)
5. Polls until **all nodes** advance LFB by 2+ blocks past the errored deploy's block

**What it proves:**
- Permit-frontier out-of-phlo charging is exact, not approximate, and not zero
- The errored deploy does not break finalization on the proposing validator or its peers

### test_parallel_permutation_explore_cost_is_stable

1. Builds four logically-equivalent permutations of a parallel send/receive pair (`@0!(0) | @1!(1) | for (_ <- @0) { 0 } | for (_ <- @1) { 0 }`)
2. For each permutation, calls `POST /api/explore-deploy` on the readonly node
3. Asserts every returned `cost` is a positive int
4. Asserts `len(set(costs)) == 1` — all four permutations return the same cost

**What it proves:**
- Exploratory deploy is read-only (no block created — verified by reusing the session shard with no LFB advance check)
- The cost computation is invariant under parallel reordering at the top level

### test_interacting_comm_bodies_finalize_without_replay_cost_mismatch

1. Deploys the body-interleaving repro (`@0!(0) | for (_ <- @0) { @2!(0) } | @1!(0) | for (_ <- @1) { for (_ <- @2) { 0 } }`) on V1 with `phlo_limit=100_000`
2. Waits for inclusion via `wait_for_deploy_included`
3. Fetches the full block and calls `assert_deploy_succeeded(block_info, deploy_id)`
4. Polls until **all nodes** advance LFB by 2+ blocks

**What it proves:**
- Permit-frontier accounting produces the same cost during play and replay for deploys whose COMM trigger order is sensitive to scheduler choice
- The block is accepted by every peer (no `replay cost mismatch` validation rejection)

## Setup

- **Topology**: Session-scoped `shared_shard` (3 validators + readonly)
- **FTT**: From `conf/rust.conf`
- **Heartbeat**: Enabled (for automatic block inclusion)

## Key assertions

- Out-of-phlo: `assert_deploy_errored(block_info, deploy_id)` AND `deploy.cost == phlo_limit`
- Explore-deploy cost stability: every variant returns positive int, `len(set(costs)) == 1`
- Body-interleaving: `assert_deploy_succeeded(block_info, deploy_id)` AND LFB advances on all 4 nodes

## Infrastructure used

- Session-scoped `shared_shard` fixture (3 validators + readonly)
- `assert_deploy_succeeded()`, `assert_deploy_errored()` from `infra/assertions.py` (delegate to `f1r3fly.deploy`)
- `wait_for_deploy_included()` from `infra/polling.py`
- `poll_until()` for cross-node LFB advancement (local `_wait_lfb_all_nodes_at_least` helper)
- `Node.deploy_string()` for the two block-affecting deploys
- `Node.api_post("/explore-deploy", {"term": ...})` on the readonly node for the cost-stability test
- `timeouts.custom(120)` / `timeouts.custom(180)` for the scaled per-test budgets

## Related

- [test_deployment](test_deployment.md) -- the canonical insufficient-phlo regression also asserts `deploy.cost == phlo_limit`
- [test_web_api](test_web_api.md) -- `test_explore_deploy_returns_cost` exercises the same `/api/explore-deploy` endpoint for a single term
- [test_query_endpoints](test_query_endpoints.md) -- `test_estimate_cost` exercises `/api/estimate-cost` (a separate cost-estimation surface)

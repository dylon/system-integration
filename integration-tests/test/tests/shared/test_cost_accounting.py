"""
Cost-accounting integration tests.

Regression tests for the current permit-frontier cost-accounting design.
The historical concurrent-RSpace proposal is used as the bug catalogue
only; the proposal itself is not the accepted architecture.

Three scenarios:
1. Out-of-phlo parallel fanout charges exactly to the phlo limit and the
   shard continues finalizing.
2. Equivalent parallel send/receive permutations return positive, equal
   explore-deploy costs.
3. Interacting COMM bodies finalize across validators with no play/replay
   cost mismatch.
"""

import pytest

from ...infra.assertions import assert_deploy_errored, assert_deploy_succeeded
from ...infra.keys import VALIDATOR1_ID
from ...infra.polling import poll_until, wait_for_deploy_included

pytestmark = pytest.mark.xdist_group("shared")


PARALLEL_FANOUT_LOW_PHLO = """
@0!(0) | @1!(1) | @2!(2) | @3!(3) | @4!(4) | @5!(5) | @6!(6) | @7!(7)
"""

BODY_INTERLEAVING_REPRO = """
@0!(0) |
for (_ <- @0) { @2!(0) } |
@1!(0) |
for (_ <- @1) { for (_ <- @2) { 0 } }
"""


def _wait_lfb_all_nodes_at_least(nodes, target: int, timeout: int) -> None:
    """Poll until every node's LFB reaches ``target``."""
    remaining = {n.name for n in nodes}

    def _check():
        for node in nodes:
            if node.name not in remaining:
                continue
            try:
                if node.last_finalized_block().blockInfo.blockNumber >= target:
                    remaining.discard(node.name)
            except Exception:
                pass
        return True if not remaining else None

    poll_until(
        predicate=_check,
        timeout=timeout,
        interval=5.0,
        description=f"LFB >= #{target} on all {len(nodes)} nodes",
    )


def _deploy_from_block(block_info, deploy_id: str):
    return next(d for d in block_info.deploys if d.sig == deploy_id)


def test_low_phlo_parallel_fanout_charges_to_limit_and_finalizes(
    shared_shard, timeouts
) -> None:
    """Parallel fanout with insufficient phlo must charge to the limit and not stall finalization."""
    v1 = shared_shard.node("validator1")

    phlo_limit = 20
    deploy_id = v1.deploy_string(
        PARALLEL_FANOUT_LOW_PHLO,
        VALIDATOR1_ID.private_key(),
        phlo_limit=phlo_limit,
        phlo_price=1,
    )

    info = wait_for_deploy_included(v1, deploy_id, timeout=timeouts.custom(120))
    block_info = v1.get_block(info.blockHash)
    assert_deploy_errored(block_info, deploy_id)

    deploy = _deploy_from_block(block_info, deploy_id)
    assert deploy.cost == phlo_limit, (
        f"Out-of-phlo fanout must charge exact phlo limit, got {deploy.cost}"
    )

    _wait_lfb_all_nodes_at_least(
        shared_shard.all_nodes,
        info.blockNumber + 2,
        timeout=timeouts.custom(180),
    )


def test_parallel_permutation_explore_cost_is_stable(shared_shard) -> None:
    """Equivalent parallel send/receive permutations must return positive, equal costs."""
    ro = shared_shard.readonly
    variants = [
        "@0!(0) | @1!(1) | for (_ <- @0) { 0 } | for (_ <- @1) { 0 }",
        "for (_ <- @1) { 0 } | @1!(1) | for (_ <- @0) { 0 } | @0!(0)",
        "@1!(1) | for (_ <- @0) { 0 } | @0!(0) | for (_ <- @1) { 0 }",
        "for (_ <- @0) { 0 } | for (_ <- @1) { 0 } | @0!(0) | @1!(1)",
    ]

    costs = []
    for term in variants:
        result = ro.api_post("/explore-deploy", {"term": term}).json()
        cost = result["cost"]
        assert isinstance(cost, int) and cost > 0, (
            f"explore-deploy returned non-positive cost {cost} for term: {term}"
        )
        costs.append(cost)

    assert len(set(costs)) == 1, f"Equivalent parallel permutations changed cost: {costs}"


def test_interacting_comm_bodies_finalize_without_replay_cost_mismatch(
    shared_shard, timeouts
) -> None:
    """Body-interleaving repro must execute, finalize, and not cause play/replay cost mismatch."""
    v1 = shared_shard.node("validator1")

    deploy_id = v1.deploy_string(
        BODY_INTERLEAVING_REPRO,
        VALIDATOR1_ID.private_key(),
        phlo_limit=100_000,
        phlo_price=1,
    )

    info = wait_for_deploy_included(v1, deploy_id, timeout=timeouts.custom(120))
    block_info = v1.get_block(info.blockHash)
    assert_deploy_succeeded(block_info, deploy_id)

    _wait_lfb_all_nodes_at_least(
        shared_shard.all_nodes,
        info.blockNumber + 2,
        timeout=timeouts.custom(180),
    )

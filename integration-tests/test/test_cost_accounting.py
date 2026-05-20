"""
Cost-accounting integration tests.

These are Rust-node regression tests for the current permit-frontier
cost-accounting design. They use the historical concurrent RSpace proposal as
the bug catalogue only; the proposal itself is not the accepted architecture.
"""

import time
from typing import List

import pytest
from docker.client import DockerClient

from .conftest import (
    ALL_CONTAINERS,
    VALIDATOR1_KEY,
    assert_containers_running,
)
from .http_client import HttpClient
from .rnode import DEFAULT_IMAGE, Node

pytestmark = [
    pytest.mark.xdist_group("shard"),
    pytest.mark.skipif(
        "rust" not in DEFAULT_IMAGE.lower(),
        reason="cost-accounting permit-frontier regressions are Rust-node specific",
    ),
]


PARALLEL_FANOUT_LOW_PHLO = """
@0!(0) | @1!(1) | @2!(2) | @3!(3) | @4!(4) | @5!(5) | @6!(6) | @7!(7)
"""

BODY_INTERLEAVING_REPRO = """
@0!(0) |
for (_ <- @0) { @2!(0) } |
@1!(0) |
for (_ <- @1) { for (_ <- @2) { 0 } }
"""


def _wait_for_deploy_in_block(node: Node, deploy_id: str, timeout: float):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            light_block = node.find_deploy(deploy_id)
            block_info = node.get_block(light_block.blockHash)
            return light_block.blockHash, light_block.blockNumber, block_info
        except Exception:
            time.sleep(3)
    raise AssertionError(f"Deploy {deploy_id[:24]} was not included in a block within {timeout}s")


def _wait_for_all_nodes_lfb(all_nodes: List[Node], target: int, timeout: float) -> None:
    deadline = time.time() + timeout
    reached = {node.name: 0 for node in all_nodes}

    while time.time() < deadline:
        all_done = True
        for node in all_nodes:
            if reached[node.name] >= target:
                continue
            try:
                lfb = node.last_finalized_block()
                reached[node.name] = lfb.blockInfo.blockNumber
            except Exception:
                pass
            if reached[node.name] < target:
                all_done = False
        if all_done:
            return
        time.sleep(5)

    stalled = [f"{name} at #{block}" for name, block in reached.items() if block < target]
    raise AssertionError(
        f"LFB did not reach #{target} on all nodes within {timeout}s: {', '.join(stalled)}"
    )


def _deploy_from_block(block_info, deploy_id: str):
    for deploy in block_info.deploys:
        if deploy.sig == deploy_id:
            return deploy
    raise AssertionError(f"Deploy {deploy_id[:24]} not found in fetched block")


def test_low_phlo_parallel_fanout_charges_to_limit_and_finalizes(
    docker_client: DockerClient,
    testing_context,
    validator1_node: Node,
    all_nodes: List[Node],
) -> None:
    assert_containers_running(docker_client, ALL_CONTAINERS)

    phlo_limit = 20
    deploy_id = validator1_node.deploy_string(
        PARALLEL_FANOUT_LOW_PHLO,
        VALIDATOR1_KEY,
        phlo_limit=phlo_limit,
        phlo_price=1,
    )

    find_timeout = int(120 * testing_context.timeout_scale)
    _, deploy_block, block_info = _wait_for_deploy_in_block(
        validator1_node,
        deploy_id,
        find_timeout,
    )
    deploy = _deploy_from_block(block_info, deploy_id)

    assert deploy.errored, "Parallel fanout with insufficient phlo must be marked errored"
    assert (
        deploy.cost == phlo_limit
    ), f"Out-of-phlo fanout must charge exact phlo limit, got {deploy.cost}"

    _wait_for_all_nodes_lfb(
        all_nodes,
        deploy_block + 2,
        int(180 * testing_context.timeout_scale),
    )


def test_parallel_permutation_explore_cost_is_stable(readonly_node: Node) -> None:
    client = HttpClient("localhost", readonly_node.get_http_port())
    variants = [
        "@0!(0) | @1!(1) | for (_ <- @0) { 0 } | for (_ <- @1) { 0 }",
        "for (_ <- @1) { 0 } | @1!(1) | for (_ <- @0) { 0 } | @0!(0)",
        "@1!(1) | for (_ <- @0) { 0 } | @0!(0) | for (_ <- @1) { 0 }",
        "for (_ <- @0) { 0 } | for (_ <- @1) { 0 } | @0!(0) | @1!(1)",
    ]

    costs = [client.explore_deploy(term)["cost"] for term in variants]

    assert all(isinstance(cost, int) and cost > 0 for cost in costs)
    assert len(set(costs)) == 1, f"Equivalent parallel permutations changed cost: {costs}"


def test_interacting_comm_bodies_finalize_without_replay_cost_mismatch(
    docker_client: DockerClient,
    testing_context,
    validator1_node: Node,
    all_nodes: List[Node],
) -> None:
    assert_containers_running(docker_client, ALL_CONTAINERS)

    deploy_id = validator1_node.deploy_string(
        BODY_INTERLEAVING_REPRO,
        VALIDATOR1_KEY,
        phlo_limit=100_000,
        phlo_price=1,
    )

    find_timeout = int(120 * testing_context.timeout_scale)
    _, deploy_block, block_info = _wait_for_deploy_in_block(
        validator1_node,
        deploy_id,
        find_timeout,
    )
    deploy = _deploy_from_block(block_info, deploy_id)

    assert (
        not deploy.errored
    ), f"Body-interleaving replay repro should execute successfully: {deploy.systemDeployError}"
    assert deploy.cost > 0

    _wait_for_all_nodes_lfb(
        all_nodes,
        deploy_block + 2,
        int(180 * testing_context.timeout_scale),
    )

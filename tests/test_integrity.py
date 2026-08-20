from datetime import datetime, timezone

import pytest
from conftest import proposal_markdown
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_scheduler.adapters.harness import FakeHarnessAdapter
from agent_scheduler.domain.compiler import CompilationContext, compile_task
from agent_scheduler.domain.models import Review, ReviewDecision, Revision, new_id, utc_now
from agent_scheduler.integrity import canonical_bytes, generate_keypair, verify_model

IMAGE = "harbor.sourcefind.cn:5443/dcu/admin/base/custom@sha256:158bdfd1567477cc4d7b276ba9328b2d29b9c8bcd996d11921a9ea855dbfb238"


def inputs(cards: int = 2):
    proposal_id = new_id("prop")
    revision = Revision(
        revision_id=new_id("rev"),
        proposal_id=proposal_id,
        number=1,
        markdown=proposal_markdown(cards),
        created_at=utc_now(),
    )
    facts = FakeHarnessAdapter().process(revision)
    review = Review(
        review_id=new_id("review"),
        proposal_id=proposal_id,
        revision_id=revision.revision_id,
        decision=ReviewDecision.APPROVE,
        rationale="ok",
        created_at=utc_now(),
    )
    context = CompilationContext(
        task_id=new_id("task"),
        execution_id=new_id("exec"),
        proposal_id=proposal_id,
        revision_id=revision.revision_id,
        facts_id=facts.facts_id,
        review_id=review.review_id,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        key_id="test-key",
        policy_version="policy-v1",
        max_workers=1,
        allowed_worker_ids=("worker-local-01",),
        allowed_container=(
            "worker-local-01",
            "fh-sglang-deepseek-v4-flash",
            "zz_chentian",
        ),
        allowed_image_digest=IMAGE,
    )
    return revision, facts, review, context


def test_compile_is_deterministic_and_signed():
    revision, facts, review, context = inputs()
    private, public = generate_keypair()
    first = compile_task(revision, facts, review, context, private)
    second = compile_task(revision, facts, review, context, private)
    assert first == second
    assert first.content_hash
    assert verify_model(first, public)
    assert not verify_model(first.model_copy(update={"policy_version": "tampered"}), public)
    assert first.signature
    assert not verify_model(first.model_copy(update={"signature": first.signature + "!"}), public)


def test_rejects_non_approved_review():
    revision, facts, review, context = inputs()
    private, _ = generate_keypair()
    review = review.model_copy(update={"decision": ReviewDecision.REQUEST_CHANGES})
    with pytest.raises(ValueError, match="approved"):
        compile_task(revision, facts, review, context, private)


def test_rfc8785_external_shape_vector():
    assert canonical_bytes({"b": "x", "a": 1}) == b'{"a":1,"b":"x"}'


def test_rfc8032_ed25519_test_vector_one():
    seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    expected_public = bytes.fromhex(
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    )
    expected_signature = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    private = Ed25519PrivateKey.from_private_bytes(seed)
    actual_public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    assert actual_public == expected_public
    assert private.sign(b"") == expected_signature

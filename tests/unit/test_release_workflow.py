"""Qualify the ReMe Python release workflow's publication guards."""

from pathlib import Path

import pytest
import yaml


WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release-python.yml"


def _publication_allowed(
    event_name: str,
    ref: str,
    target_commitish: str,
    publishing_enabled: bool,
    event_sha_is_on_main: bool,
) -> bool:
    """Mirror the workflow's opt-in, event/ref, and event-SHA ancestry policy."""
    return publishing_enabled and (
        (event_name == "workflow_dispatch" and ref == "refs/heads/main")
        or (event_name == "release" and target_commitish == "main")
    ) and event_sha_is_on_main


@pytest.mark.parametrize(
    ("event_name", "ref", "target_commitish", "publishing_enabled", "event_sha_is_on_main", "expected"),
    [
        ("workflow_dispatch", "refs/heads/main", "", True, True, True),
        ("workflow_dispatch", "refs/heads/main", "", True, False, False),
        ("workflow_dispatch", "refs/heads/feature", "", True, True, False),
        ("workflow_dispatch", "refs/heads/main", "", False, True, False),
        ("release", "refs/tags/v1.2.3", "main", True, True, True),
        ("release", "refs/tags/v1.2.3", "main", True, False, False),
        ("release", "refs/tags/v1.2.3", "release/1.2", True, True, False),
        ("release", "refs/tags/v1.2.3", "main", False, True, False),
        ("push", "refs/heads/main", "", True, True, False),
    ],
)
def test_release_workflow_publication_truth_table(
    event_name: str,
    ref: str,
    target_commitish: str,
    publishing_enabled: bool,
    event_sha_is_on_main: bool,
    expected: bool,
) -> None:
    """Keep release and dispatch publication paths behind the same trust-boundary checks."""
    assert _publication_allowed(
        event_name, ref, target_commitish, publishing_enabled, event_sha_is_on_main
    ) is expected


def test_release_workflow_is_valid_yaml_and_guards_both_jobs() -> None:
    """Ensure the opt-in and event/ref policy is enforced by both workflow jobs."""
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = document["jobs"]

    for job_name in ("validate-publish-target", "publish-reme"):
        job_if = jobs[job_name]["if"]
        assert "vars.FERMES_ENABLE_PUBLISHING == 'true'" in job_if
        assert "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'" in job_if
        assert "github.event_name == 'release' && github.event.release.target_commitish == 'main'" in job_if

    assert "needs.validate-publish-target.result == 'success'" in jobs["publish-reme"]["if"]


def test_release_workflow_validates_event_sha_without_resolving_a_local_tag() -> None:
    """The checked-out event SHA must be the commit whose ancestry is validated for both events."""
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    validate_job = document["jobs"]["validate-publish-target"]
    checkout_step = next(step for step in validate_job["steps"] if step["name"] == "Check out the event commit")
    verify_step = next(
        step for step in validate_job["steps"] if step["name"] == "Verify the publication commit is on main"
    )
    script = verify_step["run"]

    assert checkout_step["with"]["ref"] == "${{ github.sha }}"
    assert verify_step["env"]["WORKFLOW_SHA"] == "${{ github.sha }}"
    assert "git fetch --no-tags origin refs/heads/main:refs/remotes/origin/main" in script
    assert 'target_commit="$WORKFLOW_SHA"' in script
    assert 'git merge-base --is-ancestor "$target_commit" refs/remotes/origin/main' in script
    assert "git rev-parse" not in script
    assert "refs/tags/" not in script
    assert "RELEASE_TAG" not in verify_step.get("env", {})

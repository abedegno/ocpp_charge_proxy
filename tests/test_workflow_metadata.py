"""Run the actual build-metadata step with realistic helper outputs."""

import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml


@pytest.mark.parametrize(
    "image,architectures,head,event,expected,release",
    [
        (
            json.dumps("ghcr.io/abedegno/{arch}-addon-ocpp-charge-proxy"),
            '["aarch64","amd64"]',
            "feature",
            "pull_request",
            True,
            False,
        ),
        (
            "ghcr.io/abedegno/{arch}-addon-ocpp-charge-proxy",
            '["aarch64","amd64"]',
            "feature",
            "pull_request",
            True,
            False,
        ),
        ("null", '["aarch64","amd64"]', "feature", "pull_request", False, False),
        ('"null"', '["aarch64","amd64"]', "feature", "pull_request", False, False),
        (
            json.dumps("ghcr.io/abedegno/{arch}-addon-ocpp-charge-proxy"),
            '["amd64"]',
            "feature",
            "pull_request",
            False,
            False,
        ),
        (
            json.dumps("ghcr.io/abedegno/{arch}-addon-ocpp-charge-proxy"),
            '["aarch64","amd64"]',
            "",
            "push",
            True,
            True,
        ),
    ],
)
def test_builder_metadata_is_normalized(
    tmp_path, image, architectures, head, event, expected, release
):
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/builder.yaml").read_text())
    script = next(
        step["run"]
        for step in workflow["jobs"]["build"]["steps"]
        if step.get("id") == "check"
    )
    output = tmp_path / "output"
    env_file = tmp_path / "env"
    env = dict(
        os.environ,
        INFO_IMAGE=image,
        INFO_ARCHITECTURES=architectures,
        TARGET_ARCH="aarch64",
        HEAD_REF=head,
        EVENT_NAME=event,
        GITHUB_OUTPUT=str(output),
        GITHUB_ENV=str(env_file),
    )
    subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    fields = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert fields["build_arch"] == str(expected).lower()
    if expected:
        assert fields["image"] == "{arch}-addon-ocpp-charge-proxy"
    assert env_file.exists() == release
    if release:
        assert env_file.read_text() == "BUILD_ARGS=\n"

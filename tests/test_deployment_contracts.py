"""Contracts the deployment lifecycle depends on, checked without running it.

These are the assumptions that broke before: a moving image tag, a base image
that could change underneath a build, and a deploy path that pulled in host
virtualenvs the image never reads.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE = _ROOT / "docker" / "docker-compose.stack.yml"
_DOCKER_DIR = _ROOT / "docker"
_MOVING_TAGS = ("latest", "main", "master", "edge", "stable")
_FROM = re.compile(r"^\s*FROM\s+(\S+)", re.MULTILINE)
_ARG = re.compile(r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\S+)", re.MULTILINE)


def _compose_text() -> str:
    return _COMPOSE.read_text(encoding="utf-8")


def test_compose_never_resolves_a_moving_platform_tag() -> None:
    text = _compose_text()

    references = re.findall(r"vynmatrix/platform:\$\{VM_DEPLOY_IMAGE_TAG([^}]*)\}", text)
    assert references, "Compose must name the platform image through VM_DEPLOY_IMAGE_TAG"
    for suffix in references:
        # ``:?`` fails closed; ``:-latest`` would silently start yesterday's build.
        assert suffix.startswith(":?"), suffix
    assert ":-latest}" not in text


def test_every_first_party_image_reference_carries_an_explicit_tag() -> None:
    document = yaml.safe_load(_compose_text().replace("${", "$DOLLAR{"))
    images = [
        service["image"]
        for service in document["services"].values()
        if isinstance(service, dict) and "image" in service
    ]

    for image in images:
        assert ":" in image, image
        if image.startswith("vynmatrix/"):
            assert "$DOLLAR{VM_DEPLOY_IMAGE_TAG" in image, image
        else:
            # Third-party images are pinned to a concrete released tag.
            assert image.rsplit(":", 1)[1] not in _MOVING_TAGS, image


def test_the_platform_image_resolves_its_base_through_a_build_argument() -> None:
    text = (_DOCKER_DIR / "platform_runtime.Dockerfile").read_text(encoding="utf-8")

    bases = _FROM.findall(text)
    assert bases, "the Dockerfile must declare a base"
    for base in bases:
        assert base.startswith("${VM_SVC_BASE_REF"), base
    # ``vmdev deploy`` overrides it with the generation it just built; the
    # default only keeps a bare ``docker build`` working.
    args = dict(_ARG.findall(text))
    assert args["VM_SVC_BASE_REF"] == "vynmatrix/svc-base:latest"


def test_the_image_is_stamped_with_the_commit_it_was_built_from() -> None:
    text = (_DOCKER_DIR / "platform_runtime.Dockerfile").read_text(encoding="utf-8")

    for argument in ("VM_SOURCE_COMMIT", "VM_SOURCE_DIRTY", "VM_BUILD_TIME"):
        assert f"ARG {argument}=" in text, argument
        assert f"${{{argument}}}" in text, argument
    assert "/app/BUILD_INFO.json" in text
    # ``.dockerignore`` must keep excluding .git, which is why they are arguments.
    assert ".git/" in (_ROOT / ".dockerignore").read_text(encoding="utf-8")


def test_the_builder_publishes_the_stamp_as_labels() -> None:
    from dev_cli.core.deployment import artefact

    source = (_ROOT / "tools/dev_cli/dev_cli/core/docker_builder.py").read_text(encoding="utf-8")

    for label in (
        artefact.SOURCE_COMMIT_LABEL,
        artefact.SOURCE_DIRTY_LABEL,
        artefact.BUILD_TIME_LABEL,
        artefact.BASE_IMAGE_LABEL,
    ):
        assert label in source, label


def test_the_deploy_path_never_builds_host_virtualenvs() -> None:
    """803 MB of venvs the image never reads are a contributor step, not a deploy step."""
    source = (_ROOT / "tools/dev_cli/dev_cli/core/deployment/pipeline.py").read_text(
        encoding="utf-8"
    )

    for forbidden in ("create_all_venvs", "create_venv_for", "build venvs", "venv_manager"):
        assert forbidden not in source, forbidden


@pytest.mark.parametrize(
    "document",
    ["SETUP.md", "docs/DEPLOYMENT.md"],
)
def test_the_install_documents_point_at_the_single_command(document: str) -> None:
    text = (_ROOT / document).read_text(encoding="utf-8")

    assert "vmdev deploy" in text
    assert "vmdev doctor" in text


def test_the_template_and_the_generator_agree_on_the_secret_inventory() -> None:
    from dev_cli.core.deployment import configuration, environment

    declared = environment.declared_keys(_ROOT / ".env.example")

    assert set(configuration.SECRET_KEYS) <= set(declared)
    assert configuration.PAPER_EQUITY_KEY in declared
    # The template must not ship a resolvable default for the image tag.
    assert "VM_DEPLOY_IMAGE_TAG=" in (_ROOT / ".env.example").read_text(encoding="utf-8")
    assert environment.load_env_file(_ROOT / ".env.example")["VM_DEPLOY_IMAGE_TAG"] == ""

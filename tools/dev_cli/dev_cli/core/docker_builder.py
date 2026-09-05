"""Docker image builder."""

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Literal

import yaml
from rich.console import Console

console = Console()

CacheBackend = Literal["gha"]
_MANAGED_BY_LABEL = "com.vynmatrix.managed-by"
_MANAGED_BY_VALUE = "vmdev-v1"
_SOURCE_REPOSITORY_LABEL = "com.vynmatrix.source-repository"
_SOURCE_REPOSITORY_VALUE = "vynmatrix"
_IMAGE_REPOSITORY_LABEL = "com.vynmatrix.image-repository"
_SVC_BASE_REPOSITORY = "vynmatrix/svc-base"
_FULL_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")


class DockerBuilder:
    """Builds Docker images."""

    def __init__(self, config: dict[str, Any], root_dir: Path | None = None):
        self.config = config
        self.root_dir = root_dir if root_dir else Path.cwd()
        self._wheelhouse_validated = False

    def _build_command(
        self,
        *,
        dockerfile: Path,
        image_name: str,
        cache_backend: CacheBackend | None,
        cache_scope: str,
        build_contexts: dict[str, str] | None = None,
    ) -> list[str]:
        repository = self._repository_from_ref(image_name)
        label_args = [
            "--label",
            f"{_MANAGED_BY_LABEL}={_MANAGED_BY_VALUE}",
            "--label",
            f"{_SOURCE_REPOSITORY_LABEL}={_SOURCE_REPOSITORY_VALUE}",
            "--label",
            f"{_IMAGE_REPOSITORY_LABEL}={repository}",
        ]
        context_args = [
            argument
            for name, value in sorted((build_contexts or {}).items())
            for argument in ("--build-context", f"{name}={value}")
        ]
        if cache_backend is None:
            return [
                "docker",
                "build",
                *label_args,
                *context_args,
                "-f",
                str(dockerfile),
                "-t",
                image_name,
                ".",
            ]
        if cache_backend != "gha":
            msg = f"Unsupported Docker cache backend: {cache_backend}"
            raise ValueError(msg)
        return [
            "docker",
            "buildx",
            "build",
            "--load",
            "--cache-from",
            f"type=gha,scope={cache_scope}",
            "--cache-to",
            f"type=gha,mode=max,scope={cache_scope}",
            *label_args,
            *context_args,
            "-f",
            str(dockerfile),
            "-t",
            image_name,
            ".",
        ]

    @staticmethod
    def _repository_from_ref(image_ref: str) -> str:
        """Return the repository portion of a tagged image reference."""
        without_digest = image_ref.split("@", maxsplit=1)[0]
        slash_index = without_digest.rfind("/")
        colon_index = without_digest.rfind(":")
        if colon_index > slash_index:
            return without_digest[:colon_index]
        return without_digest

    @staticmethod
    def _warn_cleanup(message: str) -> None:
        console.print(f"[yellow]Skipping Docker image cleanup: {message}[/yellow]")

    @classmethod
    def _inspect_image(cls, image_ref: str) -> dict[str, Any] | None:
        """Inspect an image locally; cleanup callers treat any failure as unsafe."""
        try:
            inspected = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{json .}}", image_ref],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            cls._warn_cleanup(f"cannot inspect {image_ref}: {exc}")
            return None
        if inspected.returncode != 0:
            detail = inspected.stderr.strip() or "Docker image inspect failed"
            cls._warn_cleanup(f"cannot inspect {image_ref}: {detail}")
            return None
        try:
            payload = json.loads(inspected.stdout)
        except (json.JSONDecodeError, TypeError):
            cls._warn_cleanup(f"Docker returned invalid inspection data for {image_ref}")
            return None
        if not isinstance(payload, dict):
            cls._warn_cleanup(f"Docker returned non-object inspection data for {image_ref}")
            return None
        return payload

    @classmethod
    def _validate_owned_image(
        cls,
        payload: dict[str, Any],
        *,
        expected_id: str | None,
        expected_repository: str,
    ) -> str | None:
        """Revalidate immutable identity and all ownership labels."""
        image_id = payload.get("Id")
        if not isinstance(image_id, str) or _FULL_IMAGE_ID.fullmatch(image_id) is None:
            cls._warn_cleanup("an inspected image has a non-canonical ID")
            return None
        if expected_id is not None and image_id != expected_id:
            cls._warn_cleanup(f"image identity changed while inspecting {expected_id}")
            return None
        config = payload.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if not isinstance(labels, dict):
            cls._warn_cleanup(f"{image_id} has no verifiable ownership labels")
            return None
        expected_labels = {
            _MANAGED_BY_LABEL: _MANAGED_BY_VALUE,
            _SOURCE_REPOSITORY_LABEL: _SOURCE_REPOSITORY_VALUE,
            _IMAGE_REPOSITORY_LABEL: expected_repository,
        }
        if any(labels.get(key) != value for key, value in expected_labels.items()):
            cls._warn_cleanup(f"{image_id} does not have the expected ownership labels")
            return None
        return image_id

    @classmethod
    def _validate_current_refs(
        cls,
        current_refs: dict[str, str],
    ) -> set[str] | None:
        """Resolve every just-built fleet reference before considering deletion."""
        protected_ids: set[str] = set()
        for image_ref, repository in sorted(current_refs.items()):
            payload = cls._inspect_image(image_ref)
            if payload is None:
                return None
            image_id = cls._validate_owned_image(
                payload,
                expected_id=None,
                expected_repository=repository,
            )
            repo_tags = payload.get("RepoTags")
            if image_id is None or not isinstance(repo_tags, list) or image_ref not in repo_tags:
                cls._warn_cleanup(f"{image_ref} is not a verified current image reference")
                return None
            protected_ids.add(image_id)
        return protected_ids

    @classmethod
    def _candidate_is_dangling(
        cls,
        payload: dict[str, Any],
        *,
        image_id: str,
        allowed_repositories: frozenset[str],
    ) -> bool:
        """Require an owned, allowlisted image with no tag or digest reference."""
        config = payload.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        repository = labels.get(_IMAGE_REPOSITORY_LABEL) if isinstance(labels, dict) else None
        if not isinstance(repository, str) or repository not in allowed_repositories:
            cls._warn_cleanup(f"{image_id} is outside the configured repository allowlist")
            return False
        if (
            cls._validate_owned_image(
                payload,
                expected_id=image_id,
                expected_repository=repository,
            )
            is None
        ):
            return False
        repo_tags = payload.get("RepoTags")
        repo_digests = payload.get("RepoDigests")
        if repo_tags not in (None, []) or repo_digests not in (None, []):
            cls._warn_cleanup(f"{image_id} still has a tag or digest reference")
            return False
        return True

    @classmethod
    def _remove_dangling_candidate(
        cls,
        image_id: str,
        *,
        allowed_repositories: frozenset[str],
        protected_ids: set[str],
    ) -> None:
        """Remove exactly one twice-inspected image when no container uses it."""
        if image_id in protected_ids:
            cls._warn_cleanup(f"{image_id} is a current fleet image")
            return
        payload = cls._inspect_image(image_id)
        if payload is None or not cls._candidate_is_dangling(
            payload,
            image_id=image_id,
            allowed_repositories=allowed_repositories,
        ):
            return
        has_container_reference = cls._has_container_reference(image_id)
        if has_container_reference is None or has_container_reference:
            return

        # Reinspect immediately before removal. Docker's non-forced removal is
        # the final race-safe guard if a tag or container appears afterwards.
        rechecked = cls._inspect_image(image_id)
        if rechecked is None or not cls._candidate_is_dangling(
            rechecked,
            image_id=image_id,
            allowed_repositories=allowed_repositories,
        ):
            return
        cls._remove_exact_image(image_id)

    @classmethod
    def _has_container_reference(cls, image_id: str) -> bool | None:
        """Return whether any stopped or running container protects an image."""
        try:
            containers = subprocess.run(
                [
                    "docker",
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    "--filter",
                    f"ancestor={image_id}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            cls._warn_cleanup(f"cannot verify container references for {image_id}: {exc}")
            return None
        if containers.returncode != 0:
            detail = containers.stderr.strip() or "Docker container listing failed"
            cls._warn_cleanup(f"cannot verify container references for {image_id}: {detail}")
            return None
        if containers.stdout.strip():
            cls._warn_cleanup(f"a container still references {image_id}")
            return True
        return False

    @classmethod
    def _remove_exact_image(cls, image_id: str) -> None:
        """Remove one already-revalidated ID without pruning parent layers."""
        try:
            removed = subprocess.run(
                ["docker", "image", "rm", "--no-prune", image_id],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            cls._warn_cleanup(f"cannot remove {image_id}: {exc}")
            return
        if removed.returncode != 0:
            detail = removed.stderr.strip() or "Docker image removal failed"
            cls._warn_cleanup(f"cannot remove {image_id}: {detail}")
            return
        console.print(f"[green]✓ Removed superseded image {image_id}[/green]")

    @classmethod
    def _cleanup_owned_dangling_images(
        cls,
        *,
        allowed_repositories: frozenset[str],
        current_refs: dict[str, str],
    ) -> None:
        """Clean only owned, dangling generations after a full fleet build."""
        protected_ids = cls._validate_current_refs(current_refs)
        if protected_ids is None:
            return
        try:
            listed = subprocess.run(
                [
                    "docker",
                    "image",
                    "ls",
                    "--filter",
                    "dangling=true",
                    "--filter",
                    f"label={_MANAGED_BY_LABEL}={_MANAGED_BY_VALUE}",
                    "--filter",
                    f"label={_SOURCE_REPOSITORY_LABEL}={_SOURCE_REPOSITORY_VALUE}",
                    "--quiet",
                    "--no-trunc",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            cls._warn_cleanup(f"cannot discover owned dangling images: {exc}")
            return
        if listed.returncode != 0:
            detail = listed.stderr.strip() or "Docker image listing failed"
            cls._warn_cleanup(f"cannot discover owned dangling images: {detail}")
            return
        candidates: set[str] = set()
        for candidate in listed.stdout.splitlines():
            image_id = candidate.strip()
            if _FULL_IMAGE_ID.fullmatch(image_id) is None:
                cls._warn_cleanup(f"Docker returned a non-canonical image ID: {image_id!r}")
                continue
            candidates.add(image_id)
        for image_id in sorted(candidates):
            cls._remove_dangling_candidate(
                image_id,
                allowed_repositories=allowed_repositories,
                protected_ids=protected_ids,
            )

    def validate_wheelhouse(self) -> None:
        """Fail before Docker starts when a configured wheel is missing or stale.

        Dockerfiles deliberately consume prebuilt local wheels. Exact payload
        comparison catches deleted/changed Python modules, while the metadata
        timestamp check catches packaging/dependency edits that do not alter a
        module. This closes the local ``docker compose --build`` stale-wheel
        failure mode without rebuilding artifacts implicitly.
        """
        if self._wheelhouse_validated:
            return

        # Local import avoids a module-import cycle: Builder owns the canonical
        # wheel payload verifier and constructs DockerBuilder in its __init__.
        from dev_cli.core.builder import Builder  # noqa: PLC0415

        wheels_dir = self.root_dir / str(
            self.config.get("global", {}).get("wheels_dir", "./build/wheels")
        )
        components = [
            *self.config.get("libs", {}).get("components", []),
            *self.config.get("strategies", {}).get("groups", []),
        ]
        for component in components:
            source = self.root_dir / str(component["path"])
            distribution = str(component.get("wheel_distribution") or component["name"]).replace(
                "-", "_"
            )
            wheels = sorted(wheels_dir.glob(f"{distribution}-*.whl"))
            if len(wheels) != 1:
                msg = (
                    f"Expected exactly one current wheel for {distribution}, "
                    f"found {len(wheels)} in {wheels_dir}. Rebuild wheels before Docker."
                )
                raise RuntimeError(msg)
            wheel = wheels[0]
            Builder._validate_wheel_payload(
                wheel,
                source,
                verify_strategy_payload=bool(component.get("verify_strategy_payload", False)),
                verify_exact_python_payload=True,
            )
            metadata_inputs = [
                path
                for name in ("setup.py", "pyproject.toml", "requirements.txt")
                if (path := source / name).is_file()
            ]
            if metadata_inputs and wheel.stat().st_mtime_ns < max(
                path.stat().st_mtime_ns for path in metadata_inputs
            ):
                msg = (
                    f"Wheel {wheel.name} predates packaging metadata in {source}. "
                    "Rebuild wheels before Docker."
                )
                raise RuntimeError(msg)

        self._wheelhouse_validated = True

    def build_svc_base(self, *, cache_backend: CacheBackend | None = None) -> None:
        """Build ``vynmatrix/svc-base:latest`` before all service images."""
        dockerfile = self.root_dir / "docker" / "svc-base.Dockerfile"
        if not dockerfile.is_file():
            msg = f"svc-base Dockerfile not found: {dockerfile}"
            raise FileNotFoundError(msg)
        image_name = "vynmatrix/svc-base:latest"
        console.print(f"[cyan]Building shared service base image: {image_name}[/cyan]")
        try:
            subprocess.run(
                self._build_command(
                    dockerfile=dockerfile,
                    image_name=image_name,
                    cache_backend=cache_backend,
                    cache_scope="vynmatrix-svc-base",
                ),
                cwd=self.root_dir,
                check=True,
            )
            console.print(f"[green]✓ Built {image_name}[/green]")
        except subprocess.CalledProcessError:
            console.print(f"[red]✗ Failed to build {image_name}[/red]")
            raise

    def _configured_dockerfile(self, service: dict[str, Any], *, index: int) -> Path | None:
        """Validate an explicit composed-image Dockerfile before contacting Docker."""
        if "dockerfile" not in service:
            return None
        declared = service["dockerfile"]
        if (
            not isinstance(declared, str)
            or not declared.strip()
            or Path(declared).is_absolute()
            or ".." in Path(declared).parts
        ):
            msg = f"services[{index}].dockerfile must be a repository-relative Dockerfile"
            raise ValueError(msg)
        dockerfile = (self.root_dir / declared).resolve()
        if not dockerfile.is_relative_to((self.root_dir / "docker").resolve()):
            msg = f"services[{index}].dockerfile must stay inside the repository docker directory"
            raise ValueError(msg)
        if not dockerfile.is_file():
            msg = f"Declared Dockerfile does not exist: {declared}"
            raise FileNotFoundError(msg)
        return dockerfile

    def build_from_containers_config(
        self,
        tag: str = "latest",
        config_path: Path | None = None,
        *,
        cache_backend: CacheBackend | None = None,
    ) -> None:
        """Build service images defined in config/containers.yaml."""
        if config_path:
            raw_path = Path(config_path)
            path = raw_path if raw_path.is_absolute() else self.root_dir / raw_path
        else:
            path = self.root_dir / "config" / "containers.yaml"
        if not path.is_file():
            msg = f"Containers config not found: {path}"
            raise FileNotFoundError(msg)

        with path.open() as f:
            containers_config = yaml.safe_load(f) or {}
        if not isinstance(containers_config, dict):
            msg = f"Containers config must decode to an object: {path}"
            raise TypeError(msg)

        # A composed image need not correspond to one apps/ directory. The
        # inventory can declare its Dockerfile explicitly while retaining its
        # logical app identity for build/cache labels and validated wheel inputs.
        services = containers_config.get("services")
        if not isinstance(services, list) or not services:
            msg = f"Containers config must define a non-empty services list: {path}"
            raise ValueError(msg)
        image_inventory: list[tuple[str, str, Path | None]] = []
        for index, service in enumerate(services):
            if not isinstance(service, dict):
                msg = f"services[{index}] must be an object"
                raise TypeError(msg)
            app_name = service.get("app")
            if not isinstance(app_name, str) or not app_name.strip():
                msg = f"services[{index}].app must be a non-empty string"
                raise ValueError(msg)
            normalized_app_name = app_name.strip()
            configured_repository = service.get("image")
            if not isinstance(configured_repository, str) or not configured_repository.strip():
                msg = f"services[{index}].image must be a non-empty repository reference"
                raise ValueError(msg)
            image_repository = configured_repository.strip()
            if (
                image_repository != configured_repository
                or "@" in image_repository
                or self._repository_from_ref(image_repository) != image_repository
            ):
                msg = (
                    f"services[{index}].image must be a repository-only reference "
                    "without a tag or digest"
                )
                raise ValueError(msg)
            dockerfile = self._configured_dockerfile(service, index=index)
            image_inventory.append((normalized_app_name, image_repository, dockerfile))
        app_names = [app_name for app_name, _, _ in image_inventory]
        if len(app_names) != len(set(app_names)):
            msg = f"Containers config contains duplicate service apps: {app_names}"
            raise ValueError(msg)
        image_repositories = [repository for _, repository, _ in image_inventory]
        if len(image_repositories) != len(set(image_repositories)):
            msg = (
                "Containers config contains duplicate service image repositories: "
                f"{image_repositories}"
            )
            raise ValueError(msg)

        console.print("\n[bold cyan]Building Pipeline Service Images:[/bold cyan]")
        self.validate_wheelhouse()
        self.build_svc_base(cache_backend=cache_backend)
        for app_name, image_repository, dockerfile in image_inventory:
            self.build_app(
                app_name,
                tag,
                image_repository=image_repository,
                cache_backend=cache_backend,
                validate_wheelhouse=False,
                **({"dockerfile": dockerfile} if dockerfile is not None else {}),
            )
        current_refs = {
            f"{_SVC_BASE_REPOSITORY}:latest": _SVC_BASE_REPOSITORY,
            **{
                f"{image_repository}:{tag}": image_repository
                for _, image_repository, _ in image_inventory
            },
        }
        self._cleanup_owned_dangling_images(
            allowed_repositories=frozenset(current_refs.values()),
            current_refs=current_refs,
        )

    def build_app(
        self,
        app_name: str,
        tag: str = "latest",
        *,
        image_repository: str | None = None,
        cache_backend: CacheBackend | None = None,
        validate_wheelhouse: bool = True,
        dockerfile: Path | None = None,
    ) -> None:
        """Build Docker image for application."""
        if validate_wheelhouse:
            self.validate_wheelhouse()
        dockerfile = dockerfile or self.root_dir / "docker" / f"{app_name}.Dockerfile"
        if not dockerfile.is_file():
            msg = f"Dockerfile not found for app {app_name}: {dockerfile}"
            raise FileNotFoundError(msg)

        repository = image_repository or f"vynmatrix/{app_name.replace('_', '-')}"
        image_name = f"{repository}:{tag}"

        console.print(f"[cyan]Building Docker image: {image_name}[/cyan]")

        try:
            subprocess.run(
                self._build_command(
                    dockerfile=dockerfile,
                    image_name=image_name,
                    cache_backend=cache_backend,
                    cache_scope=f"vynmatrix-{app_name.replace('_', '-')}",
                    # Container-backed Buildx workers cannot rely on resolving
                    # the Docker daemon's local image store implicitly. Passing
                    # the just-built base as a named image context avoids a
                    # registry pull during pull-request builds.
                    build_contexts={
                        "vynmatrix/svc-base:latest": ("docker-image://vynmatrix/svc-base:latest")
                    },
                ),
                cwd=self.root_dir,
                check=True,
            )
            console.print(f"[green]✓ Built Docker image: {image_name}[/green]")
        except subprocess.CalledProcessError:
            console.print(f"[red]✗ Failed to build {image_name}[/red]")
            raise

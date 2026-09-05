"""Core build engine."""

import ast
import hashlib
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, cast

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from dev_cli.core.docker_builder import DockerBuilder
from dev_cli.core.venv_manager import VenvManager
from dev_cli.utils.helpers import get_project_root as _find_repo_root

console = Console()

_VALIDATION_ONLY_WHEEL_PREFIXES = (
    "lib_application/services/backtest_",
    "lib_application/services/strategy_validation_",
    "lib_infrastructure/market_data/coinbase_data_parity.py",
    "lib_infrastructure/market_data/coinbase_execution_costs.py",
    "lib_strategy/backtest/",
)


class Builder:
    """Main builder class."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.root_dir: Path = Path(_find_repo_root())
        self.build_dir: Path = self.root_dir / str(config["global"]["build_dir"])
        self.wheels_dir: Path = self.build_dir / "wheels"
        self.venvs_dir: Path = self.build_dir / "venvs"
        self.app_constraints_file: Path = self.root_dir / "docker" / "constraints.txt"

        # Create directories
        self.wheels_dir.mkdir(parents=True, exist_ok=True)
        self.venvs_dir.mkdir(parents=True, exist_ok=True)

        # Get Python version from config
        python_version = config["global"].get("python_version", "3.11.13")

        self.venv_manager = VenvManager(self.venvs_dir, python_version)
        self.docker_builder = DockerBuilder(config, self.root_dir)

    def build_lib(self, lib_name: str) -> None:
        """Build a single library wheel."""
        # Find library config
        lib_config = self._find_component_config("libs", lib_name)
        if not lib_config:
            console.print(f"[red]Library {lib_name} not found[/red]")
            return

        self._build_wheel(lib_config)

    def build_all_libs(self) -> None:
        """Build all library wheels."""
        libs = self.config["libs"]["components"]
        self._prune_unconfigured_wheels(
            prefix="lib_",
            configured_distributions=[str(lib["name"]) for lib in libs],
        )

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
        ) as progress:
            task = progress.add_task("[cyan]Building libraries...", total=len(libs))

            for lib in libs:
                progress.update(task, description=f"[cyan]Building {lib['name']}...")
                self._build_wheel(lib)
                progress.advance(task)

    def build_strategy_group(self, group_name: str) -> None:
        """Build a single strategy group wheel."""
        group_config = self._find_component_config("strategies", group_name)
        if not group_config:
            console.print(f"[red]Strategy group {group_name} not found[/red]")
            return

        self._build_wheel(group_config)

    def build_all_strategies(self) -> None:
        """Build all strategy group wheels."""
        groups = self.config["strategies"]["groups"]
        self._prune_unconfigured_wheels(
            prefix="vynmatrix_",
            configured_distributions=[str(group["wheel_distribution"]) for group in groups],
        )

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
        ) as progress:
            task = progress.add_task("[cyan]Building strategy groups...", total=len(groups))

            for group in groups:
                progress.update(task, description=f"[cyan]Building {group['name']}...")
                self._build_wheel(group)
                progress.advance(task)

    def create_all_venvs(self) -> None:
        """Create all virtual environments."""
        # Strategy groups
        for group in self.config["strategies"]["groups"]:
            self.venv_manager.create_venv(
                name=f"strategy-{group['name']}",
                requirements=self._strategy_venv_requirements(group),
                import_name=group.get("verify_import"),
                constraints_file=self.app_constraints_file,
            )

        # The research runner needs application/infrastructure libraries and
        # the packaged vmdev command in addition to the strategy wheel. Keep it
        # separate from the deliberately small production strategy venv.
        self.create_strategy_validation_venv()

        # Applications
        for app in self.config["apps"]["components"]:
            self._create_app_venv(app)

    def create_venv_for_strategy(self, group_name: str) -> None:
        """Create venv for specific strategy group."""
        group_config = self._find_component_config("strategies", group_name)
        if not group_config:
            console.print(f"[red]Strategy group {group_name} not found[/red]")
            return

        self.venv_manager.create_venv(
            name=f"strategy-{group_name}",
            requirements=self._strategy_venv_requirements(group_config),
            import_name=group_config.get("verify_import"),
            constraints_file=self.app_constraints_file,
        )

    def create_venv_for_app(self, app_name: str) -> None:
        """Create venv for specific application."""
        app_config = self._find_component_config("apps", app_name)
        if not app_config:
            console.print(f"[red]Application {app_name} not found[/red]")
            return

        self._create_app_venv(app_config)

    def create_strategy_validation_venv(self) -> None:
        """Create the exact installed-artifact environment used by campaigns."""

        validation = self._strategy_validation_config()
        wheels = self.strategy_validation_wheel_paths()
        tool_path = (self.root_dir / str(validation["tool_path"])).resolve()
        self._require_within_repository(tool_path, field="validation tool path")
        group_name = str(validation["strategy_group"])
        group = self._find_component_config("strategies", group_name)
        if group is None:
            message = f"Unknown validation strategy group: {group_name!r}"
            raise RuntimeError(message)
        strategy_import = str(group.get("verify_import") or "").strip()
        if not strategy_import:
            message = f"Validation strategy group {group_name!r} has no verify_import"
            raise RuntimeError(message)
        strategy_distribution = str(group["wheel_distribution"]).replace("-", "_")
        raw_external_requirements = validation.get("external_requirements", [])
        if not isinstance(raw_external_requirements, list) or any(
            not isinstance(value, str) or not value.strip() or value != value.strip()
            for value in raw_external_requirements
        ):
            message = "strategy_validation.external_requirements must be canonical strings"
            raise TypeError(message)
        external_requirements = list(raw_external_requirements)
        if len(external_requirements) != len(set(external_requirements)):
            message = "strategy_validation.external_requirements must be unique"
            raise ValueError(message)
        raw_applications = validation.get("applications", [])
        if not isinstance(raw_applications, list) or any(
            not isinstance(value, str) or not value.strip() or value != value.strip()
            for value in raw_applications
        ):
            message = "strategy_validation.applications must be canonical strings"
            raise TypeError(message)
        application_names = list(raw_applications)
        if len(application_names) != len(set(application_names)):
            message = "strategy_validation.applications must be unique"
            raise ValueError(message)
        application_paths: list[Path] = []
        application_imports: list[str] = []
        for application_name in application_names:
            application = self._find_component_config("apps", application_name)
            if application is None:
                message = f"Unknown validation application: {application_name!r}"
                raise RuntimeError(message)
            application_path = (self.root_dir / str(application["path"])).resolve()
            self._require_within_repository(
                application_path,
                field=f"validation application {application_name}",
            )
            VenvManager.clean_package_build_artifacts(application_path)
            application_paths.append(application_path)
            application_imports.append(application_name.replace("-", "_"))
        verify_imports = [
            "dev_cli",
            *(name for name in wheels if name != strategy_distribution),
            "lib_data.sessions",
            strategy_import,
            *application_imports,
        ]
        self.venv_manager.create_venv(
            name=self.strategy_validation_venv_path().name,
            requirements=[
                *(str(path) for path in wheels.values()),
                *external_requirements,
                *(str(path) for path in application_paths),
            ],
            package_path=tool_path,
            import_names=verify_imports,
            constraints_file=self.app_constraints_file,
        )

    def strategy_validation_venv_path(self) -> Path:
        """Return the configured local validation-environment path."""

        validation = self._strategy_validation_config()
        name = str(validation.get("venv_name", "")).strip()
        if not name or Path(name).name != name:
            message = "strategy_validation.venv_name must be one local directory name"
            raise ValueError(message)
        path = (self.venvs_dir / name).resolve()
        self._require_within_repository(path, field="validation venv path")
        return path

    def strategy_validation_wheel_paths(self) -> dict[str, Path]:
        """Resolve configured validation wheels in their declared install order."""

        validation = self._strategy_validation_config()
        configured_libraries = validation.get("libraries")
        if not isinstance(configured_libraries, list) or not configured_libraries:
            message = "strategy_validation.libraries must be a non-empty list"
            raise TypeError(message)
        library_names = tuple(str(value).strip() for value in configured_libraries)
        if any(not name for name in library_names) or len(set(library_names)) != len(library_names):
            message = "strategy_validation.libraries must contain unique non-empty names"
            raise ValueError(message)
        group_name = str(validation.get("strategy_group", "")).strip()
        group = self._find_component_config("strategies", group_name)
        if group is None:
            message = f"Unknown validation strategy group: {group_name!r}"
            raise RuntimeError(message)
        strategy_distribution = str(group.get("wheel_distribution") or "").strip().replace("-", "_")
        if not strategy_distribution or strategy_distribution in library_names:
            message = "validation strategy wheel must be distinct from configured libraries"
            raise RuntimeError(message)

        records: list[tuple[str, dict[str, Any]]] = []
        for name in library_names:
            component = self._find_component_config("libs", name)
            if component is None:
                message = f"Unknown validation library: {name!r}"
                raise RuntimeError(message)
            records.append((name, component))
        records.append((strategy_distribution, group))

        wheels: dict[str, Path] = {}
        for distribution, component in records:
            wheel = self._exact_wheel_path(distribution)
            component_path = (self.root_dir / str(component["path"])).resolve()
            self._validate_wheel_payload(
                wheel,
                component_path,
                verify_strategy_payload=bool(component.get("verify_strategy_payload", False)),
                verify_exact_python_payload=True,
            )
            wheels[distribution] = wheel
        return wheels

    def _create_app_venv(self, app: dict[str, Any]) -> None:
        """Create an app venv from internal wheels plus the app package itself."""
        app_name = str(app["name"])
        requirements = self._get_dependencies(app)
        for group_name in app.get("strategy_groups", []):
            group = self._find_component_config("strategies", str(group_name))
            if group is None:
                message = f"Unknown strategy group {group_name!r} required by {app_name}"
                raise RuntimeError(message)
            requirements.append(str(self._strategy_wheel_path(group)))
        self.venv_manager.create_venv(
            name=f"app-{app_name}",
            requirements=requirements,
            package_path=self.root_dir / str(app["path"]),
            import_name=app_name.replace("-", "_"),
            constraints_file=self.app_constraints_file,
        )

    def build_docker_image(self, app_name: str, tag: str = "latest") -> None:
        """Build Docker image for specific app."""
        self.docker_builder.build_app(app_name, tag)

    def build_for_team(self, team_name: str) -> None:
        """Build all components owned by a team."""
        team_components = self.config.get("teams", {}).get(team_name)
        if not team_components:
            console.print(f"[red]Team {team_name} not found[/red]")
            return

        console.print(
            f"[cyan]Building {len(team_components)} components for team {team_name}[/cyan]"
        )

        for component_name in team_components:
            # Try to find component in each category
            # Libraries
            if self._find_component_config("libs", component_name):
                console.print(f"[cyan]Building library: {component_name}[/cyan]")
                self.build_lib(component_name)
            # Strategy groups (handle naming variations)
            elif self._find_component_config("strategies", component_name):
                console.print(f"[cyan]Building strategy: {component_name}[/cyan]")
                self.build_strategy_group(component_name)
            # Try without suffix for strategies
            elif component_name.endswith("_strategies"):
                base_name = component_name.replace("_strategies", "")
                if self._find_component_config("strategies", base_name):
                    console.print(f"[cyan]Building strategy: {base_name}[/cyan]")
                    self.build_strategy_group(base_name)
                else:
                    console.print(f"[yellow]Component {component_name} not found[/yellow]")
            # Apps
            elif self._find_component_config("apps", component_name):
                console.print(f"[cyan]Building app venv: {component_name}[/cyan]")
                self.create_venv_for_app(component_name)
            else:
                console.print(
                    f"[yellow]Component {component_name} not found in any category[/yellow]"
                )

    def _build_wheel(self, component: dict[str, Any]) -> None:
        """Build a wheel for a component."""
        component_path = self.root_dir / component["path"]

        if not component_path.exists():
            console.print(f"[red]Component path not found: {component_path}[/red]")
            return

        try:
            self.venv_manager.clean_package_build_artifacts(component_path)
            # Build into an empty per-component output directory so a stale
            # global wheel cannot be mistaken for the result of this invocation.
            with tempfile.TemporaryDirectory(
                prefix=f"wheel-{component['name']}-", dir=self.build_dir
            ) as temporary_output:
                output_dir = Path(temporary_output)
                # Reproducible wheels: pin every zip-entry mtime to the
                # component's last commit time so an unchanged component
                # produces byte-identical wheel bytes on every rebuild —
                # otherwise the docker COPY layer's content hash misses on
                # all service images every CI run.
                build_env = dict(os.environ)
                build_env.setdefault(
                    "SOURCE_DATE_EPOCH",
                    self._component_source_epoch(component_path),
                )
                subprocess.run(
                    ["python", "-m", "build", "--wheel", "--outdir", str(output_dir)],
                    cwd=component_path,
                    capture_output=True,
                    text=True,
                    check=True,
                    env=build_env,
                )
                wheels = list(output_dir.glob("*.whl"))
                if len(wheels) != 1:
                    message = f"Expected one wheel for {component['name']}, found {len(wheels)}"
                    raise RuntimeError(message)
                wheel = wheels[0]
                self._validate_wheel_payload(
                    wheel,
                    component_path,
                    verify_strategy_payload=bool(component.get("verify_strategy_payload", False)),
                )

                distribution = wheel.name.split("-", maxsplit=1)[0]
                for prior_wheel in self.wheels_dir.glob(f"{distribution}-*.whl"):
                    prior_wheel.unlink()
                wheel.replace(self.wheels_dir / wheel.name)

            console.print(f"[green]✓ Built wheel for {component['name']}[/green]")

        except subprocess.CalledProcessError as e:
            console.print(f"[red]✗ Failed to build {component['name']}[/red]")
            console.print(f"[red]{e.stderr}[/red]")
            raise

    def _prune_unconfigured_wheels(
        self,
        *,
        prefix: str,
        configured_distributions: list[str],
    ) -> None:
        """Remove generated wheels whose distribution left the build inventory."""
        normalized_prefix = prefix.replace("-", "_").lower()
        configured = {
            distribution.replace("-", "_").lower() for distribution in configured_distributions
        }
        for wheel in self.wheels_dir.glob("*.whl"):
            distribution = wheel.name.split("-", maxsplit=1)[0].lower()
            if distribution.startswith(normalized_prefix) and distribution not in configured:
                wheel.unlink()
                console.print(f"[yellow]Removed unconfigured wheel: {wheel.name}[/yellow]")

    @staticmethod
    def _validate_production_wheel_boundary(
        archive: zipfile.ZipFile,
        members: list[str],
        *,
        wheel_name: str,
    ) -> None:
        """Reject validation modules and development-tool imports in runtime wheels."""

        validation_payloads = sorted(
            name
            for name in members
            if any(name.startswith(prefix) for prefix in _VALIDATION_ONLY_WHEEL_PREFIXES)
        )
        if validation_payloads:
            message = f"Wheel {wheel_name} contains validation-only modules: {validation_payloads}"
            raise RuntimeError(message)

        validation_imports: list[str] = []
        for name in members:
            if not name.endswith(".py") or ".dist-info/" in name:
                continue
            tree = ast.parse(archive.read(name), filename=name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(
                    alias.name == "dev_cli" or alias.name.startswith("dev_cli.")
                    for alias in node.names
                ):
                    validation_imports.append(name)
                    break
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module is not None
                    and (node.module == "dev_cli" or node.module.startswith("dev_cli."))
                ):
                    validation_imports.append(name)
                    break
        if validation_imports:
            message = (
                f"Wheel {wheel_name} imports development-only tooling: {sorted(validation_imports)}"
            )
            raise RuntimeError(message)

    @staticmethod
    def _validate_wheel_payload(
        wheel_path: Path,
        component_path: Path,
        *,
        verify_strategy_payload: bool = False,
        verify_exact_python_payload: bool = False,
    ) -> None:
        """Reject stale modules and prove required strategy files are byte-identical."""

        source_hashes = {
            hashlib.sha256(source.read_bytes()).digest()
            for source in component_path.rglob("*.py")
            if not {"build", "dist", "__pycache__"}.intersection(source.parts)
            and not any(part.endswith(".egg-info") for part in source.parts)
        }
        with zipfile.ZipFile(wheel_path) as archive:
            members = archive.namelist()
            Builder._validate_production_wheel_boundary(
                archive,
                members,
                wheel_name=wheel_path.name,
            )

            cached = sorted(
                name for name in members if name.endswith(".pyc") or "__pycache__/" in name
            )
            if cached:
                message = f"Wheel {wheel_path.name} contains cached bytecode: {cached}"
                raise RuntimeError(message)

            unbacked = sorted(
                name
                for name in members
                if name.endswith(".py")
                and hashlib.sha256(archive.read(name)).digest() not in source_hashes
            )
            if unbacked:
                message = (
                    f"Wheel {wheel_path.name} contains Python modules absent from "
                    f"current source: {unbacked}"
                )
                raise RuntimeError(message)

            if verify_exact_python_payload:
                expected_python = {
                    source.relative_to(component_path).as_posix(): source.read_bytes()
                    for source in component_path.rglob("*.py")
                    if Builder._is_packaged_python_source(source, component_path)
                }
                wheel_python = {
                    name: archive.read(name)
                    for name in members
                    if name.endswith(".py") and ".dist-info/" not in name
                }
                missing = sorted(set(expected_python) - set(wheel_python))
                unexpected = sorted(set(wheel_python) - set(expected_python))
                changed = sorted(
                    name
                    for name in set(expected_python) & set(wheel_python)
                    if expected_python[name] != wheel_python[name]
                )
                if missing or unexpected or changed:
                    message = (
                        f"Wheel {wheel_path.name} is stale relative to current Python source: "
                        f"missing={missing}, unexpected={unexpected}, changed={changed}"
                    )
                    raise RuntimeError(message)

            if verify_strategy_payload:
                required_sources: list[Path] = []
                strategy_directories: list[Path] = []
                for strategy_dir in sorted(component_path.iterdir()):
                    core = strategy_dir / "core.py"
                    config = strategy_dir / "config.json"
                    if not strategy_dir.is_dir() or not core.is_file() or not config.is_file():
                        continue
                    # Underscore-prefixed scaffolds (_template) are excluded
                    # from the wheel by setup.py discovery and must not be
                    # counted as deployable strategy payloads.
                    if strategy_dir.name.startswith(("_", ".")):
                        continue
                    strategy_directories.append(strategy_dir)
                    required_sources.extend((core, config))
                if not required_sources:
                    message = "Strategy wheel verification found no production core/config pairs"
                    raise RuntimeError(message)
                missing_or_changed: list[str] = []
                for source in required_sources:
                    relative = source.relative_to(component_path).as_posix()
                    if relative not in members or archive.read(relative) != source.read_bytes():
                        missing_or_changed.append(relative)
                if missing_or_changed:
                    message = (
                        f"Wheel {wheel_path.name} is missing or changed strategy payloads: "
                        f"{missing_or_changed}"
                    )
                    raise RuntimeError(message)
                research_payloads = sorted(
                    f"{strategy_dir.name}/{filename}"
                    for strategy_dir in strategy_directories
                    for filename in ("README.md", "validation_protocol.json")
                    if f"{strategy_dir.name}/{filename}" in members
                )
                if research_payloads:
                    message = (
                        f"Wheel {wheel_path.name} contains research-only strategy payloads: "
                        f"{research_payloads}"
                    )
                    raise RuntimeError(message)

    def _strategy_venv_requirements(self, group: dict[str, Any]) -> list[str]:
        """Return dependency wheels plus the exact strategy distribution wheel."""

        requirements = self._get_dependencies(group)
        distribution = str(group.get("wheel_distribution") or "").strip()
        if not distribution:
            return requirements
        return [*requirements, str(self._strategy_wheel_path(group))]

    def _strategy_wheel_path(self, group: dict[str, Any]) -> Path:
        """Resolve exactly one freshly built wheel for a configured strategy group."""

        distribution = str(group.get("wheel_distribution") or "").strip().replace("-", "_")
        if not distribution:
            message = f"Strategy group {group.get('name')!r} has no wheel_distribution"
            raise RuntimeError(message)
        return self._exact_wheel_path(distribution)

    def _exact_wheel_path(self, distribution: str) -> Path:
        """Resolve one local wheel without accepting aliases, escapes, or leftovers."""

        normalized = distribution.strip().replace("-", "_")
        if not normalized or normalized != distribution:
            message = f"wheel distribution must use its normalized local name: {distribution!r}"
            raise ValueError(message)
        wheels = sorted(self.wheels_dir.glob(f"{normalized}-*.whl"))
        if len(wheels) != 1:
            message = (
                f"Expected exactly one built wheel for {normalized}, found {len(wheels)}; "
                "rebuild wheels before creating the validation environment"
            )
            raise RuntimeError(message)
        wheel = wheels[0].resolve()
        self._require_within_repository(wheel, field=f"{normalized} wheel")
        if wheel.parent != self.wheels_dir.resolve() or not wheel.is_file():
            message = f"validation wheel must be a regular file in build/wheels: {wheel}"
            raise ValueError(message)
        return wheel

    @staticmethod
    def _is_packaged_python_source(source: Path, component_path: Path) -> bool:
        relative = source.relative_to(component_path)
        if source.parent == component_path:
            return False
        excluded = {"build", "dist", "tests", "__pycache__"}
        if excluded.intersection(relative.parts) or any(
            part.endswith(".egg-info") for part in relative.parts
        ):
            return False
        # Underscore-prefixed strategy directories (e.g. _template) are
        # non-deployable scaffolds: setup.py package discovery skips them
        # (iter_strategy_dirs semantics), so the staleness scan must too.
        return not relative.parts[0].startswith("_")

    def _strategy_validation_config(self) -> dict[str, Any]:
        validation = self.config.get("strategy_validation")
        if not isinstance(validation, dict):
            message = "config/build.yaml must define strategy_validation"
            raise TypeError(message)
        return cast(dict[str, Any], validation)

    def _require_within_repository(self, path: Path, *, field: str) -> None:
        root = self.root_dir.resolve()
        resolved = path.resolve()
        if resolved == root or root not in resolved.parents:
            message = f"{field} escapes the repository: {path}"
            raise ValueError(message)

    def _get_dependencies(self, component: dict[str, Any]) -> list[str]:
        """Get list of dependencies for a component."""
        deps = []
        all_deps = []

        # Check if this is a strategy and add base dependencies
        if component.get("path", "").startswith("strategies/"):
            base_deps = self.config.get("strategies", {}).get("base_dependencies", [])
            all_deps.extend(base_deps)

        # Add component-specific dependencies
        all_deps.extend(component.get("dependencies", []))

        # Stable install order for internal libs to satisfy dependency chains
        priority = {
            "lib_common": 1,
            "lib_data": 2,
            "lib_indicators": 3,
            "lib_strategy": 4,
            "lib_infrastructure": 5,
            "lib_application": 6,
        }
        all_deps = sorted(all_deps, key=lambda d: priority.get(d, 999))

        # Process each dependency
        for dep in all_deps:
            # Try to find the wheel file
            # Internal wheel distribution names use underscores.
            wheel_pattern = f"{dep}-*.whl"
            wheels = list(self.wheels_dir.glob(wheel_pattern))

            if wheels:
                # Use the wheel file
                deps.append(str(wheels[0]))
            else:
                # Check if it's an internal dependency that should have been built
                if dep.startswith(("lib_", "ext_")):
                    console.print(
                        f"[yellow]Warning: Wheel not found for {dep}, trying package name[/yellow]"
                    )
                # Fallback to package name for external dependencies
                deps.append(dep)

        # Add requirements.txt if exists
        req_file = self.root_dir / component["path"] / "requirements.txt"
        if req_file.exists():
            with req_file.open() as f:
                for line in f:
                    line_str = line.strip()
                    if line_str and not line_str.startswith("#"):
                        deps.append(line_str)

        return deps

    def _component_source_epoch(self, component_path: Path) -> str:
        """Last-commit unix time for the component (SOURCE_DATE_EPOCH).

        Falls back to a fixed epoch when git metadata is unavailable (e.g. an
        exported tree) — determinism matters more than the specific value.
        """
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--", str(component_path)],
            cwd=self.root_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        epoch = result.stdout.strip()
        return epoch if result.returncode == 0 and epoch.isdigit() else "315532800"

    def _find_component_config(self, category: str, name: str) -> dict[str, Any] | None:
        """Find component configuration by name."""
        if category == "libs":
            components = self.config["libs"]["components"]
        elif category == "strategies":
            components = self.config["strategies"]["groups"]
        elif category == "apps":
            components = self.config["apps"]["components"]
        else:
            return None

        for component in components:
            if component["name"] == name:
                return cast(dict[str, Any], component)

        return None

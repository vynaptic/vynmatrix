"""Virtual environment manager."""

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from rich.console import Console

console = Console()


class VenvManager:
    """Manages virtual environments."""

    def __init__(self, venvs_dir: Path, python_version: str = "3.11.13") -> None:
        self.venvs_dir = venvs_dir
        self.python_version = python_version
        self.venvs_dir.mkdir(parents=True, exist_ok=True)

    def _find_python_executable(self) -> str:
        """
        Find the Python executable for the specified version.

        Returns:
            Path to Python executable

        Raises:
            RuntimeError: If Python executable not found
        """
        # Extract major.minor (e.g., "3.11.13" -> "3.11")
        major_minor = ".".join(self.python_version.split(".")[:2])

        # Try different Python executable names
        python_candidates = [
            f"python{self.python_version}",
            f"python{major_minor}",
            f"/usr/local/bin/python{major_minor}",
            f"/opt/homebrew/bin/python{major_minor}",
            f"~/.pyenv/versions/{self.python_version}/bin/python",
        ]

        for candidate in python_candidates:
            # Expand ~ in path
            candidate_path = Path(candidate).expanduser()

            try:
                # Check if executable exists and is correct version
                result = subprocess.run(
                    [str(candidate_path), "--version"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode == 0:
                    version_output = result.stdout.strip()
                    # Check if version matches (at least major.minor)
                    if major_minor in version_output:
                        msg = f"Found Python {major_minor}: {candidate_path}"
                        console.print(f"[green]{msg}[/green]")
                        return str(candidate_path)
            except (FileNotFoundError, PermissionError):
                continue

        # If not found, provide helpful error message
        msg = (
            f"Python {self.python_version} not found!\n"
            f"Please install Python {self.python_version}:\n"
            f"  - pyenv: pyenv install {self.python_version}\n"
            f"  - Homebrew: brew install python@{major_minor}\n"
            f"  - apt: sudo apt install python{major_minor}\n"
        )
        raise RuntimeError(msg)

    @staticmethod
    def _validate_package_path(package_path: Path | None) -> Path | None:
        if package_path is None:
            return None
        package_path = package_path.absolute()
        if not package_path.is_dir():
            msg = f"Package path not found: {package_path}"
            raise FileNotFoundError(msg)
        metadata_files = ("pyproject.toml", "setup.py")
        if not any((package_path / filename).is_file() for filename in metadata_files):
            msg = f"No Python package metadata found in: {package_path}"
            raise FileNotFoundError(msg)
        return package_path

    @staticmethod
    def _validate_constraints_file(constraints_file: Path | None) -> Path | None:
        if constraints_file is None:
            return None
        constraints_file = constraints_file.absolute()
        if not constraints_file.is_file():
            msg = f"Constraints file not found: {constraints_file}"
            raise FileNotFoundError(msg)
        return constraints_file

    @staticmethod
    def clean_package_build_artifacts(package_path: Path) -> None:
        """Remove generated package state that can resurrect deleted modules.

        Setuptools may reuse ``build/lib`` and stale ``*.egg-info/SOURCES.txt``
        for both wheel builds and direct source installs. Clean only generated
        paths beneath the explicitly validated package directory.
        """
        package = VenvManager._validate_package_path(package_path)
        if package is None:
            msg = "Package path is required for artifact cleanup"
            raise ValueError(msg)
        generated_paths = {
            package / "build",
            package / "dist",
            *package.rglob("*.egg-info"),
            *package.rglob("__pycache__"),
        }
        for path in sorted(generated_paths, key=lambda item: len(item.parts), reverse=True):
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path)

    @staticmethod
    def _install(
        pip_path: Path,
        install_args: list[str],
        *,
        description: str,
    ) -> None:
        try:
            result = subprocess.run(
                [str(pip_path), "install", *install_args],
                check=True,
                capture_output=True,
                text=True,
            )
            if result.stdout:
                console.print(f"[grey50]{result.stdout.strip()}[/grey50]")
        except subprocess.CalledProcessError as exc:
            console.print(f"[red]Failed to install {description}[/red]")
            if exc.stdout:
                console.print(f"[yellow]{exc.stdout.strip()}[/yellow]")
            if exc.stderr:
                console.print(f"[red]{exc.stderr.strip()}[/red]")
            raise

    def _verify_packages(self, python_path: Path, import_names: Sequence[str]) -> None:
        verification_environment = os.environ.copy()
        # A developer PYTHONPATH can make an empty or stale venv appear healthy
        # by importing directly from this checkout. Verification must exercise
        # only what pip installed into the target environment.
        verification_environment.pop("PYTHONPATH", None)
        subprocess.run(
            [str(python_path), "-m", "pip", "check"],
            check=True,
            capture_output=True,
            text=True,
            env=verification_environment,
        )
        for import_name in import_names:
            subprocess.run(
                [
                    str(python_path),
                    "-c",
                    "import importlib, sys; importlib.import_module(sys.argv[1])",
                    import_name,
                ],
                check=True,
                capture_output=True,
                text=True,
                cwd=self.venvs_dir,
                env=verification_environment,
            )
            # A distribution can import successfully while advertising a
            # broken console command (for example, a missing ``main`` symbol).
            # Resolve only entry points owned by the installed distribution(s)
            # that provide this top-level package; third-party commands are
            # outside this component's packaging contract.
            subprocess.run(
                [
                    str(python_path),
                    "-c",
                    (
                        "import importlib.metadata as md, sys; "
                        "names=md.packages_distributions().get(sys.argv[1], []); "
                        "[entry.load() for name in names "
                        "for entry in md.distribution(name).entry_points "
                        "if entry.group == 'console_scripts']"
                    ),
                    import_name,
                ],
                check=True,
                capture_output=True,
                text=True,
                cwd=self.venvs_dir,
                env=verification_environment,
            )

    def create_venv(
        self,
        name: str,
        requirements: list[str],
        *,
        package_path: Path | None = None,
        import_name: str | None = None,
        import_names: Sequence[str] | None = None,
        constraints_file: Path | None = None,
    ) -> None:
        """Create a virtual environment and install its dependency closure.

        Application environments pass ``package_path`` so the application's own
        packaging metadata is installed after its internal wheels.  Without that
        final install, pip cannot validate ``install_requires`` and a venv can be
        reported as successful while runtime imports are absent.
        """
        package_path = self._validate_package_path(package_path)
        constraints_file = self._validate_constraints_file(constraints_file)
        configured_imports = [
            name.strip()
            for name in ([import_name] if import_name is not None else [])
            + list(import_names or ())
            if name.strip()
        ]
        if len(configured_imports) != len(set(configured_imports)):
            msg = "Venv import verification contains duplicate package names"
            raise ValueError(msg)
        if package_path is not None:
            self.clean_package_build_artifacts(package_path)

        venv_path = self.venvs_dir / name

        if venv_path.exists():
            console.print(f"[yellow]Venv {name} already exists, recreating...[/yellow]")
            shutil.rmtree(venv_path)

        # Find Python executable
        python_exe = self._find_python_executable()

        # Create venv using subprocess with specific Python version
        console.print(f"[cyan]Creating venv: {name} (Python {self.python_version})[/cyan]")
        subprocess.run([python_exe, "-m", "venv", str(venv_path)], check=True)

        # Get pip path
        if os.name == "nt":  # Windows
            pip_path = venv_path / "Scripts" / "pip.exe"
            python_path = venv_path / "Scripts" / "python.exe"
        else:  # Unix
            pip_path = venv_path / "bin" / "pip"
            python_path = venv_path / "bin" / "python"

        # Upgrade pip
        subprocess.run(
            [str(python_path), "-m", "pip", "install", "--upgrade", "pip"],
            check=True,
            capture_output=True,
        )

        # Install requirements
        constraint_args = (
            ["--constraint", str(constraints_file)] if constraints_file is not None else []
        )
        # Make local wheels resolvable as transitive deps: a component wheel
        # (e.g. lib_application) declares deps on sibling local wheels (lib_data)
        # that are not on PyPI, so pip must be pointed at the wheel dir(s) via
        # --find-links or it fails to resolve them.
        find_links: list[str] = []
        for req in requirements:
            if req.endswith(".whl"):
                wheel_dir = str(Path(req).parent)
                if wheel_dir not in find_links:
                    find_links.append(wheel_dir)
        find_links_args = [arg for link in find_links for arg in ("--find-links", link)]
        if requirements:
            console.print(f"[cyan]Installing requirements for {name}...[/cyan]")
            for req in requirements:
                self._install(
                    pip_path,
                    [*constraint_args, *find_links_args, req],
                    description=req,
                )

        if package_path is not None:
            console.print(f"[cyan]Installing application package for {name}...[/cyan]")
            self._install(
                pip_path,
                [*constraint_args, *find_links_args, str(package_path)],
                description=f"application package {package_path}",
            )

        if package_path is not None or configured_imports:
            # Installing a source application or strategy wheel makes pip aware
            # of its runtime contract. Check the closure and prove the configured
            # import works outside the repository before declaring the venv
            # complete.
            self._verify_packages(python_path, configured_imports)

        console.print(f"[green]✓ Venv {name} created successfully[/green]")

    def list_venvs(self) -> list[str]:
        """List all virtual environments."""
        return [d.name for d in self.venvs_dir.iterdir() if d.is_dir()]

    def delete_venv(self, name: str) -> None:
        """Delete a virtual environment."""
        venv_path = self.venvs_dir / name
        if venv_path.exists():
            shutil.rmtree(venv_path)
            console.print(f"[green]✓ Deleted venv: {name}[/green]")
        else:
            console.print(f"[yellow]Venv {name} not found[/yellow]")

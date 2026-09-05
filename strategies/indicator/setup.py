from pathlib import Path

from setuptools import find_packages, setup

_ROOT = Path(__file__).resolve().parent
_STRATEGY_PACKAGES = sorted(
    path.name
    for path in _ROOT.iterdir()
    if path.is_dir()
    and not path.name.startswith((".", "_"))
    and (path / "core.py").is_file()
    and (path / "config.json").is_file()
)
_PACKAGE_DATA = {package: ["config.json"] for package in _STRATEGY_PACKAGES}

setup(
    name="vynmatrix-indicator",
    version="0.1.0",
    description="Indicator-based trading strategies for the vynmatrix platform",
    author="vynmatrix Team",
    # Strategy directories are PEP 420 namespace packages: keeping their
    # on-disk names makes the installed artifact and the production source
    # loader address the same ``<Strategy>/core.py`` payload.
    packages=[*find_packages(exclude=["tests*"]), *_STRATEGY_PACKAGES],
    package_data=_PACKAGE_DATA,
    include_package_data=False,
    python_requires=">=3.11",
    install_requires=[
        # Strategy cores import only the indicator and strategy domain layers.
        "lib-indicators>=0.1.0",
        "lib-strategy>=0.2.0",
    ],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3.11",
    ],
)

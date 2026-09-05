"""Setup configuration for lib_application."""

from setuptools import find_packages, setup

setup(
    name="lib-application",
    version="0.1.0",
    description="Application layer - use cases, orchestration",
    author="vynmatrix Team",
    packages=find_packages(exclude=["tests*"]),
    package_data={
        "lib_application": ["py.typed"],
    },
    python_requires=">=3.11",
    install_requires=[
        "lib-common>=0.1.0",  # Logging, env parsing, event contracts
        "lib-strategy>=0.2.0",  # Domain layer (entities, ports, spreads)
        "lib-data>=0.1.0",  # Data-layer primitives (CandleRow, symbol normalization)
        "sqlalchemy>=2.0.51,<3.0.0",  # ORM models, session factory, outbox store
        # NOTE: NO infrastructure dependencies!
    ],
    zip_safe=False,
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3.11",
    ],
)

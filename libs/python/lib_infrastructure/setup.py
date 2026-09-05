"""Setup configuration for lib_infrastructure."""

from setuptools import find_packages, setup

setup(
    name="lib-infrastructure",
    version="0.1.0",
    description="Infrastructure layer - database, external APIs, adapters",
    author="vynmatrix Team",
    packages=find_packages(exclude=["tests*"]),
    package_data={
        "lib_infrastructure": ["py.typed"],
    },
    python_requires=">=3.11",
    install_requires=[
        "lib-common>=0.1.0",  # Logging, retries, HTTP exceptions
        "lib-data>=0.1.0",  # Market-data primitives consumed by providers
        "lib-strategy>=0.2.0",  # Domain layer (ports and entities)
        "lib-application>=0.1.0",  # Production ORM models and application services
        "sqlalchemy>=2.0.51,<3.0.0",
        "psycopg2-binary>=2.9.12,<3.0.0",  # PostgreSQL adapter
        "httpx>=0.28.1,<1.0.0",  # Broker and market-data HTTP adapters
        "cryptography>=49.0.0,<50.0.0",  # Fernet at-rest encryption for DbSecretsProvider
    ],
    zip_safe=False,
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3.11",
    ],
)

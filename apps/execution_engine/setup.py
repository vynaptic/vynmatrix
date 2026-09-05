"""Setup configuration for execution_engine."""

from setuptools import find_packages, setup

setup(
    name="execution-engine",
    version="0.1.0",
    description="Execution Engine - Signal execution with broker adapters",
    author="vynmatrix Team",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "lib-common>=0.1.0",
        "lib-data>=0.1.0",
        "lib-strategy>=0.2.0",
        "lib-application>=0.1.0",
        "lib-infrastructure>=0.1.0",
        "fastapi>=0.140.0,<1.0.0",
        "uvicorn>=0.51.0,<1.0.0",
        "pydantic>=2.13.4,<3.0.0",
        "sqlalchemy>=2.0.51,<3.0.0",
        "psycopg2-binary>=2.9.12,<3.0.0",
    ],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3.11",
    ],
)

"""Setup for the database-fed indicator strategy runner."""

from setuptools import find_packages, setup

setup(
    name="indicator-runner",
    version="0.1.0",
    description="Database-fed indicator strategy orchestrator",
    author="vynmatrix Team",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "lib-common>=0.1.0",
        "lib-data>=0.1.0",
        "lib-strategy>=0.2.0",
        "lib-application>=0.1.0",
        "httpx>=0.28.1,<1.0.0",
        "jsonschema>=4.26.0,<5.0.0",
        "psutil>=7.2.2,<8.0.0",
        "sqlalchemy>=2.0.51,<3.0.0",
        "psycopg2-binary>=2.9.12,<3.0.0",
    ],
    entry_points={
        "console_scripts": [
            "indicator-runner=indicator_runner.main:main",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3.11",
    ],
)

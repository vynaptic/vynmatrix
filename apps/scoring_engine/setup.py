"""Setup configuration for scoring_engine."""

from setuptools import find_packages, setup

setup(
    name="scoring-engine",
    version="0.1.0",
    description="Scoring Engine - aggregates signals into scores and triggers execution rules",
    author="vynmatrix Team",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "lib-common>=0.1.0",
        "lib-data>=0.1.0",
        "lib-strategy>=0.2.0",
        "lib-application>=0.1.0",
        "fastapi>=0.140.0,<1.0.0",
        "uvicorn>=0.51.0,<1.0.0",
        "httpx>=0.28.1,<1.0.0",
        "pydantic>=2.13.4,<3.0.0",
        "sqlalchemy>=2.0.51,<3.0.0",
    ],
    entry_points={
        "console_scripts": [
            "scoring-engine=scoring_engine.main:main",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3.11",
    ],
)

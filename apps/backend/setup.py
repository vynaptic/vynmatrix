"""Backend config API — tenant self-service surface (bindings + broker onboarding)."""

from setuptools import find_packages, setup

setup(
    name="backend",
    version="0.1.0",
    packages=find_packages(exclude=["tests", "tests.*"]),
    python_requires=">=3.11",
    install_requires=[
        "lib-common>=0.1.0",
        "lib-application>=0.1.0",
        "lib-infrastructure>=0.1.0",
        "fastapi>=0.140.0,<1.0.0",
        "uvicorn>=0.51.0,<1.0.0",
        "pydantic>=2.13.4,<3.0.0",
    ],
)

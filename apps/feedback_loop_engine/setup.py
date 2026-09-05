"""Setup for the Feedback Loop Engine application."""

from setuptools import find_packages, setup

setup(
    name="feedback_loop_engine",
    version="0.1.0",
    description="Strategy signal performance monitoring and parameter optimization",
    author="vynmatrix",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "fastapi>=0.140.0,<1.0.0",
        "uvicorn>=0.51.0,<1.0.0",
        "sqlalchemy>=2.0.51,<3.0.0",
        "pydantic>=2.13.4,<3.0.0",
        "lib-common>=0.1.0",
        "lib-data>=0.1.0",
        "lib-strategy>=0.2.0",
        "lib-application>=0.1.0",
        "lib-infrastructure>=0.1.0",
    ],
    entry_points={
        "console_scripts": [
            "feedback-loop-engine=feedback_loop_engine.main:main",
        ],
    },
)

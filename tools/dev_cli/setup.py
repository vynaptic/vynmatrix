from setuptools import find_packages, setup

setup(
    name="vmdev",
    version="0.1.0",
    description="vynmatrix Developer CLI",
    packages=find_packages(exclude=["tests*"]),
    python_requires=">=3.11",
    install_requires=[
        "click>=8.4.1,<9.0.0",
        "cryptography>=49.0.0,<50.0.0",
        "httpx>=0.28.1,<1.0.0",
        "rich>=15.0.0,<16.0.0",
        "pyyaml>=6.0.3,<7.0",
    ],
    entry_points={
        "console_scripts": [
            "vmdev=dev_cli.main:cli",
        ],
    },
)

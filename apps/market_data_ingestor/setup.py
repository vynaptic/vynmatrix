from setuptools import find_packages, setup

setup(
    name="market_data_ingestor",
    version="0.1.0",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "lib-common>=0.1.0",
        "lib-data>=0.1.0",
        "lib-application>=0.1.0",
        "lib-infrastructure>=0.1.0",
        "lib-strategy>=0.2.0",
        "fastapi>=0.140.0,<1.0.0",
        "uvicorn>=0.51.0,<1.0.0",
        "sqlalchemy>=2.0.51,<3.0.0",
        "httpx>=0.28.1,<1.0.0",
    ],
    entry_points={
        "console_scripts": [
            "market-data-ingestor=market_data_ingestor.main:main",
        ],
    },
)

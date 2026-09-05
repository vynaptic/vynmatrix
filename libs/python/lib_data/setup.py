from setuptools import find_packages, setup

setup(
    name="lib-data",
    version="0.1.0",
    description="Data handling and processing utilities for the vynmatrix platform",
    author="vynmatrix Team",
    packages=find_packages(exclude=["tests*"]),
    package_data={
        "lib_data": ["py.typed"],
    },
    python_requires=">=3.11",
    install_requires=[
        "lib-common>=0.1.0",
    ],
    extras_require={
        # Scheduled-market session math (lib_data.sessions) — needed only by
        # the market-data runtime profile; every other image stays free of the
        # heavy analytics closure.
        "sessions": [
            "pandas>=3.0.3,<4.0.0",
            "exchange_calendars>=4.7,<5.0",
        ],
    },
    zip_safe=False,
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3.11",
    ],
)

from setuptools import find_packages, setup

setup(
    name="lib-strategy",
    version="0.2.0",
    description="Strategy domain layer - framework and persistence independent",
    author="vynmatrix Team",
    packages=find_packages(exclude=["tests*"]),
    package_data={
        "lib_strategy": ["py.typed"],
    },
    python_requires=">=3.11",
    install_requires=[
        # True direct imports only — wheel metadata is the statically
        # auditable dependency contract (images install with --no-deps).
        "lib-common>=0.1.0",
        # pandas is an optional, guarded import (BacktestSignalEmitter frame
        # export); it is deliberately NOT a mandatory runtime dependency.
    ],
    zip_safe=False,
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3.11",
    ],
)

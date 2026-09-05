from setuptools import find_packages, setup

setup(
    name="lib-indicators",
    version="0.1.0",
    description="Technical indicators library for the vynmatrix platform",
    author="vynmatrix Team",
    packages=find_packages(exclude=["tests*"]),
    package_data={
        "lib_indicators": ["py.typed"],
    },
    python_requires=">=3.11",
    install_requires=[
        # Pure-stdlib streaming indicators: no third-party or first-party
        # runtime dependencies.
    ],
    zip_safe=False,
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3.11",
    ],
)

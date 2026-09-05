from setuptools import find_packages, setup

setup(
    name="lib-common",
    version="0.1.0",
    description="Common utilities for the vynmatrix platform",
    author="vynmatrix Team",
    packages=find_packages(exclude=["tests*"]),
    package_data={
        "lib_common": ["py.typed"],
    },
    python_requires=">=3.11",
    install_requires=[
        "pyyaml>=6.0.3,<7.0",
        "python-dotenv>=1.2.2,<2.0.0",
        "pydantic>=2.13.4,<3.0.0",
        "fastapi>=0.140.0,<1.0.0",
        "httpx>=0.28.1,<1.0.0",
        "prometheus-client>=0.25.0,<1.0.0",
    ],
    zip_safe=False,
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3.11",
    ],
)

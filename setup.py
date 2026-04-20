from setuptools import find_packages, setup

setup(
    name="pcdesign",
    version="0.1.0",
    description="Proposer/Critic adversarial design CLI using codex app-server.",
    python_requires=">=3.10",
    packages=find_packages(include=["pcd", "pcd.*"]),
    install_requires=[],
    entry_points={
        "console_scripts": [
            "pcd = pcd.cli:main",
        ],
    },
)

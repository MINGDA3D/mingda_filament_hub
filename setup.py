#!/usr/bin/env python3
"""
送料柜自动续料系统 - 安装脚本
"""
from setuptools import setup, find_packages

with open('README.md', 'r', encoding='utf-8') as f:
    long_description = f.read()

setup(
    name="feeder_cabinet",
    version="1.0.0",
    author="Mingda",
    author_email="your-email@example.com",
    description="自动续料系统 - 3D打印机自动送料解决方案",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/your-username/feeder_cabinet",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: POSIX :: Linux",
        "Topic :: Printing",
        "Topic :: System :: Hardware",
    ],
    python_requires=">=3.7",
    install_requires=[
        "python-can>=4.0.0",
        "requests>=2.25.0",
        "pyyaml>=5.1.0",
    ],
    entry_points={
        "console_scripts": [
            "feeder_cabinet=feeder_cabinet.main:main",
        ],
    },
    include_package_data=True,
    zip_safe=False,
) 
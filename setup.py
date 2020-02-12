from codecs import open
from os import path

from setuptools import setup, find_packages

here = path.abspath(path.dirname(__file__))

# Get the long description from the README file
with open(path.join(here, "README.md"), encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="readout",
    version="0.1.1",
    description="A framework for detecting changes and reacting to them.",
    long_description=long_description,
    url="https://github.com/lainproliant/readout",
    author="Lain Musgrove (lainproliant)",
    author_email="lain.proliant@gmail.com",
    license="MIT",
    classifiers=[
        "Intended Audience :: Developers",
        "Topic :: Software Development :: Libraries :: Application Frameworks",
        "License :: OSI Approved :: BSD License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8"
    ],
    keywords="monitoring framework API",
    py_modules=['readout'],
    install_requires=["lexex"],
    extras_require={},
    package_data={'readout': ['LICENSE']},
    data_files=[],
    entry_points={"console_scripts": []},
)

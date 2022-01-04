import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="vreducer",
    version="0.0.1",
    install_requires=[
        "Pillow",
    ],
    author="t_furu",
    author_email="t_furu@tf-web.jp",
    description="VReducer VRoidMobile support",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/tfuru/VReducer",
    packages=setuptools.find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.9',
)
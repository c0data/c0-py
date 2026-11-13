from setuptools import Extension, setup

setup(
    ext_modules=[
        Extension(
            "c0._c0",
            sources=["src/c0/_c0module.c", "c0-c/c0_diff.c"],
            include_dirs=["c0-c"],
        )
    ],
)

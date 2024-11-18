# Copyright (c) 2023, Tri Dao.
import sys
import warnings
import os
import re
import ast
from pathlib import Path
from packaging.version import parse, Version
import platform

from setuptools import setup, find_packages
import subprocess

import urllib.request
import urllib.error
from wheel.bdist_wheel import bdist_wheel as _bdist_wheel

import torch
from torch.utils.cpp_extension import (
    BuildExtension,
    CppExtension,
    CUDAExtension,
    CUDA_HOME,
)

def get_hip_version():
    return parse(torch.version.hip.split()[-1].rstrip('-').replace('-', '+'))

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()


# ninja build does not work unless include_dirs are abs path
this_dir = os.path.dirname(os.path.abspath(__file__))

PACKAGE_NAME = "fast_hadamard_transform"

BASE_WHEEL_URL = "https://github.com/Dao-AILab/fast-hadamard-transform/releases/download/{tag_name}/{wheel_name}"

# FORCE_BUILD: Force a fresh build locally, instead of attempting to find prebuilt wheels
# SKIP_CUDA_BUILD: Intended to allow CI to use a simple `python setup.py sdist` run to copy over raw files, without any cuda compilation
FORCE_BUILD = os.getenv("FAST_HADAMARD_TRANSFORM_FORCE_BUILD", "FALSE") == "TRUE"
SKIP_CUDA_BUILD = os.getenv("FAST_HADAMARD_TRANSFORM_SKIP_CUDA_BUILD", "FALSE") == "TRUE"
# For CI, we want the option to build with C++11 ABI since the nvcr images use C++11 ABI
FORCE_CXX11_ABI = os.getenv("FAST_HADAMARD_TRANSFORM_FORCE_CXX11_ABI", "FALSE") == "TRUE"


def get_platform():
    """
    Returns the platform name as used in wheel filenames.
    """
    if sys.platform.startswith("linux"):
        return "linux_x86_64"
    elif sys.platform == "darwin":
        mac_version = ".".join(platform.mac_ver()[0].split(".")[:2])
        return f"macosx_{mac_version}_x86_64"
    elif sys.platform == "win32":
        return "win_amd64"
    else:
        raise ValueError("Unsupported platform: {}".format(sys.platform))




def check_if_cuda_home_none(global_option: str) -> None:
    if CUDA_HOME is not None:
        return
    # warn instead of error because user could be downloading prebuilt wheels, so nvcc won't be necessary
    # in that case.
    warnings.warn(
        f"{global_option} was requested, but nvcc was not found.  Are you sure your environment has nvcc available?  "
        "If you're installing within a container from https://hub.docker.com/r/pytorch/pytorch, "
        "only images whose names contain 'devel' will provide nvcc."
    )


def append_nvcc_threads(nvcc_extra_args):
    return nvcc_extra_args #+ ["--threads", "4"]


cmdclass = {}
ext_modules = []

if not SKIP_CUDA_BUILD:
    print("\n\ntorch.__version__  = {}\n\n".format(torch.__version__))
    TORCH_MAJOR = int(torch.__version__.split(".")[0])
    TORCH_MINOR = int(torch.__version__.split(".")[1])

    check_if_cuda_home_none("fast_hadamard_transform")
    # Check, if CUDA11 is installed for compute capability 8.0
    cc_flag = [f"--offload-arch=gfx90a"]

    # HACK: The compiler flag -D_GLIBCXX_USE_CXX11_ABI is set to be the same as
    # torch._C._GLIBCXX_USE_CXX11_ABI
    # https://github.com/pytorch/pytorch/blob/8472c24e3b5b60150096486616d98b7bea01500b/torch/utils/cpp_extension.py#L920
    if FORCE_CXX11_ABI:
        torch._C._GLIBCXX_USE_CXX11_ABI = True

    cc_flag += ["-O3","-std=c++17",
                "-DCK_TILE_FMHA_FWD_FAST_EXP2=1",
                "-fgpu-flush-denormals-to-zero",
                "-DCK_ENABLE_BF16",
                "-DCK_ENABLE_BF8",
                "-DCK_ENABLE_FP16",
                "-DCK_ENABLE_FP32",
                "-DCK_ENABLE_FP64",
                "-DCK_ENABLE_FP8",
                "-DCK_ENABLE_INT8",
                "-DCK_USE_XDL",
                "-DUSE_PROF_API=1",
                # "-DFLASHATTENTION_DISABLE_BACKWARD",
                "-D__HIP_PLATFORM_HCC__=1"]

    cc_flag += [f"-DCK_TILE_FLOAT_TO_BFLOAT16_DEFAULT={os.environ.get('CK_TILE_FLOAT_TO_BFLOAT16_DEFAULT', 3)}"]

    # Imitate https://github.com/ROCm/composable_kernel/blob/c8b6b64240e840a7decf76dfaa13c37da5294c4a/CMakeLists.txt#L190-L214
    hip_version = get_hip_version()
    if hip_version > Version('5.7.23302'):
        cc_flag += ["-fno-offload-uniform-block"]
    if hip_version > Version('6.1.40090'):
        cc_flag += ["-mllvm", "-enable-post-misched=0"]
    if hip_version > Version('6.2.41132'):
        cc_flag += ["-mllvm", "-amdgpu-early-inline-all=true",
                    "-mllvm", "-amdgpu-function-calls=false"]
    if hip_version > Version('6.2.41133') and hip_version < Version('6.3.00000'):
        cc_flag += ["-mllvm", "-amdgpu-coerce-illegal-types=1"]

    ext_modules.append(
        CUDAExtension(
            name="fast_hadamard_transform_hip",
            sources=[
                "csrc/fast_hadamard_transform.cpp",
                "csrc/fast_hadamard_transform_hip.hip",
            ],
            extra_compile_args={
                "cxx": ["-O3", "-std=c++17"],
                "nvcc": cc_flag,
            },
            include_dirs=[this_dir],
        )
    )


def get_package_version():
    with open(Path(this_dir) / "fast_hadamard_transform" / "__init__.py", "r") as f:
        version_match = re.search(r"^__version__\s*=\s*(.*)$", f.read(), re.MULTILINE)
    public_version = ast.literal_eval(version_match.group(1))
    local_version = os.environ.get("FAST_HADAMARD_TRANSFORM_LOCAL_VERSION")
    if local_version:
        return f"{public_version}+{local_version}"
    else:
        return str(public_version)


def get_wheel_url():
    # Determine the version numbers that will be used to determine the correct wheel
    # We're using the CUDA version used to build torch, not the one currently installed
    # _, cuda_version_raw = get_cuda_bare_metal_version(CUDA_HOME)
    torch_cuda_version = parse(torch.version.hip)
    torch_version_raw = parse(torch.__version__)
    # For CUDA 11, we only compile for CUDA 11.8, and for CUDA 12 we only compile for CUDA 12.2
    # to save CI time. Minor versions should be compatible.
    torch_cuda_version = parse("11.8") if torch_cuda_version.major == 11 else parse("12.2")
    python_version = f"cp{sys.version_info.major}{sys.version_info.minor}"
    platform_name = get_platform()
    fast_hadamard_transform_version = get_package_version()
    # cuda_version = f"{cuda_version_raw.major}{cuda_version_raw.minor}"
    cuda_version = f"{torch_cuda_version.major}{torch_cuda_version.minor}"
    torch_version = f"{torch_version_raw.major}.{torch_version_raw.minor}"
    cxx11_abi = str(torch._C._GLIBCXX_USE_CXX11_ABI).upper()

    # Determine wheel URL based on CUDA version, torch version, python version and OS
    wheel_filename = f"{PACKAGE_NAME}-{fast_hadamard_transform_version}+cu{cuda_version}torch{torch_version}cxx11abi{cxx11_abi}-{python_version}-{python_version}-{platform_name}.whl"
    wheel_url = BASE_WHEEL_URL.format(
        tag_name=f"v{fast_hadamard_transform_version}", wheel_name=wheel_filename
    )
    return wheel_url, wheel_filename


class CachedWheelsCommand(_bdist_wheel):
    """
    The CachedWheelsCommand plugs into the default bdist wheel, which is ran by pip when it cannot
    find an existing wheel (which is currently the case for all installs). We use
    the environment parameters to detect whether there is already a pre-built version of a compatible
    wheel available and short-circuits the standard full build pipeline.
    """

    def run(self):
        if FORCE_BUILD:
            return super().run()

        wheel_url, wheel_filename = get_wheel_url()
        print("Guessing wheel URL: ", wheel_url)
        try:
            urllib.request.urlretrieve(wheel_url, wheel_filename)

            # Make the archive
            # Lifted from the root wheel processing command
            # https://github.com/pypa/wheel/blob/cf71108ff9f6ffc36978069acb28824b44ae028e/src/wheel/bdist_wheel.py#LL381C9-L381C85
            if not os.path.exists(self.dist_dir):
                os.makedirs(self.dist_dir)

            impl_tag, abi_tag, plat_tag = self.get_tag()
            archive_basename = f"{self.wheel_dist_name}-{impl_tag}-{abi_tag}-{plat_tag}"

            wheel_path = os.path.join(self.dist_dir, archive_basename + ".whl")
            print("Raw wheel path", wheel_path)
            os.rename(wheel_filename, wheel_path)
        except urllib.error.HTTPError:
            print("Precompiled wheel not found. Building from source...")
            # If the wheel could not be downloaded, build from source
            super().run()


setup(
    name=PACKAGE_NAME,
    version=get_package_version(),
    packages=find_packages(
        exclude=(
            "build",
            "csrc",
            "include",
            "tests",
            "dist",
            "docs",
            "benchmarks",
            "fast_hadamard_transform.egg-info",
        )
    ),
    author="Tri Dao",
    author_email="tri@tridao.me",
    description="Fast Hadamard Transform in CUDA, with a PyTorch interface",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/Dao-AILab/fast-hadamard-transform",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: BSD License",
        "Operating System :: Unix",
    ],
    ext_modules=ext_modules,
    cmdclass={"bdist_wheel": CachedWheelsCommand, "build_ext": BuildExtension}
    if ext_modules
    else {
        "bdist_wheel": CachedWheelsCommand,
    },
    python_requires=">=3.7",
    install_requires=[
        "torch",
        "packaging",
        "ninja",
    ],
)

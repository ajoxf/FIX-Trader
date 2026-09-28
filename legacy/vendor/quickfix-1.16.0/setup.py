import os

from setuptools import setup, Extension
import distutils.sysconfig
import glob

# Remove the "-Wstrict-prototypes" compiler option, which isn't valid for C++.
cfg_vars = distutils.sysconfig.get_config_vars()
for key, value in cfg_vars.items():
    if type(value) == str:
        cfg_vars[key] = value.replace("-Wstrict-prototypes", "")

setup(
    data_files=[('share/quickfix', glob.glob('spec/FIX*.xml'))],
    include_dirs=['C++', 'swig'],
    ext_modules=[
        Extension(
            '_quickfix',
            glob.glob('C++/*.cpp'),
            # The PyPI source distribution supplies GCC-only flags. MSVC
            # rejects them, so use its default C++ compiler flags on Windows.
            extra_compile_args=[] if os.name == 'nt' else [
                '-std=c++17',
                '-Wno-deprecated',
                '-Wno-unused-variable',
                '-Wno-unused-label',
                '-Wno-deprecated-declarations',
                '-Wno-maybe-uninitialized',
                '-D__cpp_noexcept_function_type'
            ]
        )
    ],
)

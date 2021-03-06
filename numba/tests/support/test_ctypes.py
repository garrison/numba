"""
Test support for ctypes. See also numba.tests.foreign_call.test_ctypes_call.
"""

import ctypes

from numba import *
from numba import environment
from numba.support import ctypes_support

try:
    from numba.tests.support import ctypes_values
except ImportError:
    ctypes_values = None

#-------------------------------------------------------------------
# Utilities
#-------------------------------------------------------------------

env = environment.NumbaEnvironment.get_environment()
context = env.context
from_python = context.typemapper.from_python

def get_cast_type(type):
    assert type.is_cast
    return type.dst_type

def assert_signature(ctypes_func, expected=None):
    sig = from_python(ctypes_func)
    assert sig.is_pointer_to_function
    if expected:
        assert sig.signature == expected, (sig.signature, expected)

#-------------------------------------------------------------------
# Tests
#-------------------------------------------------------------------

if ctypes_values:
    rk_state_t = get_cast_type(from_python(ctypes_values.rk_state))

def ctypes_func_values():
    int_or_long = long_ if ctypes.c_int == ctypes.c_long else int_
    long_or_longlong = (longlong if ctypes.c_long == ctypes.c_longlong
                        else long_)

    signature = int_or_long(rk_state_t.pointer())
    assert_signature(ctypes_values.rk_randomseed, signature)

    signature = void(long_or_longlong, rk_state_t.pointer())
    assert_signature(ctypes_values.rk_seed, signature)

    signature = double(rk_state_t.pointer(), double, double)
    assert_signature(ctypes_values.rk_gamma, signature)

def ctypes_data_values():
    assert from_python(ctypes_values.state) == rk_state_t
    assert from_python(ctypes_values.state_p) == rk_state_t.pointer()
    assert from_python(ctypes_values.state_vp) == void.pointer()
    assert from_python(ctypes.c_void_p(10)) == void.pointer()

    ctypes_double_p = ctypes.POINTER(ctypes.c_double)(ctypes.c_double(10))
    assert from_python(ctypes_double_p) == double.pointer()

def test():
    if ctypes_values is not None:
        ctypes_func_values()
        ctypes_data_values()


if __name__ == '__main__':
    test()

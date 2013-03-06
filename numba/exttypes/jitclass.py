"""
Compiling extension classes works as follows:

    * Create an extension Numba/minivect type holding a symtab
    * Capture attribute types in the symtab ...

        * ... from the class attributes:

            @jit
            class Foo(object):
                attr = double

        * ... from __init__

            @jit
            class Foo(object):
                def __init__(self, attr):
                    self.attr = double(attr)

    * Type infer all methods
    * Compile all extension methods

        * Process signatures such as @void(double)
        * Infer native attributes through type inference on __init__
        * Path the extension type with a native attributes struct
        * Infer types for all other methods
        * Update the ext_type with a vtab type
        * Compile all methods

    * Create descriptors that wrap the native attributes
    * Create an extension type:

      {
        PyObject_HEAD
        ...
        virtual function table (func **)
        native attributes
      }

    The virtual function table (vtab) is a ctypes structure set as
    attribute of the extension types. Objects have a direct pointer
    for efficiency.

See also extension_types.pyx
"""

import types
import ctypes

import numba
from numba import *
from numba import error
from numba import typesystem
from numba import pipeline
from numba import symtab
from numba.minivect import minitypes

from numba.exttypes import logger
from numba.exttypes import virtual
from numba.exttypes import signatures
from numba.exttypes import extension_types

#------------------------------------------------------------------------
# Populate Extension Type with Methods
#------------------------------------------------------------------------

def process_method_signatures(class_dict, ext_type):
    """
    Process all method signatures:

        * Verify signatures
        * Populate ext_type with method signatures (ExtMethodType)
    """
    method_maker = signatures.JitMethodMaker(ext_type)
    processor = signatures.MethodSignatureProcessor(class_dict, ext_type,
                                                    method_maker)

    for method, method_type in processor.get_method_signatures():
        ext_type.add_method(method.name, method_type)
        class_dict[method.name] = method

def _type_infer_method(env, ext_type, method, method_name, class_dict, flags):
    if method_name not in ext_type.methoddict:
        return

    signature = ext_type.get_signature(method_name)
    restype, argtypes = signature.return_type, signature.args

    class_dict[method_name] = method
    func_signature, symtab, ast = pipeline.infer_types2(
                        env, method.py_func, restype, argtypes, **flags)
    ext_type.add_method(method_name, func_signature)

def _type_infer_init_method(env, class_dict, ext_type, flags):
    initfunc = class_dict.get('__init__', None)
    if initfunc is None:
        return

    _type_infer_method(env, ext_type, initfunc, '__init__', class_dict, flags)

def _type_infer_methods(env, class_dict, ext_type, flags):
    for method_name, method in class_dict.iteritems():
        if method_name in ('__new__', '__init__') or method is None:
            continue

        _type_infer_method(env, ext_type, method, method_name, class_dict, flags)

def _compile_methods(class_dict, env, ext_type, lmethods, method_pointers,
                     flags):
    parent_method_pointers = getattr(
                    ext_type.py_class, '__numba_method_pointers', None)
    for i, (method_name, func_signature) in enumerate(ext_type.methods):
        if method_name not in class_dict:
            # Inherited method
            assert parent_method_pointers is not None
            name, p = parent_method_pointers[i]
            assert name == method_name
            method_pointers.append((method_name, p))
            continue

        method = class_dict[method_name]
        # Don't use compile_after_type_inference, re-infer, since we may
        # have inferred some return types
        # TODO: delayed types and circular calls/variable assignments
        logger.debug(method.py_func)
        func_env = pipeline.compile2(
            env, method.py_func, func_signature.return_type,
            func_signature.args, name=method.py_func.__name__,
            **flags)
        lmethods.append(func_env.lfunc)
        method_pointers.append((method_name, func_env.translator.lfunc_pointer))
        class_dict[method_name] = method.result(func_env.numba_wrapper_func)

#------------------------------------------------------------------------
# Build Attributes Struct
#------------------------------------------------------------------------

def _construct_native_attribute_struct(ext_type):
    """
    Create attribute struct type from symbol table.
    """
    attrs = dict((name, var.type) for name, var in ext_type.symtab.iteritems())
    if ext_type.attribute_struct is None:
        # No fields to inherit
        ext_type.attribute_struct = numba.struct(**attrs)
    else:
        # Inherit fields from parent
        fields = []
        for name, variable in ext_type.symtab.iteritems():
            if name not in ext_type.attribute_struct.fielddict:
                fields.append((name, variable.type))
                ext_type.attribute_struct.fielddict[name] = variable.type

        # Sort fields by rank
        fields = numba.struct(fields).fields
        ext_type.attribute_struct.fields.extend(fields)

def _create_descr(attr_name):
    """
    Create a descriptor that accesses the attribute on the ctypes struct.
    """
    def _get(self):
        return getattr(self._numba_attrs, attr_name)
    def _set(self, value):
        return setattr(self._numba_attrs, attr_name, value)
    return property(_get, _set)

def inject_descriptors(env, py_class, ext_type, class_dict):
    "Cram descriptors into the class dict"
    for attr_name, attr_type in ext_type.symtab.iteritems():
        descriptor = _create_descr(attr_name)
        class_dict[attr_name] = descriptor

#------------------------------------------------------------------------
# Attribute Inheritance
#------------------------------------------------------------------------

def is_numba_class(cls):
    return hasattr(cls, '__numba_struct_type')

def verify_base_class_compatibility(cls, struct_type, vtab_type):
    "Verify that we can build a compatible class layout"
    bases = [cls]
    for base in cls.__bases__:
        if is_numba_class(base):
            attr_prefix = base.__numba_struct_type.is_prefix(struct_type)
            method_prefix = base.__numba_vtab_type.is_prefix(vtab_type)
            if not attr_prefix or not method_prefix:
                raise error.NumbaError(
                            "Multiple incompatible base classes found: "
                            "%s and %s" % (base, bases[-1]))

            bases.append(base)

def inherit_attributes(ext_type, class_dict):
    "Inherit attributes and methods from superclasses"
    cls = ext_type.py_class
    if not is_numba_class(cls):
        # superclass is not a numba class
        return

    struct_type = cls.__numba_struct_type
    vtab_type = cls.__numba_vtab_type
    verify_base_class_compatibility(cls, struct_type, vtab_type)

    # Inherit attributes
    ext_type.attribute_struct = numba.struct(struct_type.fields)
    for field_name, field_type in ext_type.attribute_struct.fields:
        ext_type.symtab[field_name] = symtab.Variable(field_type,
                                                      promotable_type=False)

    # Inherit methods
    for method_name, method_type in vtab_type.fields:
        func_signature = method_type.base_type
        args = list(func_signature.args)
        if not (func_signature.is_class or func_signature.is_static):
            args[0] = ext_type
        func_signature = func_signature.return_type(*args)
        ext_type.add_method(method_name, func_signature)

    ext_type.parent_attr_struct = struct_type
    ext_type.parent_vtab_type = vtab_type

def process_class_attribute_types(ext_type, class_dict):
    """
    Process class attribute types:

        @jit
        class Foo(object):

            attr = double
    """
    for name, value in class_dict.iteritems():
        if isinstance(value, minitypes.Type):
            ext_type.symtab[name] = symtab.Variable(value, promotable_type=False)

#------------------------------------------------------------------------
# Compile Methods and Build Attributes
#------------------------------------------------------------------------

def compile_extension_methods(env, py_class, ext_type, class_dict, flags):
    """
    Compile extension methods:

        1) Process signatures such as @void(double)
        2) Infer native attributes through type inference on __init__
        3) Path the extension type with a native attributes struct
        4) Infer types for all other methods
        5) Update the ext_type with a vtab type
        6) Compile all methods
    """
    method_pointers = []
    lmethods = []

    class_dict['__numba_py_class'] = py_class

    process_method_signatures(class_dict, ext_type)
    _type_infer_init_method(env, class_dict, ext_type, flags)
    _construct_native_attribute_struct(ext_type)
    _type_infer_methods(env, class_dict, ext_type, flags)

    # TODO: patch method call types

    # Set vtab type before compiling
    ext_type.vtab_type = numba.struct(
        [(field_name, field_type.pointer())
         for field_name, field_type in ext_type.methods])
    _compile_methods(class_dict, env, ext_type, lmethods, method_pointers,
                     flags)
    return method_pointers, lmethods

#------------------------------------------------------------------------
# Build Extension Type
#------------------------------------------------------------------------

def create_extension(env, py_class, flags):
    """
    Compile an extension class given the NumbaEnvironment and the Python
    class that contains the functions that are to be compiled.
    """
    flags.pop('llvm_module', None)

    ext_type = typesystem.ExtensionType(py_class)
    class_dict = dict(vars(py_class))

    inherit_attributes(ext_type, class_dict)
    process_class_attribute_types(ext_type, class_dict)

    method_pointers, lmethods = compile_extension_methods(
            env, py_class, ext_type, class_dict, flags)
    inject_descriptors(env, py_class, ext_type, class_dict)

    vtab, vtab_type = virtual.build_vtab(ext_type.vtab_type, method_pointers)

    logger.debug("struct: %s" % ext_type.attribute_struct)
    logger.debug("ctypes struct: %s" % ext_type.attribute_struct.to_ctypes())

    extension_type = extension_types.create_new_extension_type(
            py_class.__name__, py_class.__bases__, class_dict,
            ext_type, vtab, vtab_type,
            lmethods, method_pointers)
    return extension_type
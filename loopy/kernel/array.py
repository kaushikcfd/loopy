"""Implementation tagging of array axes."""

from __future__ import division

__copyright__ = "Copyright (C) 2012 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import re
from pytools import Record, memoize_method

import pyopencl as cl  # noqa
import pyopencl.array  # noqa

import numpy as np


# {{{ array dimension tags

class ArrayDimImplementationTag(Record):
    pass


class _StrideArrayDimTagBase(ArrayDimImplementationTag):
    pass


class FixedStrideArrayDimTag(_StrideArrayDimTagBase):
    """An arg dimension implementation tag for a fixed (potentially
    symbolic) stride.

    The stride is given in units of :attr:`ArrayBase.dtype`.

    .. attribute :: target_axis

        For objects (such as images) with more than one axis, *target_axis*
        sets which of these indices is being targeted by this dimension.
        Note that there may be multiple dim_tags with the same *target_axis*,
        their contributions are combined additively.

        Note that "normal" arrays only have one *target_axis*.
    """

    def __init__(self, stride, target_axis=0):
        _StrideArrayDimTagBase.__init__(self, stride=stride, target_axis=target_axis)
        self.stride = stride
        self.target_axis = target_axis

    def __str__(self):
        return "stride:%s->%d" % (self.stride, self.target_axis)

    def map_expr(self, mapper):
        return self.copy(stride=mapper(self.stride))


class ComputedStrideArrayDimTag(_StrideArrayDimTagBase):
    """
    :arg order: "C" or "F", indicating whether this argument dimension will be added
        as faster-moving ("C") or more-slowly-moving ("F") than the previous
        argument.
    :arg pad_to: :attr:`ArrayBase.dtype` granularity to which to pad this dimension

    This type of stride arg dim gets converted to :class:`FixedStrideArrayDimTag`
    on input to :class:`ArrayBase` subclasses.
    """

    def __init__(self, order, pad_to=None, target_axis=0):
        order = order.upper()
        if order not in "CF":
            raise ValueError("'order' must be either 'C' or 'F'")

        _StrideArrayDimTagBase.__init__(self, order=order, pad_to=pad_to,
                target_axis=target_axis)

    def __str__(self):
        if self.pad_to is None:
            return self.order
        else:
            return "%s(pad=%s)" % (self.order, self.pad_to)

    def map_expr(self, mapper):
        raise TypeError("ComputedStrideArrayDimTag is a transient type only used "
                "for construction of arrays. It should never have to map its "
                "expressions.")


class SeparateArrayArrayDimTag(ArrayDimImplementationTag):
    def __str__(self):
        return "sep"

    def map_expr(self, mapper):
        return self


class VectorArrayDimTag(ArrayDimImplementationTag):
    def __str__(self):
        return "vec"

    def map_expr(self, mapper):
        return self


PADDED_STRIDE_TAG = re.compile(r"^([a-zA-Z]+)\(pad=(.*)\)$")
TARGET_AXIS_RE = re.compile(r"->([0-9])$")


def parse_array_dim_tag(tag):
    if isinstance(tag, ArrayDimImplementationTag):
        return tag

    if not isinstance(tag, str):
        raise TypeError("arg dimension implementation tag must be "
                "string or tag object")

    if tag.startswith("stride:"):
        from loopy.symbolic import parse
        return FixedStrideArrayDimTag(parse(tag[7:]))
    elif tag == "sep":
        return SeparateArrayArrayDimTag()
    elif tag == "vec":
        return VectorArrayDimTag()

    target_axis_match = TARGET_AXIS_RE.search(tag)

    if target_axis_match is not None:
        target_axis = int(target_axis_match.group(1))
        tag = tag[:target_axis_match.start()]
    else:
        target_axis = 0

    if tag in ["c", "C", "f", "F"]:
        return ComputedStrideArrayDimTag(tag, target_axis=target_axis)
    else:
        padded_stride_match = PADDED_STRIDE_TAG.match(tag)
        if padded_stride_match is None:
            raise ValueError("invalid arg dim tag: '%s'" % tag)

        order = padded_stride_match.group(1)
        pad = parse(padded_stride_match.group(2))

        if order not in ["c", "C", "f", "F"]:
            raise ValueError("invalid arg dim tag: '%s'" % tag)

        return ComputedStrideArrayDimTag(order, pad, target_axis=target_axis)


def parse_array_dim_tags(dim_tags):
    if isinstance(dim_tags, str):
        dim_tags = dim_tags.split(",")

    def parse_dim_tag_if_necessary(dt):
        if isinstance(dt, str):
            dt = parse_array_dim_tag(dt)
        return dt

    return [parse_dim_tag_if_necessary(dt) for dt in dim_tags]


def convert_computed_to_fixed_dim_tags(name, num_user_axes, num_target_axes,
        shape, dim_tags):

    # Just to clarify:
    #
    # - user axes are user-facing--what the user actually uses for indexing.
    #
    # - target axes are implementation facing. Normal in-memory arrays have one.
    #   3D images have three.

    # {{{ pick apart arg dim tags into computed, fixed and vec

    vector_dim = None

    # one list of indices into dim_tags for each target axis
    computed_stride_dim_tags = [[] for i in range(num_target_axes)]
    fixed_stride_dim_tags = [[] for i in range(num_target_axes)]

    for i, dt in enumerate(dim_tags):
        if isinstance(dt, VectorArrayDimTag):
            if vector_dim is not None:
                raise ValueError("arg '%s' may only have one vector-tagged "
                        "argument dimension" % name)

            vector_dim = i

        elif isinstance(dt, FixedStrideArrayDimTag):
            fixed_stride_dim_tags[dt.target_axis].append(i)

        elif isinstance(dt, ComputedStrideArrayDimTag):
            if dt.order in "cC":
                computed_stride_dim_tags[dt.target_axis].insert(0, i)
            elif dt.order in "fF":
                computed_stride_dim_tags[dt.target_axis].append(i)
            else:
                raise ValueError("invalid value '%s' for "
                        "ComputedStrideArrayDimTag.order" % dt.order)

        elif isinstance(dt, SeparateArrayArrayDimTag):
            pass

        else:
            raise ValueError("invalid array dim tag")

    # }}}

    # {{{ convert computed to fixed stride dim tags

    new_dim_tags = dim_tags[:]

    for target_axis in range(num_target_axes):
        if (computed_stride_dim_tags[target_axis]
                and fixed_stride_dim_tags[target_axis]):
            error_msg = "computed and fixed stride arg dim tags may " \
                    "not be mixed for argument '%s'" % name

            if num_target_axes > 1:
                error_msg += " (target axis %d)" % target_axis

            raise ValueError(error_msg)

        stride_so_far = 1

        if fixed_stride_dim_tags[target_axis]:
            for i in fixed_stride_dim_tags[target_axis]:
                dt = dim_tags[i]
                new_dim_tags[i] = dt
        else:
            for i in computed_stride_dim_tags[target_axis]:
                dt = dim_tags[i]
                new_dim_tags[i] = FixedStrideArrayDimTag(stride_so_far)

                if shape is None:
                    # unable to normalize without known shape
                    return None

                stride_so_far *= shape[i]

                if dt.pad_to is not None:
                    from pytools import div_ceil
                    stride_so_far = (
                            div_ceil(stride_so_far, dt.pad_to)
                            * stride_so_far)

    # }}}

    return new_dim_tags

# }}}


# {{{ array base class (for arguments and temporary arrays)

def _pymbolic_parse_if_necessary(x):
    if isinstance(x, str):
        from pymbolic import parse
        return parse(x)
    else:
        return x


def _parse_shape_or_strides(x):
    import loopy as lp
    if x == "auto":
        from warnings import warn
        warn("use of 'auto' as a shape or stride won't work "
                "any more--use loopy.auto instead",
                stacklevel=3)
    x = _pymbolic_parse_if_necessary(x)
    if isinstance(x, lp.auto):
        return x
    if not isinstance(x, tuple):
        assert x is not lp.auto
        x = (x,)

    return tuple(_pymbolic_parse_if_necessary(xi) for xi in x)


class ArrayBase(Record):
    """
    .. attribute :: name

    .. attribute :: dtype

    .. attribute :: shape

    .. attribute:: dim_tags

        a list of :class:`ArrayDimImplementationTag` instances.
        or a list of strings that :func:`parse_array_dim_tag` understands,
        or a comma-separated string of such tags.

    .. attribute:: offset

    """

    # Note that order may also wind up in attributes, if the
    # number of dimensions has not yet been determined.

    def __init__(self, name, dtype=None, shape=None, dim_tags=None, offset=0,
            strides=None, order=None, **kwargs):
        """
        All of the following are optional. Specify either strides or shape.

        :arg name: May contain multiple names separated by
            commas, in which case multiple arguments,
            each with identical properties, are created
            for each name.
        :arg dtype: the :class:`numpy.dtype` of the array.
            If this is *None*, :mod:`loopy` will try to continue
            without knowing the type of this array.

            Note that some operations, such as :func:`loopy.add_padding`
            will not work without the *dtype*.

            :class:`loopy.CompiledKernel` will automatically compile a kernel
            with the right dtype when called with a concrete array on a kernel
            with argument whose *dtype* is *None*.
        :arg shape: May be one of the following:

            * *None*. In this case, no shape is intended to be specified,
              only the strides will be used to access the array. Bounds checking
              will not be performed.

            * :class:`loopy.auto`. The shape will be determined by finding the
              access footprint.

            * a tuple like like :attr:`numpy.ndarray.shape`.

              Each entry of the tuple is also allowed to be a :mod:`pymbolic`
              expression involving kernel parameters, or a (potentially-comma
              separated) or a string that can be parsed to such an expression.

            * A string which can be parsed into the previous form.

        :arg dim_tags: A comma-separated list of tags as understood by
            :func:`parse_array_dim_tag`.

        :arg strides: May be one of the following:

            * None

            * :class:`loopy.auto`. The strides will be determined by *order*
              and the access footprint.

            * a tuple like like :attr:`numpy.ndarray.shape`.

              Each entry of the tuple is also allowed to be a :mod:`pymbolic`
              expression involving kernel parameters, or a (potentially-comma
              separated) or a string that can be parsed to such an expression.

            * A string which can be parsed into the previous form.

        :arg order: "F" or "C" for C (row major) or Fortran
            (column major). Defaults to the *default_order* argument
            passed to :func:`loopy.make_kernel`.
        :arg offset: Offset from the beginning of the buffer to the point from
            which the strides are counted. May be one of

            * 0
            * a string (that is interpreted as an argument name).
            * :class:`loopy.auto`, in which case an offset argument
              is added automatically, immediately following this argument.
              :class:`loopy.CompiledKernel` is even smarter in its treatment of
              this case and will compile custom versions of the kernel based on
              whether the passed arrays have offsets or not.
        """

        import loopy as lp

        if dtype is not None and dtype is not lp.auto:
            dtype = np.dtype(dtype)

        strides_known = strides is not None and strides is not lp.auto
        shape_known = shape is not None and shape is not lp.auto

        if strides_known:
            strides = _parse_shape_or_strides(strides)

        if shape_known:
            shape = _parse_shape_or_strides(shape)

        # {{{ convert strides to dim_tags (Note: strides override order)

        if dim_tags is not None and strides_known:
            raise TypeError("may not specify both strides and dim_tags")

        if dim_tags is None and strides_known:
            dim_tags = [FixedStrideArrayDimTag(s) for s in strides]
            strides = None

        # }}}

        # {{{ determine number of user axes

        num_user_axes = None
        if shape_known:
            num_user_axes = len(shape)
        if dim_tags is not None:
            new_num_user_axes = len(dim_tags)

            if num_user_axes is None:
                num_user_axes = new_num_user_axes
            else:
                if new_num_user_axes != num_user_axes:
                    raise ValueError("contradictory values for number of dimensions "
                            "from shape, strides, or dim_tags")

            del new_num_user_axes

        # }}}

        # {{{ convert order to dim_tags

        if dim_tags is None and num_user_axes is not None and order is not None:
            dim_tags = num_user_axes*[order]
            order = None

        # }}}

        if dim_tags is not None:
            dim_tags = parse_array_dim_tags(dim_tags)

            # {{{ find number of target axes

            target_axes = set()
            for dt in dim_tags:
                if isinstance(dt, _StrideArrayDimTagBase):
                    target_axes.add(dt.target_axis)

            if target_axes != set(xrange(len(target_axes))):
                raise ValueError("target axes for variable '%s' are non-"
                        "contiguous" % self.name)

            num_target_axes = len(target_axes)
            del target_axes

            # }}}

            if not (self.min_target_axes <= num_target_axes <= self.max_target_axes):
                raise ValueError("%s only supports between %d and %d target axes "
                        "('%s' has %d)" % (type(self).__name__, self.min_target_axes,
                            self.max_target_axes, self.name, num_target_axes))

            new_dim_tags = convert_computed_to_fixed_dim_tags(
                    name, num_user_axes, num_target_axes,
                    shape, dim_tags)

            if new_dim_tags is not None:
                # successfully normalized
                dim_tags = new_dim_tags
                del new_dim_tags

        if dim_tags is not None:
            # for hashability
            dim_tags = tuple(dim_tags)
            order = None

        Record.__init__(self,
                name=name,
                dtype=dtype,
                shape=shape,
                dim_tags=dim_tags,
                offset=offset,
                order=order,
                strides=strides,
                **kwargs)

    def __str__(self):
        import loopy as lp

        info_entries = [type(self).__name__, str(self.dtype)]

        if self.shape is None:
            pass
        elif self.shape is lp.auto:
            info_entries.append("shape: auto")
        else:
            info_entries.append("shape: (%s)"
                    % ",".join(str(i) for i in self.shape))

        if self.dim_tags is not None:
            info_entries.append("dim_tags: (%s)"
                    % ",".join(str(i) for i in self.dim_tags))

        if self.offset:
            info_entries.append("offset: %s" % self.offset)

        return "%s: %s" % (self.name, ", ".join(info_entries))

    def __repr__(self):
        return "<%s>" % self.__str__()

    @property
    @memoize_method
    def numpy_strides(self):
        return tuple(self.dtype.itemsize*s for s in self.strides)

    def num_target_axes(self):
        target_axes = set()
        for dt in self.dim_tags:
            if isinstance(dt, _StrideArrayDimTagBase):
                target_axes.add(dt.target_axis)

        return len(target_axes)

    def num_user_axes(self, require_answer=True):
        if self.shape is not None:
            return len(self.shape)
        if self.dim_tags is not None:
            return len(self.dim_tags)
        if require_answer:
            raise RuntimeError("number of user axes of array '%s' cannot be found"
                    % self.name)
        else:
            return None

    def map_exprs(self, mapper):
        """Return a copy of self with all expressions replaced with what *mapper*
        transformed them into.
        """
        kwargs = {}
        import loopy as lp

        if self.shape is not None and self.shape is not lp.auto:
            kwargs["shape"] = tuple(mapper(s) for s in self.shape)

        if self.dim_tags is not None:
            kwargs["dim_tags"] = [dt.map_expr(mapper) for dt in self.dim_tags]

        # offset is not an expression, do not map.

        return self.copy(**kwargs)

    def vector_size(self):
        """Return the size of the vector type used for the array
        divided by the basic data type.

        Note: For 3-vectors, this will be 4.
        """

        for i, dim_tag in enumerate(self.dim_tags):
            if isinstance(dim_tag, VectorArrayDimTag):
                shape_i = self.shape[i]
                if not isinstance(shape_i, int):
                    raise RuntimeError("shape of '%s' has non-constant "
                            "integer axis %d (0-based)" % (
                                self.name, user_axis))

                vec_dtype = cl.array.vec.types[self.dtype, shape_i]

                return int(vec_dtype.itemsize) // int(self.dtype.itemsize)

        return 1

    def decl_info(self, is_written, index_dtype):
        """Return a list of tuples ``(cgen_decl, arg_info)``, where
        *cgen_decl* is a :mod:`cgen` argument declarations, *arg_info*
        is a :class:`CLArgumentInfo` instance.
        """

        from loopy.codegen import CLArgumentInfo

        vector_size = self.vector_size()

        def gen_decls(name_suffix, shape, strides, dtype, user_index):
            if dtype is None:
                dtype = self.dtype

            user_axis = len(user_index)

            num_user_axes = self.num_user_axes(require_answer=False)

            if num_user_axes is None or user_axis >= num_user_axes:
                # implemented by various argument types
                full_name = self.name + name_suffix

                yield (self.get_arg_decl(name_suffix, shape, dtype, is_written),
                        CLArgumentInfo(
                            name=full_name,
                            base_name=self.name,
                            dtype=dtype,
                            shape=shape,
                            strides=strides,
                            offset_for_name=None))

                if self.offset:
                    from cgen import Const, POD
                    offset_name = full_name+"_offset"
                    yield (Const(POD(index_dtype, offset_name)),
                            CLArgumentInfo(
                                name=offset_name,
                                base_name=None,
                                dtype=index_dtype,
                                shape=None,
                                strides=None,
                                offset_for_name=full_name))

                return

            dim_tag = self.dim_tags[user_axis]

            if isinstance(dim_tag, FixedStrideArrayDimTag):
                if self.shape is None:
                    new_shape = shape + (None,)
                else:
                    new_shape = shape + (self.shape[user_axis],)

                for res in gen_decls(name_suffix, new_shape,
                        strides + (dim_tag.stride // vector_size,),
                        dtype, user_index + (None,)):
                    yield res

            elif isinstance(dim_tag, SeparateArrayArrayDimTag):
                shape_i = self.shape[user_axis]
                if not isinstance(shape_i, int):
                    raise RuntimeError("shape of '%s' has non-constant "
                            "integer axis %d (0-based)" % (
                                self.name, user_axis))

                for i in xrange(shape_i):
                    for res in gen_decls(name_suffix + "_s%d" % i,
                            shape, dtype,
                            user_index + (i,)):
                        yield res

            elif isinstance(dim_tag, VectorArrayDimTag):
                shape_i = self.shape[user_axis]
                if not isinstance(shape_i, int):
                    raise RuntimeError("shape of '%s' has non-constant "
                            "integer axis %d (0-based)" % (
                                self.name, user_axis))

                for res in gen_decls(name_suffix, shape, strides,
                        cl.array.vec.types[dtype, shape_i],
                        user_index + (None,)):
                    yield res

            else:
                raise RuntimeError("unsupported array dim implementation tag '%s' "
                        "in array '%s'" % (dim_tag, self.name))

        for res in gen_decls("", (), (), self.dtype, ()):
            yield res

# }}}


# {{{ access code generation

class AccessInfo(Record):
    """
    :ivar array_suffix:
    :ivar vector_index:
    :ivar subscripts: List of expressions, one for each target axis
    """


def get_access_info(ary, index, eval_expr):
    """
    :arg ary: an object of type :class:`ArrayBase`
    :arg index: a tuple of indices representing a subscript into ary
    """
    if not isinstance(index, tuple):
        index = (index,)

    if ary.shape is None:
        return AccessInfo(subscripts=index, vector_index=0)

    if len(ary.shape) != len(index):
        raise RuntimeError("subscript to '%s[%s]' has the wrong "
                "number of indices (got: %d, expected: %d)" % (
                    ary.name, index, len(index), len(ary.shape)))

    num_target_axes = ary.num_target_axes()

    array_suffix = ""
    vector_index = None
    subscripts = [0] * num_target_axes

    vector_size = ary.vector_size()

    for i, (idx, dim_tag) in enumerate(zip(index, ary.dim_tags)):
        if isinstance(dim_tag, FixedStrideArrayDimTag):
            if isinstance(dim_tag.stride, int):
                if not dim_tag.stride % vector_size == 0:
                    raise RuntimeError("stride of axis %d of array '%s' "
                            "is not a multiple of the vector axis"
                            % (i, ary.name))

            subscripts[dim_tag.target_axis] += (dim_tag.stride // vector_size)*idx

        elif isinstance(dim_tag, SeparateArrayArrayDimTag):
            idx = eval_expr(idx)
            if not isinstance(idx, int):
                raise RuntimeError("subscript '%s[%s]' has non-constant "
                        "index for separate-array axis %d (0-based)" % (
                            ary.name, index, i))
            array_suffix += "_s%d" % idx

        elif isinstance(dim_tag, VectorArrayDimTag):
            idx = eval_expr(idx)

            if not isinstance(idx, int):
                raise RuntimeError("subscript '%s[%s]' has non-constant "
                        "index for separate-array axis %d (0-based)" % (
                            ary.name, index, i))
            assert vector_index is None
            vector_index = idx

        else:
            raise RuntimeError("unsupported array dim implementation tag '%s' "
                    "in array '%s'" % (dim_tag, ary.name))

    from pymbolic import var
    import loopy as lp
    if ary.offset:
        if num_target_axes > 1:
            raise NotImplementedError("offsets for multiple image axes")

        offset_name = ary.offset
        if offset_name is lp.auto:
            offset_name = ary.name+array_suffix+"_offset"

        subscripts[0] = var(offset_name) + subscripts[0]

    return AccessInfo(
            array_suffix=array_suffix,
            vector_index=vector_index,
            subscripts=subscripts)

# }}}

# vim: fdm=marker
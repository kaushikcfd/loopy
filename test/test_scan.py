from __future__ import division, absolute_import, print_function

__copyright__ = """
Copyright (C) 2012 Andreas Kloeckner
Copyright (C) 2016 Matt Wala
"""

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

import sys
import numpy as np
import loopy as lp
import pyopencl as cl
import pyopencl.clmath  # noqa
import pyopencl.clrandom  # noqa
import pytest

import logging
logger = logging.getLogger(__name__)

try:
    import faulthandler
except ImportError:
    pass
else:
    faulthandler.enable()

from pyopencl.tools import pytest_generate_tests_for_pyopencl \
        as pytest_generate_tests

__all__ = [
        "pytest_generate_tests",
        "cl"  # 'cl.create_some_context'
        ]


# More things to test.
# - test that dummy inames are removed
# - nested sequential/parallel scan
# - scan(a) + scan(b)
# - global parallel scan
# - base_exec_iname different bounds from sweep iname

# TO DO:
# segmented<sum>(...) syntax


@pytest.mark.parametrize("n", [1, 2, 3, 16])
@pytest.mark.parametrize("stride", [1, 2])
def test_sequential_scan(ctx_factory, n, stride):
    ctx = ctx_factory()
    queue = cl.CommandQueue(ctx)

    knl = lp.make_kernel(
        "[n] -> {[i,j]: 0<=i<n and 0<=j<=%d*i}" % stride,
        "a[i] = sum(j, j**2) {id=scan}"
        )

    knl = lp.fix_parameters(knl, n=n)
    knl = lp.realize_reduction(knl, force_scan=True)
    evt, (a,) = knl(queue)

    assert (a.get() == np.cumsum(np.arange(stride*n)**2)[::stride]).all()


def test_scan_with_different_lower_bound_from_sweep(ctx_factory):
    ctx = ctx_factory()
    queue = cl.CommandQueue(ctx)

    knl = lp.make_kernel(
        "[n, lbound] -> {[i,j]: 0<=i<n and lbound<=j<=2*i+lbound}",
        "a[i] = sum(j, j**2) {id=scan}"
        )

    lbound = 7
    n = 10

    knl = lp.fix_parameters(knl, lbound=lbound)
    knl = lp.realize_reduction(knl, force_scan=True)
    print(knl)
    evt, (a,) = knl(queue, n=n)

    assert (a.get() == np.cumsum(np.arange(lbound, 2*n+lbound)**2)[::2]).all()


def test_automatic_scan_detection():
    pass


def test_dependent_domain_scan(ctx_factory):
    ctx = ctx_factory()
    queue = cl.CommandQueue(ctx)

    knl = lp.make_kernel(
        [
            "[n] -> {[i]: 0<=i<n}",
            "{[j]: 0<=j<=2*i}"
        ],
        """
        a[i] = sum(j, j**2) {id=scan}
        """
        )
    knl = lp.realize_reduction(knl, force_scan=True)
    evt, (a,) = knl(queue, n=100)

    assert (a.get() == np.cumsum(np.arange(200)**2)[::2]).all()


"""
def test_nested_scan():
    knl = lp.make_kernel(
        [
            "[n] -> {[i]: 0 <= i < n}",
            "{[j]: 0 <= j <= i}",
            "{[k]: 0 <= j <= k}"
        ],
        "a[i] = sum(j, sum(k, k))")
"""


def test_scan_unsupported_stride():
    knl = lp.make_kernel(
        "{[i,j]: 0<=i<100 and 1<=j<=2*i}",
        """
        a[i] = sum(j, j**2) {id=scan}
        """
        )

    with pytest.raises(lp.diagnostic.ReductionIsNotTriangularError):
        knl = lp.realize_reduction(knl, force_scan=True)


@pytest.mark.parametrize("n", [1, 2, 3, 16, 17])
def test_local_parallel_scan(ctx_factory, n):
    ctx = ctx_factory()
    queue = cl.CommandQueue(ctx)

    knl = lp.make_kernel(
        "[n] -> {[i,j]: 0<=i<n and 0<=j<=i}",
        """
        out[i] = sum(j, a[j]**2)
        """,
        "..."
        )

    knl = lp.fix_parameters(knl, n=16)
    knl = lp.tag_inames(knl, dict(i="l.0"))
    knl = lp.realize_reduction(knl, force_scan=True)

    print(knl)

    knl = lp.realize_reduction(knl)

    knl = lp.add_dtypes(knl, dict(a=int))
    c = lp.generate_code_v2(knl)

    print(c.device_code())

    evt, (a,) = knl(queue, a=np.arange(16))

    print(a)

    assert (a == np.cumsum(np.arange(16)**2)).all()


@pytest.mark.parametrize("sweep_iname_tag", ["for", "l.1"])
def test_scan_with_outer_parallel_iname(ctx_factory, sweep_iname_tag):
    ctx = ctx_factory()
    queue = cl.CommandQueue(ctx)

    knl = lp.make_kernel(
        [
            "{[k]: 0<=k<=1}",
            "[n] -> {[i,j]: 0<=i<n and 0<=j<=i}"
        ],
        "out[k,i] = k + sum(j, j**2)"
        )

    knl = lp.tag_inames(knl, dict(k="l.0", i=sweep_iname_tag))
    n = 10
    knl = lp.fix_parameters(knl, n=n)
    knl = lp.realize_reduction(knl, force_scan=True)

    evt, (out,) = knl(queue)

    inner = np.cumsum(np.arange(n)**2)

    assert (out.get() == np.array([inner, 1 + inner])).all()


def test_scan_data_types():
    # TODO
    pass


def test_scan_library():
    # TODO
    pass


@pytest.mark.parametrize("i_tag", ["for", "l.0"])
def test_argmax(ctx_factory, i_tag):
    logging.basicConfig(level=logging.INFO)

    dtype = np.dtype(np.float32)
    ctx = ctx_factory()
    queue = cl.CommandQueue(ctx)

    n = 128

    knl = lp.make_kernel(
            "{[i,j]: 0<=i<%d and 0<=j<=i}" % n,
            """
            max_vals[i], max_indices[i] = argmax(j, fabs(a[j]), j)
            """)

    knl = lp.tag_inames(knl, dict(i=i_tag))
    knl = lp.add_and_infer_dtypes(knl, {"a": np.float32})
    knl = lp.realize_reduction(knl, force_scan=True)

    print(knl)

    a = np.random.randn(n).astype(dtype)
    evt, (max_indices, max_vals) = knl(queue, a=a, out_host=True)

    assert (max_vals == [np.max(np.abs(a)[0:i+1]) for i in range(n)]).all()
    assert (max_indices == [np.argmax(np.abs(a[0:i+1])) for i in range(n)]).all()


@pytest.mark.parametrize("n, segment_boundaries_indices", (
    (1, (0,)),
    (2, (0,)),
    (2, (0, 1)),
    (3, (0,)),
    (3, (0, 1)),
    (3, (0, 2)),
    (3, (0, 1, 2)),
    (16, (0, 5)),
    ))
@pytest.mark.parametrize("iname_tag", ("for", "l.0"))
def test_segmented_scan(ctx_getter, n, segment_boundaries_indices, iname_tag):
    ctx = ctx_getter()
    queue = cl.CommandQueue(ctx)

    arr = np.ones(n, dtype=np.float32)
    segment_boundaries = np.zeros(n, dtype=np.int32)
    segment_boundaries[(segment_boundaries_indices,)] = 1

    knl = lp.make_kernel(
        "{[i,j]: 0<=i<n and 0<=j<=i}",
        "out[i], <>_ = segmented_sum(j, arr[j], segflag[j])",
        [
            lp.GlobalArg("arr", np.float32, shape=("n",)),
            lp.GlobalArg("segflag", np.int32, shape=("n",)),
            "..."
        ])

    knl = lp.fix_parameters(knl, n=n)
    knl = lp.tag_inames(knl, dict(i=iname_tag))
    knl = lp.realize_reduction(knl, force_scan=True)

    (evt, (out,)) = knl(queue, arr=arr, segflag=segment_boundaries)

    class SegmentGrouper(object):

        def __init__(self):
            self.seg_idx = 0
            self.idx = 0

        def __call__(self, key):
            if self.idx in segment_boundaries_indices:
                self.seg_idx += 1
            self.idx += 1
            return self.seg_idx

    from itertools import groupby

    expected = [np.cumsum(list(group))
            for _, group in groupby(arr, SegmentGrouper())]
    actual = [np.array(list(group))
            for _, group in groupby(out, SegmentGrouper())]

    assert len(expected) == len(actual) == len(segment_boundaries_indices)
    assert [(e == a).all() for e, a in zip(expected, actual)]


if __name__ == "__main__":
    if len(sys.argv) > 1:
        exec(sys.argv[1])
    else:
        from py.test.cmdline import main
        main([__file__])

# vim: foldmethod=marker

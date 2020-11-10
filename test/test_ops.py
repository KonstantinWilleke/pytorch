from functools import partial, wraps

import torch

from torch.testing import floating_and_complex_types
from torch.testing._internal.common_utils import \
    (TestCase, run_tests, IS_SANDCASTLE)
from torch.testing._internal.common_methods_invocations import \
    (op_db)
from torch.testing._internal.common_device_type import \
    (instantiate_device_type_tests, ops, dtypes, onlyOnCPUAndCUDA, skipCUDAIfRocm)
from torch.testing._internal.common_jit import JitCommonTestCase, check_against_reference
from torch.autograd.gradcheck import gradcheck, gradgradcheck

from torch.testing._internal.jit_metaprogramming_utils import create_script_fn, create_traced_fn, \
    check_alias_annotation
from torch.testing._internal.jit_utils import disable_autodiff_subgraph_inlining


# Tests that apply to all operators

class TestOpInfo(TestCase):
    exact_dtype = True

    # Verifies that ops have their unsupported dtypes
    #   registered correctly by testing that each claimed unsupported dtype
    #   throws a runtime error
    @skipCUDAIfRocm
    @onlyOnCPUAndCUDA
    @ops(op_db, unsupported_dtypes_only=True)
    def test_unsupported_dtypes(self, device, dtype, op):
        samples = op.sample_inputs(device, dtype)
        if len(samples) == 0:
            self.skipTest("Skipped! No sample inputs!")

        # NOTE: only tests on first sample
        sample = samples[0]
        with self.assertRaises(RuntimeError):
            op(sample.input, *sample.args, **sample.kwargs)

    # Verifies that ops have their supported dtypes
    #   registered correctly by testing that each claimed supported dtype
    #   does NOT throw a runtime error
    @skipCUDAIfRocm
    @onlyOnCPUAndCUDA
    @ops(op_db)
    def test_supported_dtypes(self, device, dtype, op):
        samples = op.sample_inputs(device, dtype)
        if len(samples) == 0:
            self.skipTest("Skipped! No sample inputs!")

        # NOTE: only tests on first sample
        sample = samples[0]
        op(sample.input, *sample.args, **sample.kwargs)


class TestGradients(TestCase):
    exact_dtype = True

    # Copies inputs to inplace operations to avoid inplace modifications
    #   to leaves requiring gradient
    def _get_safe_inplace(self, inplace_variant):
        @wraps(inplace_variant)
        def _fn(t, *args, **kwargs):
            return inplace_variant(t.clone(), *args, **kwargs)

        return _fn

    def _check_helper(self, device, dtype, op, variant, check):
        if variant is None:
            self.skipTest("Skipped! Variant not implemented.")
        if not op.supports_dtype(dtype, torch.device(device).type):
            self.skipTest(f"Skipped! {op.name} does not support dtype {str(dtype)}")

        samples = op.sample_inputs(device, dtype, requires_grad=True)
        for sample in samples:
            partial_fn = partial(variant, **sample.kwargs)
            if check == 'gradcheck':
                self.assertTrue(gradcheck(partial_fn, (sample.input,) + sample.args,
                                          check_grad_dtypes=True))
            elif check == 'gradgradcheck':
                self.assertTrue(gradgradcheck(partial_fn, (sample.input,) + sample.args,
                                              gen_non_contig_grad_outputs=False,
                                              check_grad_dtypes=True))
                self.assertTrue(gradgradcheck(partial_fn, (sample.input,) + sample.args,
                                              gen_non_contig_grad_outputs=True,
                                              check_grad_dtypes=True))
            else:
                self.assertTrue(False, msg="Unknown check requested!")

    def _grad_test_helper(self, device, dtype, op, variant):
        return self._check_helper(device, dtype, op, variant, 'gradcheck')

    def _gradgrad_test_helper(self, device, dtype, op, variant):
        return self._check_helper(device, dtype, op, variant, 'gradgradcheck')

    # Tests that gradients are computed correctly
    @dtypes(torch.double, torch.cdouble)
    @ops(op_db)
    def test_fn_grad(self, device, dtype, op):
        self._grad_test_helper(device, dtype, op, op.get_op())

    @dtypes(torch.double, torch.cdouble)
    @ops(op_db)
    def test_method_grad(self, device, dtype, op):
        self._grad_test_helper(device, dtype, op, op.get_method())

    @dtypes(torch.double, torch.cdouble)
    @ops(op_db)
    def test_inplace_grad(self, device, dtype, op):
        if not op.test_inplace_grad:
            self.skipTest("Skipped! Inplace gradcheck marked to skip.")
        self._grad_test_helper(device, dtype, op, self._get_safe_inplace(op.get_inplace()))

    # Test that gradients of gradients are computed correctly
    @dtypes(torch.double, torch.cdouble)
    @ops(op_db)
    def test_fn_gradgrad(self, device, dtype, op):
        self._gradgrad_test_helper(device, dtype, op, op.get_op())

    @dtypes(torch.double, torch.cdouble)
    @ops(op_db)
    def test_method_gradgrad(self, device, dtype, op):
        self._gradgrad_test_helper(device, dtype, op, op.get_method())

    @dtypes(torch.double, torch.cdouble)
    @ops(op_db)
    def test_inplace_gradgrad(self, device, dtype, op):
        if not op.test_inplace_grad:
            self.skipTest("Skipped! Inplace gradgradcheck marked to skip.")
        self._gradgrad_test_helper(device, dtype, op, self._get_safe_inplace(op.get_inplace()))


class TestCommon(JitCommonTestCase):
    exact_dtype = True

    # Compares variant's backward
    # NOTE: verifies it fails when the forward fails
    def check_variant_backward(self, input, forward_result, expected_grad, expected_exception):
        variant_exception_during_backwards = False
        try:
            forward_result.sum().backward()
            variant_grad = input.grad
            input.grad = None
        except Exception as e:
            if not expected_exception:
                self.fail("Unexpected exception during backwards!")
            variant_exception_during_backwards = True

        if expected_exception != variant_exception_during_backwards:
            self.fail("Unexpected success during backwards!")

        if not expected_exception:
            self.assertEqual(variant_grad, expected_grad)

    # Tests that the forward and backward passes of operations produce the
    #   same values for the cross-product of op variants (function, method, inplace)
    #   and runtimes (eager, traced, scripted).
    # TODO WARNING: inplace x {traced, scripted} not currently tested
    @ops(op_db)
    def test_variant_consistency(self, device, dtype, op):
        samples = op.sample_inputs(device, dtype, requires_grad=True)
        if len(samples) == 0:
            self.skipTest("Skipped! No sample inputs!")

        for sample in samples:
            # Computes expected forward
            expected_forward = op(sample.input, *sample.args, **sample.kwargs)

            # Computes expected backward
            # NOTE: backward may fail for some dtypes
            exception_during_backwards = False
            expected_grad = None
            try:
                expected_forward.sum().backward()
                expected_grad = sample.input.grad
                sample.input.grad = None
            except Exception as e:
                exception_during_backwards = True

            # Acquires variants to test
            method = op.get_method()
            inplace = op.get_inplace()
            variants = (v for v in (method, inplace) if v is not None)

            # Test eager consistency
            for variant in variants:
                # Verifies that inplace operations that promote int->float fail
                # on tensors with integer dtypes.
                if (variant is inplace and op.promotes_integers_to_float and
                        dtype in (torch.bool, torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64)):
                    try:
                        variant_forward = variant(sample.input.clone(), *sample.args, **sample.kwargs)
                    except Exception as e:
                        continue
                    self.fail("Inplace operation on integer tensor that should be promoted to float didn't fail!")

                # Compares variant's forward
                variant_forward = variant(sample.input.clone(), *sample.args, **sample.kwargs)
                self.assertEqual(variant_forward, expected_forward)

                # Compares variant's backward
                if variant is not inplace and op.test_inplace_grad:
                    self.check_variant_backward(sample.input, variant_forward,
                                                expected_grad, exception_during_backwards)

            # Adds function variant to variant list
            # TODO: inplace tests currently fail
            # variants = (v for v in (op, method, inplace) if v is not None)
            variants = (v for v in (op, method) if v is not None)

            # Test traced and scripted consistency
            for variant in variants:
                # Create accessor for script function variant
                if variant is op:
                    name = op.name
                    func_type = 'function'
                elif variant is method:
                    name = op.name
                    func_type = 'method'
                else:  # variant is inplace
                    assert variant is inplace
                    name = op.name + "_"
                    func_type = 'inplace'

                with disable_autodiff_subgraph_inlining():
                    def fn(*inputs, **kwargs):
                        attr = getattr(inputs[0], name)
                        output = attr(*inputs[1:], **kwargs)
                        return op.output_func(output)

                    # Check scripted forward and grad
                    script_fn = create_script_fn(self, name, func_type, op.output_func)
                    check_against_reference(self, 
                                            script_fn,
                                            fn, 
                                            (sample.input,) + sample.args, 
                                            sample.kwargs, 
                                            no_grad=(dtype not in floating_and_complex_types()))

                    # Check traced forward and grad
                    traced_fn = create_traced_fn(self, variant)
                    check_against_reference(self, 
                                            traced_fn,
                                            fn, 
                                            (sample.input,) + sample.args, 
                                            sample.kwargs, 
                                            no_grad=(dtype not in floating_and_complex_types()))

                    # Check alias annotation schema for correctness (make sure inputs that aren't supposed to be modified aren't)
                    # Op-writer TODO: make sure op supports one of the operators in below list to ensure alias annotation is checked
                    if dtype in [torch.float32, torch.int32]:
                        check_alias_annotation(name, (sample.input,) + sample.args, sample.kwargs)

                    # Check autodiff of nodes for traced and scripted graphs
                    # only need to check once per sample 
                    if dtype is torch.float32:
                        if IS_SANDCASTLE:
                            nonfusible_nodes = op.autodiff_nonfusible_nodes + op.autodiff_fusible_nodes
                            fusible_nodes = []
                        else:
                            nonfusible_nodes = op.autodiff_nonfusible_nodes
                            fusible_nodes = op.autodiff_fusible_nodes

                        self.assertAutodiffNode(traced_fn.last_graph, op.is_autodiffed, nonfusible_nodes, fusible_nodes)
                        self.assertAutodiffNode(script_fn.last_graph, op.is_autodiffed, nonfusible_nodes, fusible_nodes)


    @ops(op_db)
    def test_out(self, device, dtype, op):
        if not op.supports_tensor_out:
            self.skipTest("Skipped! Operator %s does not support out=..." % op.name)

        samples = op.sample_inputs(device, dtype)
        if len(samples) == 0:
            self.skipTest("Skipped! No sample inputs!")

        # NOTE: only tests on first sample
        sample = samples[0]
        # call it normally to get the expected result
        expected = op(sample.input, *sample.args, **sample.kwargs)
        # call it with out=... and check we get the expected result
        out_kwargs = sample.kwargs.copy()
        out_kwargs['out'] = out = torch.empty_like(expected)
        op(sample.input, *sample.args, **out_kwargs)
        self.assertEqual(expected, out)


instantiate_device_type_tests(TestOpInfo, globals())
instantiate_device_type_tests(TestGradients, globals())
instantiate_device_type_tests(TestCommon, globals())

if __name__ == '__main__':
    run_tests()

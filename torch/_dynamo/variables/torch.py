import logging

import math
import re
import types
from collections import OrderedDict
from typing import Dict, List

import torch._C
import torch.fx
import torch.nn
import torch.onnx.operators
from torch._dynamo.side_effects import AttributeMutationNew, SideEffects
from torch._dynamo.utils import get_fake_value
from torch._dynamo.variables import DynamicShapeVariable
from torch._dynamo.variables.misc import NewCellVariable
from torch._guards import GuardsCheckpointState

from .. import config, variables
from ..allowed_functions import torch_get_name
from ..exc import unimplemented
from ..source import GetItemSource, NNModuleSource
from ..utils import (
    check_constant_args,
    check_unspec_python_args,
    HAS_NUMPY,
    istype,
    np,
    product,
    proxy_args_kwargs,
    specialize_args_kwargs,
    tensortype_to_dtype,
)
from .base import VariableTracker
from .lists import ListVariable, TupleVariable
from .misc import AutocastModeVariable, NullContextVariable
from .tensor import TensorWithTFOverrideVariable

log = logging.getLogger(__name__)

# TODO(voz): Maybe rename these later
tensor_dunder_fns = [
    torch.Tensor.__rmatmul__,
    torch.Tensor.__rmod__,
    torch.Tensor.__rpow__,
    torch.Tensor.__rsub__,
    torch._C._TensorBase.__radd__,
    torch._C._TensorBase.__rmul__,
    torch._C._TensorBase.__ror__,
    torch._C._TensorBase.__rxor__,
    torch._C._TensorBase.__rand__,
]

torch_special_class_types = (torch._C.Generator,)

REWRITE_OPS_TO_TENSOR_SIZE_METHOD = [
    torch.onnx.operators.shape_as_tensor,
    torch._shape_as_tensor,
]

constant_fold_functions = [
    torch._assert,
    torch._utils._get_device_index,
    torch.cuda.is_available,
    torch.device,
    torch.distributed.is_available,
    torch.finfo,
    torch.get_default_dtype,
    torch.iinfo,
    torch.is_floating_point,
    torch.nn.functional._Reduction.get_enum,
]
if torch.distributed.is_available():
    constant_fold_functions.append(torch.distributed.is_initialized)

# TODO(voz): perhaps a decorator? This is rather readable for now tho, and not a public API.
def remap_as_fn___radd__(*args):
    return torch._C._TensorBase.__radd__(*args)


def remap_as_fn___rmul__(*args):
    return torch._C._TensorBase.__rmul__(*args)


def remap_as_fn___ror__(*args):
    return torch._C._TensorBase.__ror__(*args)


def remap_as_fn___rxor__(*args):
    return torch._C._TensorBase.__rxor__(*args)


def remap_as_fn___rand__(*args):
    return torch._C._TensorBase.__rand__(*args)


tensor_dunder_fns_remap = {
    torch._C._TensorBase.__radd__: remap_as_fn___radd__,
    torch._C._TensorBase.__rmul__: remap_as_fn___rmul__,
    torch._C._TensorBase.__ror__: remap_as_fn___ror__,
    torch._C._TensorBase.__rxor__: remap_as_fn___rxor__,
    torch._C._TensorBase.__rand__: remap_as_fn___rand__,
}


try:
    # Wed need to monkeypatch transformers here, sadly.
    # TODO(voz): Upstream to transformers lib
    import transformers

    def _dynamo_overriden_transformers_eq(self, other):
        if not hasattr(other, "__dict__"):
            return False
        return self.__dict__ == other.__dict__

    transformers.configuration_utils.PretrainedConfig.__eq__ = (
        _dynamo_overriden_transformers_eq
    )
except ImportError:
    pass


class TorchVariable(VariableTracker):
    """Points to a module or method in torch.*"""

    def __init__(self, value, **kwargs):
        super(TorchVariable, self).__init__(**kwargs)

        if value in tensor_dunder_fns_remap:
            value = tensor_dunder_fns_remap[value]
        self.value = value

        # the remainder of this is just optional debug checks
        try:
            self_should_be_none = getattr(self.value, "__self__", None)
        except RuntimeError as e:
            assert "No such operator" in str(e), str(e)
            self_should_be_none = None

        # assert "_ntuple.<locals>.parse" not in str(value)

        if self_should_be_none is None:
            pass
        elif isinstance(self_should_be_none, types.ModuleType):
            # weird ones like torch.nn.functional.avg_pool2d have __self__
            name = self_should_be_none.__name__
            assert re.match(r"^(torch|math)([.]|$)", name), f"__self__ set to {name}"
        elif isinstance(
            self_should_be_none, type(torch._C._get_tracing_state.__self__)
        ):
            # some _C functions have __self__ as a null capsule
            pass
        elif isinstance(self_should_be_none, torch_special_class_types):
            pass
        else:
            raise AssertionError(f"{value} found with __self__ set")

    def __repr__(self):
        return f"TorchVariable({self.value})"

    def unique_var_name(self):
        name = torch_get_name(self.value, f"allowed_fn_{id(self.value)}")
        return "__" + re.sub(r"[^a-zA-Z0-9_]+", "_", name)

    def reconstruct(self, codegen):
        return codegen.setup_globally_cached(self.unique_var_name(), self.value)

    def as_proxy(self):
        return self.value

    def python_type(self):
        if isinstance(self.value, (torch.Tensor, torch.nn.Module)):
            return type(self.value)
        return super().python_type()

    def as_python_constant(self):
        return self.value

    def can_constant_fold_through(self):
        if self.value in constant_fold_functions:
            return True
        return getattr(self.value, "__module__", None) == "math"

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        from . import (
            ConstantVariable,
            DynamicShapeVariable,
            GradModeVariable,
            TensorVariable,
            UserDefinedObjectVariable,
        )

        from .builder import wrap_fx_proxy

        constant_args = check_constant_args(args, kwargs)
        unspec_python_args = check_unspec_python_args(args, kwargs)
        options = VariableTracker.propagate(self, args, kwargs.values())

        if self.value in config.constant_functions:
            assert not args and not kwargs
            return ConstantVariable(config.constant_functions[self.value], **options)
        elif self.can_constant_fold_through() and (constant_args or unspec_python_args):
            args, kwargs = specialize_args_kwargs(tx, args, kwargs)
            # constant fold
            return ConstantVariable(
                self.as_python_constant()(
                    *[x.as_python_constant() for x in args],
                    **{k: v.as_python_constant() for k, v in kwargs.items()},
                ),
                **options,
            )
        elif istype(self.value, type) and issubclass(self.value, torch.nn.Module):
            if self.value is torch.nn.Softmax:
                return self._call_softmax(tx, args, kwargs, options)
            if self.value is torch.nn.CrossEntropyLoss:
                return self._call_cross_entropy_loss(tx, args, kwargs, options)
            else:
                unimplemented(f"construct nn.Module: {self.value.__name__}")
        elif self.value in (torch.is_tensor, torch.overrides.is_tensor_like):
            assert len(args) == 1
            if isinstance(args[0], TensorVariable) or (
                self.value is torch.overrides.is_tensor_like
                and isinstance(args[0], UserDefinedObjectVariable)
                and hasattr(args[0].value, "__torch_function__")
            ):
                return ConstantVariable(True, **options)
            else:
                return ConstantVariable(False, **options)
        elif (
            self.value
            in (
                torch.is_floating_point,
                torch.is_complex,
            )
            and isinstance(args[0], TensorVariable)
            and args[0].dtype is not None
        ):
            if self.value is torch.is_floating_point:
                return ConstantVariable(args[0].dtype.is_floating_point, **options)
            elif self.value is torch.is_complex:
                return ConstantVariable(args[0].dtype.is_complex, **options)
            else:
                raise AssertionError()
        elif (
            self.value is torch.numel
            and isinstance(args[0], TensorVariable)
            and args[0].size is not None
        ):
            return ConstantVariable(product(args[0].size), **options)
        elif self.value in REWRITE_OPS_TO_TENSOR_SIZE_METHOD:
            assert len(args) == 1
            assert isinstance(args[0], TensorVariable)
            return args[0].call_method(tx, "size", [], {})
        elif self.value in (
            torch.nn.modules.utils._single,
            torch.nn.modules.utils._pair,
            torch.nn.modules.utils._triple,
            torch.nn.modules.utils._quadruple,
            torch.nn.modules.utils._ntuple,
        ):
            return self._call_ntuple(tx, args, kwargs, options)
        elif self.value is torch.no_grad:
            return GradModeVariable.create(tx, False, **options)
        elif self.value is torch.enable_grad:
            return GradModeVariable.create(tx, True, **options)
        elif self.value is torch.set_grad_enabled and len(args) == 1:
            return GradModeVariable.create(tx, args[0].as_python_constant(), **options)
        elif self.value is torch.is_grad_enabled:
            assert not (args or kwargs)
            return ConstantVariable(torch.is_grad_enabled(), **options).add_guards(
                GradModeVariable._guards_singleton
            )
        elif not config.dynamic_shapes and self.is_dynamic_shapes(args, kwargs):
            unimplemented(f"dynamic shapes: {self.value.__name__}")
        elif len(args) > 0 and isinstance(args[0], TensorWithTFOverrideVariable):
            # This code block implements inlining the __torch_function__
            # override of a tensor.

            tensor_with_tf_override = args[0]

            # TODO(future PR): make this implement the full __torch_function__ API
            # instead of assuming the relevant override is in the first argument.
            args[0] = args[0].tensor_variable

            unwrapped = TensorWithTFOverrideVariable.inline_torch_function_unwrapped(
                tx,
                self,
                tensor_with_tf_override.orig_tensor_variable_source,
                tensor_with_tf_override.subclass_torch_function__func,
                tensor_with_tf_override.subclass_type,
                options,
                args,
                kwargs,
            )

            # The wrapping here follows the logic in
            # `torch.Tensor.__torch_function__`.
            if self.value in torch.overrides.get_default_nowrap_functions():
                return unwrapped
            return TensorWithTFOverrideVariable(
                unwrapped,
                tensor_with_tf_override.orig_tensor_variable_source,
                tensor_with_tf_override.subclass_torch_function__func,
                tensor_with_tf_override.subclass_type,
            )
        elif self.value is torch.amp.autocast_mode.autocast:
            return AutocastModeVariable.create(target_values=args, kwargs=kwargs)
        elif self.value in (
            torch.profiler.profile,
            torch.profiler.record_function,
            torch.autograd.profiler.profile,
            torch.autograd.profiler.record_function,
        ):
            log.warning("Profiler will be ignored")
            return NullContextVariable(**options)
        elif self.value is torch.autograd._profiler_enabled:
            unimplemented("torch.autograd._profiler_enabled not supported yet")
        elif self.value is torch.jit.annotate:
            assert len(args) == 2
            return args[1]
        elif self.value is torch.backends.cudnn.is_acceptable:
            # is_acceptable(tensor) returns true if
            #   (a) tensor dtype/device are supported by cudnn
            #   (b) cudnn is available
            #   (c) some initialization has completed
            # technically, it depends on some global state from (c) (torch.backends.cudnn.__cudnn_version)
            assert (
                len(args) == 1 or "tensor" in kwargs
            ), "Expect 1 input to cudnn.is_acceptable"
            tensor_variable = args[0] if len(args) > 0 else kwargs["tensor"]
            assert isinstance(
                tensor_variable, TensorVariable
            ), "Expect input to cudnn.is_acceptable to be a tensor"
            tensor_inp = torch.tensor(
                0, dtype=tensor_variable.dtype, device=tensor_variable.device
            )
            return ConstantVariable(
                torch.backends.cudnn.is_acceptable(tensor_inp), **options
            )
        if (
            self.value.__name__ == "get_state"
            and hasattr(self.value, "__self__")
            and isinstance(self.value.__self__, torch._C.Generator)
        ):

            def get_state_from_generator():
                return self.value()

            return wrap_fx_proxy(
                tx=tx,
                proxy=tx.output.create_proxy(
                    "call_function",
                    get_state_from_generator,
                    *proxy_args_kwargs(args, kwargs),
                ),
                example_value=self.value(),
                **options,
            )
        if (
            self.value.__name__ == "set_state"
            and hasattr(self.value, "__self__")
            and isinstance(self.value.__self__, torch._C.Generator)
        ) or self.value == torch.random.set_rng_state:
            assert len(args) == 1
            assert isinstance(args[0], TensorVariable)

            unimplemented(
                "TODO: make torch.random.set_rng_state work with FakeTensor/aot_autograd"
            )
            # In fake tensor case, this state doesn't matter, but
            # it needs to be valid to not segfault. Pull a real tensor out.
            # The value won't matter since we are running with fake tensors anyway, so rng doesn't matter.
            # However, it is imperative to record the call_function in the graph with the true args
            # (Not the fake example_value) - for the sake of graph correctness.
            if self.value == torch.random.set_rng_state:
                example_value = torch.random.get_rng_state()
            else:
                example_value = self.value.__self__.get_state()

            self.value.__module__ = self.__module__
            return wrap_fx_proxy(
                tx=tx,
                proxy=tx.output.create_proxy(
                    "call_function",
                    self.value,
                    *proxy_args_kwargs(args, kwargs),
                ),
                example_value=example_value,
                **options,
            )
        elif (
            self.value == torch.numel
            and len(args) == 1
            and isinstance(args[0], TensorVariable)
            and len(kwargs) == 0
        ):
            # TODO(voz): This is rewritten as a call_method because
            # torch.numel(x) w/ sym shapes raises a RuntimeError and x.numel() does not
            return wrap_fx_proxy(
                tx=tx,
                proxy=tx.output.create_proxy(
                    "call_method",
                    "numel",
                    *proxy_args_kwargs(args, kwargs),
                ),
                **options,
            )
        elif (
            self.value == torch.addcdiv
            and len(args) == 3
            and "value" in kwargs
            and len(kwargs) == 1
        ):
            # decompose addcdiv into constituent ops, prevents a graph break due to converting
            # value to a scalar
            result = TorchVariable(torch.div, **options).call_function(tx, args[1:], {})
            result = TorchVariable(torch.mul, **options).call_function(
                tx, [result, kwargs["value"]], {}
            )
            return TorchVariable(torch.add, **options).call_function(
                tx, [args[0], result], {}
            )
        else:
            any_symints_or_symfloats = any(
                [isinstance(x, DynamicShapeVariable) for x in args]
            )
            all_ints_or_floats = all(
                [
                    isinstance(
                        x, (variables.ConstantVariable, variables.DynamicShapeVariable)
                    )
                    for x in args
                ]
            )
            bin_ops = set(["add", "sub", "mul", "div", "sqrt"])
            if (
                self.value.__module__ == "torch"
                and self.value.__name__ in bin_ops
                and any_symints_or_symfloats
                and all_ints_or_floats
            ):
                msg = f"""\
Calling {str(self.value)} on only torch.SymInt arguments is not yet supported.
To support this behavior, we need to allow const-propping tensors that store symint data.
For now, dynamo will explicitly graph break when it encounters user code with this behavior.
"""
                log.warning(msg)
                raise unimplemented(msg)
            # Handle sth like torch.LongTensor(list(np.int64, np.int64, ...)),
            # as FX symbolic trace doesn't support numpy int/float as base types.
            if (
                HAS_NUMPY
                and self.value in tensortype_to_dtype
                and len(args) == 1
                and isinstance(args[0], ListVariable)
                and args[0].is_python_constant()
            ):
                for x in args[0].items:
                    if isinstance(x.value, np.generic):
                        x.value = x.value.item()

            # TODO(voz): Replace w/ dynamic shape rewrite table.
            # Ideally, we would be able to do this at ctor time, but alas we need a combination
            # of value + args to determine this.
            fn_ = self.value
            if any([isinstance(x, DynamicShapeVariable) for x in args]):
                if self.value == math.sqrt:
                    from torch.fx.experimental.symbolic_shapes import sym_sqrt

                    fn_ = sym_sqrt

            tensor_variable = wrap_fx_proxy(
                tx=tx,
                proxy=tx.output.create_proxy(
                    "call_function",
                    fn_,
                    *proxy_args_kwargs(args, kwargs),
                ),
                **options,
            )

            if "out" in kwargs:
                # out variants of torch operators like torch.sort and
                # torch.sigmoid mutate the tensors in the out field. Track such
                # tensors and rewrite the symbolic locals.
                if isinstance(tensor_variable, TupleVariable):
                    assert isinstance(kwargs["out"], TupleVariable)
                    output_tensor_names = [
                        tx.find_symbolic_locals_name(x) for x in kwargs["out"].items
                    ]
                    for idx, name in enumerate(output_tensor_names):
                        assert name in tx.symbolic_locals
                        tx.symbolic_locals[name] = tensor_variable.items[idx]
                elif isinstance(tensor_variable, TensorVariable):
                    assert isinstance(kwargs["out"], TensorVariable)
                    name = tx.find_symbolic_locals_name(kwargs["out"])
                    assert name in tx.symbolic_locals
                    tx.symbolic_locals[name] = tensor_variable
                else:
                    unimplemented(f"out variant of {type(kwargs['out'])}")

            return tensor_variable

    def is_dynamic_shapes(self, args, kwargs):
        """Check for dynamic shapes when shape specialization is enabled"""
        # TODO(jansel): need to get a complete list
        if self.value in (
            torch.nonzero,
            torch.unique,
            torch.unique_consecutive,
        ) or self.value.__name__ in ("nms",):
            return True

        if self.value is torch.where and len(args) + len(kwargs) == 1:
            return True

        if self.value in (
            torch.arange,
            torch.repeat_interleave,
        ):
            none = variables.ConstantVariable(None)

            def has_non_const(it):
                return not all(x.is_python_constant() for x in it)

            def arange(start=none, end=none, step=none, **kwargs):
                return has_non_const([start, end, step])

            def repeat_interleave(input, repeats, dim=none, **kwargs):
                return has_non_const([repeats])

            return locals()[self.value.__name__](*args, **kwargs)

        return False

    def _call_softmax(self, tx, args, kwargs, options):
        """rewrite the pattern nn.Softmax(dim=-1)(x) to F.softmax(x, -1)"""
        dim = args[0] if args else kwargs.get("dim", variables.ConstantVariable(None))

        def fake_softmax(input):
            from .builder import wrap_fx_proxy

            return wrap_fx_proxy(
                tx=tx,
                proxy=tx.output.create_proxy(
                    "call_function",
                    torch.nn.functional.softmax,
                    *proxy_args_kwargs([input, dim], {}),
                ),
                **VariableTracker.propagate([self, dim, input]),
            )

        return variables.LambdaVariable(fake_softmax, **options)

    def _call_cross_entropy_loss(self, tx, args, kwargs, options):
        """
        functional: input, target, weight=None, size_average=None, ignore_index=- 100, reduce=None, reduction='mean',
        label_smoothing=0.0

        non functional ctor: weight=None, size_average=None, ignore_index=- 100, reduce=None, reduction='mean',
        label_smoothing=0.0

        non functional loss call: input, target, optional_output
        """
        from . import ConstantVariable

        def normalize_args(
            weight=ConstantVariable(None),
            size_average=ConstantVariable(None),
            ignore_index=ConstantVariable(-100),
            reduce=ConstantVariable(None),
            reduction=ConstantVariable("mean"),
            label_smoothing=ConstantVariable(0.0),
        ):
            return (
                weight,
                size_average,
                ignore_index,
                reduce,
                reduction,
                label_smoothing,
            )

        (
            weight,
            size_average,
            ignore_index,
            reduce_arg,
            reduction,
            label_smoothing,
        ) = normalize_args(*args, **kwargs)

        def fake_cross_entropy_loss(input, target):
            from .builder import wrap_fx_proxy

            return wrap_fx_proxy(
                tx=tx,
                proxy=tx.output.create_proxy(
                    "call_function",
                    torch.nn.functional.cross_entropy,
                    *proxy_args_kwargs(
                        [
                            input,
                            target,
                            weight,
                            size_average,
                            ignore_index,
                            reduce_arg,
                            reduction,
                            label_smoothing,
                        ],
                        {},
                    ),
                ),
                **VariableTracker.propagate(
                    [
                        self,
                        weight,
                        size_average,
                        ignore_index,
                        reduce_arg,
                        reduction,
                        label_smoothing,
                        input,
                        target,
                    ]
                ),
            )

        return variables.LambdaVariable(fake_cross_entropy_loss, **options)

    def _call_ntuple(self, tx, args, kwargs, options):
        """inline behavior of torch.nn.modules.utils._ntuple"""
        if self.value is torch.nn.modules.utils._ntuple:
            count = args[0].as_python_constant()
        else:
            count = self.value.__closure__[0].cell_contents
        assert isinstance(count, int)

        def handle_ntuple(value):
            if value.has_unpack_var_sequence(tx):
                return variables.TupleVariable(
                    list(value.unpack_var_sequence(tx)),
                    **VariableTracker.propagate(self, value, args, kwargs.values()),
                )
            elif value.is_python_constant():
                # constant prop through it
                return variables.ConstantVariable(
                    torch.nn.modules.utils._ntuple(count)(value.as_python_constant()),
                    **VariableTracker.propagate(self, value, args, kwargs.values()),
                )
            else:
                unimplemented(f"torch.nn.modules.utils._ntuple({value})")

        if self.value is torch.nn.modules.utils._ntuple:
            return variables.LambdaVariable(handle_ntuple, **options)
        else:
            return handle_ntuple(args[0])


class TorchPyOperator(VariableTracker):
    def __init__(self, value, **kwargs):
        super(TorchPyOperator, self).__init__(**kwargs)
        self.value = value

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        from . import (
            ListVariable,
            NestedUserFunctionVariable,
            TensorVariable,
            UserFunctionVariable,
        )
        from .builder import wrap_fx_proxy

        assert kwargs is None or len(kwargs) == 0, "kwargs are not supported, yet"

        def make_attr(name):
            node = tx.output.create_proxy(
                "get_attr",
                name,
                (),
                {},
            )
            return node

        def add_subgraph(name, gm):
            next_name = None
            i = 0
            while not next_name:
                candidate = f"cond_{name}_{i}"
                if candidate in tx.output.nn_modules:
                    i += 1
                else:
                    next_name = candidate

            gm.__name__ = next_name
            src = NNModuleSource(GetItemSource(self.source, next_name))
            gm.torchdynamo_force_dynamic = False
            tx.output.register_attr_or_module(gm, next_name, source=src)
            return next_name

        if self.value.__name__ == "cond":
            # TODO(voz): Support fake tensor dispatch for recursive
            # ops - see torch/dispatch/_dispatcher.py

            assert len(args) == 4
            assert type(args[0]) is TensorVariable, str(type(args[0]))  # predicate
            assert isinstance(
                args[1], (UserFunctionVariable, NestedUserFunctionVariable)
            ), str(
                type(args[1])
            )  # true_fn
            assert isinstance(
                args[2], (UserFunctionVariable, NestedUserFunctionVariable)
            ), str(
                type(args[2])
            )  # false_fn
            assert type(args[3]) is ListVariable, str(type(args[3]))  # args

            # Our strategy for tracing the true/false branches of cond
            # are to checkpoint our graphstate, run the true branch,
            # roll it back to the checkpoint, and run the false
            # branch, and then merge the graphstates.  Well, perhaps
            # "merge" is too strong a word: we mostly assert that
            # the resulting graphstates have to be the same.
            #
            # We only permit guards to diverge (we union the guards from
            # both branches).  In particular, this means that side
            # effects are NOT permitted inside true/false branches; this
            # would be difficult to implement, because of the path
            # explosion problem.

            graph_checkpoint, checkpoint = tx.output.graph, tx.copy_graphstate()

            sub_args = args[3].unpack_var_sequence(tx)

            def speculate_branch(branch):
                # Setup the subgraph we're going to capture into
                tx.output.graph = torch.fx.Graph()
                tx.output.graphargs = []
                tx.output.name_to_input.clear()

                # One argument to graph per sub_args
                for a in sub_args:
                    assert isinstance(a, TensorVariable)
                    tx.output.create_graph_input(a.as_proxy().node.name)
                    # NB: we don't bother populating graphargs, as
                    # they won't actually get used by anything

                # NB: 0 is predicate
                ix = 1 if branch else 2

                output = args[ix].call_function(tx, sub_args, {})

                # Register output to graph
                # Modeled off of compile_and_call_fx_graph
                # TODO: support non single Tensor output
                assert isinstance(output, TensorVariable)
                tx.output.guards.update(output.guards)
                tx.output.create_node(
                    "output", "output", (tx.output.create_arg((output.as_proxy(),))), {}
                )

                tx.output.side_effects.prune_dead_object_new(tx)
                state = tx.copy_graphstate()

                guards = state.output.guards
                nn_modules = state.output.nn_modules
                side_effects = state.output.side_effects

                # Nub out bits of state that we don't require to be
                # equal
                comparable_state = state._replace(
                    output=state.output._replace(
                        guard_state=GuardsCheckpointState(set()),
                        # New cells are created for all closure variables, including functions in outer scope.
                        # Then these "side effects" are pruned (see above) based on which of them are accessed.
                        # Unfortunately this causes different "side effects" per branch.
                        # We shouldn't care when branches call different sets of functions in outer scope.
                        side_effects=SideEffects(
                            id_to_variable={
                                k: v
                                for k, v in side_effects.id_to_variable.items()
                                if not isinstance(v, NewCellVariable)
                            },
                            store_attr_mutations={
                                k: v
                                for k, v in side_effects.store_attr_mutations.items()
                                if not isinstance(k, AttributeMutationNew)
                            },
                            keepalive=[],
                        ),
                        nn_modules=None,
                        # Timestamp is monotonically increasing so we don't
                        # care about divergence
                        timestamp=0,
                        # Meh (problem is the nodes don't compare equal;
                        # maybe nub out outputs only)
                        name_to_input=OrderedDict(),
                    )
                )

                graph = tx.output.graph
                tx.output.graph = graph_checkpoint
                tx.restore_graphstate(checkpoint)

                return output, graph, guards, nn_modules, comparable_state

            (
                true_r,
                true_graph,
                true_guards,
                true_nn_modules,
                true_cmp,
            ) = speculate_branch(True)
            (
                false_r,
                false_graph,
                false_guards,
                false_nn_modules,
                false_cmp,
            ) = speculate_branch(False)

            if true_cmp != false_cmp:
                unimplemented(true_cmp.diff(false_cmp))

            # Add guards
            tx.output.tracing_context.guards_context.dynamo_guards |= false_guards
            tx.output.tracing_context.guards_context.dynamo_guards |= true_guards

            true_name = add_subgraph(
                "true", torch.fx.GraphModule(true_nn_modules, true_graph)
            )
            false_name = add_subgraph(
                "false", torch.fx.GraphModule(false_nn_modules, false_graph)
            )

            # Apply side effects (guaranteed to be equal)
            tx.output.side_effects = true_cmp.output.side_effects

            true_node = make_attr(true_name)
            false_node = make_attr(false_name)

            p_args = (
                args[0].as_proxy(),
                true_node,
                false_node,
                tuple(a.as_proxy() for a in sub_args),
            )
            # TODO: assert that the true/false return values are
            # consistent
            example_value = true_r.as_proxy().node.meta["example_value"]
        else:
            unimplemented(f"PyOperator {self.value.__name__}")

        # Store the invocation as a call
        return wrap_fx_proxy(
            tx=tx,
            proxy=tx.output.create_proxy(
                "call_function",
                self.value,
                args=tuple(p_args),
                kwargs={},
            ),
            example_value=example_value,
        )

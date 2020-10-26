import datetime
import inspect
import typing
from typing import Callable, Dict, List, Optional, Tuple, Union, Any

import flytekit.annotated.promise
import flytekit.annotated.type_engine
from flytekit import logger
from flytekit.annotated.condition import ConditionalSection
from flytekit.annotated.context_manager import ExecutionState, FlyteContext, FlyteEntities
from flytekit.annotated.interface import (
    transform_inputs_to_parameters,
    transform_interface_to_typed_interface,
    transform_signature_to_interface,
)
from flytekit.annotated.node import Node
from flytekit.annotated.promise import Promise, create_task_output
from flytekit.annotated.type_engine import TypeEngine
from flytekit.common import constants as _common_constants
from flytekit.common import promise as _common_promise
from flytekit.common.exceptions import user as _user_exceptions
from flytekit.common.mixins import registerable as _registerable
from flytekit.common.workflow import SdkWorkflow as _SdkWorkflow
from flytekit.models import interface as _interface_models
from flytekit.models import literals as _literal_models
from flytekit.models import types as _type_models
from flytekit.models.core import identifier as _identifier_model
from flytekit.models.core import workflow as _workflow_model


def _workflow_fn_outputs_to_promise(ctx: FlyteContext, native_outputs: typing.OrderedDict[str, type],
                                    typed_outputs: Dict[str, _interface_models.Variable],
                                    outputs: Union[Any, Tuple[Any]]) -> List[Promise]:
    if len(native_outputs) == 0:
        if outputs is not None:
            raise AssertionError("something returned from wf but shouldn't have outputs")
        return None

    if len(native_outputs) == 1:
        if isinstance(outputs, tuple):
            if len(outputs) != 1:
                raise AssertionError(
                    f"The Workflow specification indicates only one return value, received {len(outputs)}")
        else:
            outputs = (outputs,)

    if len(native_outputs) > 1:
        if not isinstance(outputs, tuple) or len(native_outputs) != len(outputs):
            # Length check, clean up exception
            raise AssertionError(
                f"The workflow specification indicates {len(native_outputs)} return vals, but received {len(outputs)}")

    # This recasts the Promises provided by the outputs of the workflow's tasks into the correct output names
    # of the workflow itself
    return_vals = []
    for (k, t), v in zip(native_outputs.items(), outputs):
        if isinstance(v, Promise):
            return_vals.append(v.with_var(k))
        else:
            # Found a return type that is not a promise, so we need to transform it
            var = typed_outputs[k]
            return_vals.append(Promise(var=k, val=TypeEngine.to_literal(ctx, v, t, var.type)))
    return return_vals


class Workflow(object):
    """
    When you assign a name to a node.
    * Any upstream node that is not assigned, recursively assign
    * When you get the call to the constructor, keep in mind there may be duplicate nodes, because they all should
      be wrapper nodes.
    """

    def __init__(self, workflow_function: Callable):
        self._name = f"{workflow_function.__module__}.{workflow_function.__name__}"
        self._workflow_function = workflow_function
        self._native_interface = transform_signature_to_interface(inspect.signature(workflow_function))
        self._interface = transform_interface_to_typed_interface(self._native_interface)
        # These will get populated on compile only
        self._nodes = None
        self._output_bindings: Optional[List[_literal_models.Binding]] = None

        # This will get populated only at registration time, when we retrieve the rest of the environment variables like
        # project/domain/version/image and anything else we might need from the environment in the future.
        self._registerable_entity: Optional[_SdkWorkflow] = None

        # TODO do we need this - can this not be in launchplan only?
        #    This can be in launch plan only, but is here only so that we don't have to re-evaluate. Or
        #    we can re-evaluate.
        self._input_parameters = None
        FlyteEntities.entities.append(self)

    @property
    def function(self):
        return self._workflow_function

    @property
    def name(self) -> str:
        return self._name

    @property
    def interface(self) -> _interface_models.TypedInterface:
        return self._interface

    def _construct_input_promises(self) -> Dict[str, _type_models.OutputReference]:
        """
        This constructs input promises for all the inputs of the workflow, binding them to the global
        input node id which you should think about as the start node.
        """
        return {
            k: _type_models.OutputReference(_common_constants.GLOBAL_INPUT_NODE_ID, k)
            for k in self.interface.inputs.keys()
        }

    def compile(self, **kwargs):
        """
        Supply static Python native values in the kwargs if you want them to be used in the compilation
        """
        # TODO: should we even define it here?
        self._input_parameters = transform_inputs_to_parameters(self._native_interface)
        all_nodes = []
        ctx = FlyteContext.current_context()
        with ctx.new_compilation_context() as comp_ctx:
            # Construct the default input promise bindings, but then override with the provided inputs, if any
            input_kwargs = self._construct_input_promises()
            input_kwargs.update(kwargs)
            workflow_outputs = self._workflow_function(**input_kwargs)
            all_nodes.extend(comp_ctx.compilation_state.nodes)

        # Iterate through the workflow outputs
        bindings = []
        output_names = list(self.interface.outputs.keys())
        # The reason the length 1 case is separate is because the one output might be a list. We don't want to
        # iterate through the list here, instead we should let the binding creation unwrap it and make a binding
        # collection/map out of it.
        if len(output_names) == 1:
            if isinstance(workflow_outputs, tuple) and len(workflow_outputs) != 1:
                raise AssertionError(
                    f"The Workflow specification indicates only one return value, received {len(workflow_outputs)}")
            t = self._native_interface.outputs[output_names[0]]
            b = flytekit.annotated.promise.binding_from_python_std(
                ctx, output_names[0], self.interface.outputs[output_names[0]].type, workflow_outputs, t,
            )
            bindings.append(b)
        elif len(output_names) > 1:
            if not isinstance(workflow_outputs, tuple):
                raise AssertionError(
                    f"The Workflow specification indicates multiple return values, received only one")
            if len(output_names) != len(workflow_outputs):
                raise Exception(f"Length mismatch {len(output_names)} vs {len(workflow_outputs)}")
            for i, out in enumerate(output_names):
                if isinstance(workflow_outputs[i], ConditionalSection):
                    raise AssertionError("A Conditional block (if-else) should always end with an `else_()` clause")
                t = self._native_interface.outputs[out]
                b = flytekit.annotated.promise.binding_from_python_std(
                    ctx, out, self.interface.outputs[out].type, workflow_outputs[i], t,
                )
                bindings.append(b)

        # Save all the things necessary to create an SdkWorkflow, except for the missing project and domain
        self._nodes = all_nodes
        self._output_bindings = bindings
        if not output_names:
            return None
        if len(output_names) == 1:
            return bindings[0]
        return tuple(bindings)

    def _local_execute(self, ctx: FlyteContext, **kwargs) -> Union[Tuple[Promise], Promise, None]:
        """
        Performs local execution of a workflow. kwargs are expected to be Promises for the most part (unless,
        someone has hardcoded in my_wf(input_1=5) or something).
        :param ctx: The FlyteContext
        :param nested: boolean that indicates if this is a nested workflow execution (subworkflow)
        :param kwargs: parameters for the workflow itself
        """
        logger.info(f"Executing Workflow {self._name}, ctx{ctx.execution_state.Mode}")

        # This is done to support the invariant that Workflow local executions always work with Promise objects
        # holding Flyte literal values. Even in a wf, a user can call a sub-workflow with a Python native value.
        for k, v in kwargs.items():
            if not isinstance(v, Promise):
                t = self._native_interface.inputs[k]
                kwargs[k] = Promise(var=k, val=TypeEngine.to_literal(ctx, v, t, self.interface.inputs[k].type))

        # TODO: function_outputs are all assumed to be Promise objects produced by task calls, but can they be
        #   other things as well? What if someone just returns 5? Should we disallow this?
        function_outputs = self._workflow_function(**kwargs)

        promises = _workflow_fn_outputs_to_promise(ctx, self._native_interface.outputs, self.interface.outputs,
                                                   function_outputs)
        return create_task_output(promises)

    def __call__(self, *args, **kwargs):
        if len(args) > 0:
            raise AssertionError("Only Keyword Arguments are supported for Workflow executions")

        ctx = FlyteContext.current_context()

        # Handle subworkflows in compilation
        if ctx.compilation_state is not None:
            return self._create_and_link_node(ctx, **kwargs)

        elif (
                ctx.execution_state is not None and ctx.execution_state.mode == ExecutionState.Mode.LOCAL_WORKFLOW_EXECUTION
        ):
            # We are already in a local execution, just continue the execution context
            return self._local_execute(ctx, **kwargs)

        # When someone wants to run the workflow function locally. Assume that the inputs given are given as Python
        # native values. _local_execute will always translate Python native literals to Flyte literals, so no worries
        # there, but it'll return Promise objects.
        else:
            # Run some sanity checks
            # Even though the _local_execute call generally expects inputs to be Promises, we don't have to do the
            # conversion here in this loop. The reason is because we don't prevent users from specifying inputs
            # as direct scalars, which means there's another Promise-generating loop inside _local_execute too
            for k, v in kwargs.items():
                if k not in self.interface.inputs:
                    raise ValueError(f"Received unexpected keyword argument {k}")
                if isinstance(v, Promise):
                    raise ValueError(f"Received a promise for a workflow call, when expecting a native value for {k}")

            with ctx.new_execution_context(mode=ExecutionState.Mode.LOCAL_WORKFLOW_EXECUTION) as ctx:
                result = self._local_execute(ctx, **kwargs)

            expected_outputs = len(self._native_interface.outputs)
            if (
                    (expected_outputs == 0 and result is None)
                    or (expected_outputs > 1 and len(result) == expected_outputs)
                    or (expected_outputs == 1 and result is not None)
            ):
                if result is None:
                    return None
                elif isinstance(result, Promise):
                    v = [v for k, v in self._native_interface.outputs.items()][0]
                    return TypeEngine.to_python_value(ctx, result.val, v)
                else:
                    for prom in result:
                        if not isinstance(prom, Promise):
                            raise Exception("should be promises")
                        native_list = [
                            TypeEngine.to_python_value(ctx, promise.val, self._native_interface.outputs[promise.var])
                            for promise in result
                        ]
                        return tuple(native_list)

            raise ValueError("expected outputs and actual outputs do not match")

    def _create_and_link_node(self, ctx: FlyteContext, *args, **kwargs):
        """
        This method is used to create a node representing a subworkflow call in a workflow. It should closely mirror
        the _compile function found in Task.
        """

        # TODO: Add handling of defaults
        used_inputs = set()
        bindings = []

        for k in sorted(self.interface.inputs):
            var = self.interface.inputs[k]
            if k not in kwargs:
                raise _user_exceptions.FlyteAssertion("Input was not specified for: {} of type {}".format(k, var.type))
            t = self._native_interface.inputs[k]
            bindings.append(flytekit.annotated.promise.binding_from_python_std(ctx, k, var.type, kwargs[k], t))
            used_inputs.add(k)

        extra_inputs = used_inputs ^ set(kwargs.keys())
        if len(extra_inputs) > 0:
            raise _user_exceptions.FlyteAssertion(
                "Too many inputs were specified for the interface.  Extra inputs were: {}".format(extra_inputs)
            )

        # Detect upstream nodes
        # These will be our annotated Nodes until we can amend the Promise to use NodeOutputs that reference our Nodes
        upstream_nodes = [input_val.ref.sdk_node for input_val in kwargs.values() if isinstance(input_val, Promise)]

        node = Node(
            id=f"node-{len(ctx.compilation_state.nodes)}",
            metadata=_workflow_model.NodeMetadata(self._name, datetime.timedelta(), _literal_models.RetryStrategy(0)),
            bindings=sorted(bindings, key=lambda b: b.var),
            upstream_nodes=upstream_nodes,  # type: ignore
            flyte_entity=self,
        )
        ctx.compilation_state.nodes.append(node)

        # Create a node output object for each output, they should all point to this node of course.
        # TODO: Again, we need to be sure that we end up iterating through the output names in the correct order
        #  investigate this and document here.
        node_outputs = []
        for output_name, output_var_model in self.interface.outputs.items():
            # TODO: If node id gets updated later, we have to make sure to update the NodeOutput model's ID, which
            #  is currently just a static str
            node_outputs.append(
                Promise(output_name, _common_promise.NodeOutput(sdk_node=node, sdk_type=None, var=output_name))
            )

        return create_task_output(node_outputs)

    def get_registerable_entity(self) -> _registerable.RegisterableEntity:
        settings = FlyteContext.current_context().registration_settings
        if self._registerable_entity is not None:
            return self._registerable_entity

        workflow_id = _identifier_model.Identifier(
            _identifier_model.ResourceType.WORKFLOW, settings._project, settings._domain, self._name, settings._version
        )

        # Translate nodes
        sdk_nodes = [n.get_registerable_entity() for n in self._nodes]

        self._registerable_entity = _SdkWorkflow(
            nodes=sdk_nodes,
            id=workflow_id,
            metadata=_workflow_model.WorkflowMetadata(),
            metadata_defaults=_workflow_model.WorkflowMetadataDefaults(),
            interface=self._interface,
            output_bindings=self._output_bindings,
        )
        # Reset just to make sure it's what we give it
        self._registerable_entity.id._project = settings._project
        self._registerable_entity.id._domain = settings._domain
        self._registerable_entity.id._name = self._name
        self._registerable_entity.id._version = settings._version

        return self._registerable_entity


def workflow(_workflow_function=None):
    # Unlike for tasks, where we can determine the entire structure of the task by looking at the function's signature,
    # workflows need to have the body of the function itself run at module-load time. This is because the body of the
    # workflow is what expresses the workflow structure.
    def wrapper(fn):
        # TODO: Again, at this point, we should be able to identify the name of the workflow
        workflow_id = _identifier_model.Identifier(
            _identifier_model.ResourceType.WORKFLOW, "proj", "dom", "moreblah", "1"
        )
        workflow_instance = Workflow(fn)
        workflow_instance.compile()
        workflow_instance.id = workflow_id

        return workflow_instance

    if _workflow_function:
        return wrapper(_workflow_function)
    else:
        return wrapper

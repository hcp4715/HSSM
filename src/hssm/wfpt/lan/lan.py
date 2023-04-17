"""
Likelihood Approximation Network (LAN) extension for the Wiener First-Passage Time
(WFPT) distribution. Uses a neural network to approximate the likelihood function of
the Wiener First-Passage Time distribution.

This module handles LAN-related operations, such as producing log-likelihood functions
from onnx model files and wrapping jax log-likelihood functions in pytensor Ops.
"""

from __future__ import annotations

from os import PathLike
from typing import Callable, List, Tuple

import jax.numpy as jnp
import numpy as np
import onnx
import pytensor
import pytensor.tensor as pt
from jax import jit, vjp, vmap
from numpy.typing import ArrayLike
from pytensor.graph import Apply, Op
from pytensor.link.jax.dispatch import jax_funcify

from .onnx2pt import pt_interpret_onnx
from .onnx2xla import interpret_onnx

LogLikeFunc = Callable[..., ArrayLike]
LogLikeGrad = Callable[..., ArrayLike]


def make_jax_logp_funcs_from_onnx(
    model: str | PathLike | onnx.ModelProto,
    params_is_reg: List[bool],
) -> Tuple[LogLikeFunc, LogLikeGrad, LogLikeFunc,]:
    """
    Makes a jax function and its Vector-Jacobian Product from an ONNX Model.

    Parameters
    ----------
        model:
            A path or url to the ONNX model, or an ONNX Model object that's
            already loaded.
        params_is_reg:
            A list of booleans indicating whether the parameters are regressions.
            Parameters that are regressions will not be vectorized in likelihood
            calculations.

    Returns
    -------
        A triple of jax functions. The first calculates the
        forward pass, the second calculates the VJP, and the third is
        the forward-pass that's not jitted.
    """

    loaded_model = (
        onnx.load(str(model)) if isinstance(model, (str, PathLike)) else model
    )

    def logp(data: np.ndarray, *dist_params) -> ArrayLike:
        """
        Computes the element-wise log-likelihoods given data and arbitrary
        numbers of parameters.

        Parameters
        ----------
        data:
            A two-column numpy array with response time and response
        dist_params:
            A list of parameters used in the likelihood computation.

        Returns
        -------
            The sum of log-likelihoods.
        """

        # Makes a matrix to feed to the LAN model
        input_vector = jnp.concatenate((jnp.array(dist_params), data))

        return interpret_onnx(loaded_model.graph, input_vector)[0].squeeze()

    # The vectorization of the logp function
    vmap_logp = vmap(
        logp,
        in_axes=[0] + [0 if is_regression else None for is_regression in params_is_reg],
    )

    def vjp_vmap_logp(data: np.ndarray, *dist_params, gz) -> List[ArrayLike]:
        """Computes the VJP of the log-likelihood function

        Parameters
        ----------
        data:
            A two-column numpy array with response time and response.
        dist_params:
            A list of parameters used in the likelihood computation.
        gz:
            The point at which the VJP is to be computed.

        Returns
        -------
            The VJP of the log-likelihood function computed at gz.
        """

        _, vjp_fn = vjp(vmap_logp, data, *dist_params)
        return vjp_fn(gz)[1:]

    return jit(vmap_logp), jit(vjp_vmap_logp), vmap_logp


def make_jax_logp_ops(
    logp: LogLikeFunc,
    logp_vjp: LogLikeGrad,
    logp_nojit: LogLikeFunc,
) -> Op:
    """Wraps the JAX functions and its gradient in pytensor Ops.

    Parameters
    ----------
    logp:
        A JAX function that represents the feed-forward operation of the LAN network.
    logp_vjp:
        The Jax function that calculates the VJP of the logp function.
    logp_nojit:
        The non-jit version of logp.

    Returns
    -------
        An pytensor op that wraps the feed-forward operation and can be used with
        pytensor.grad.
    """

    class LANLogpOp(Op):  # pylint: disable=W0223
        """Wraps a JAX function in an pytensor Op."""

        def make_node(self, data, *dist_params):
            inputs = [
                pt.as_tensor_variable(data),
            ] + [pt.as_tensor_variable(dist_param) for dist_param in dist_params]

            outputs = [pt.vector()]

            return Apply(self, inputs, outputs)

        def perform(self, node, inputs, output_storage):
            """Performs the Apply node.

            Parameters
            ----------
                inputs: This is a list of data from which the values stored in
                    output_storage are to be computed using non-symbolic language.
                output_storage: This is a list of storage cells where the output
                    is to be stored. A storage cell is a one-element list. It is
                    forbidden to change the length of the list(s) contained in
                    output_storage. There is one storage cell for each output of
                    the Op.
            """
            result = logp(*inputs)
            output_storage[0][0] = np.asarray(result, dtype=node.outputs[0].dtype)

        def grad(self, inputs, output_gradients):
            results = lan_logp_vjp_op(inputs[0], *inputs[1:], gz=output_gradients[0])
            output = [
                pytensor.gradient.grad_not_implemented(self, 0, inputs[0]),
            ] + results

            return output

    class LANLogpVJPOp(Op):  # pylint: disable=W0223
        """Wraps the gradient opearation of a jax function in an pytensor op."""

        def make_node(self, data, *dist_params, gz):
            inputs = (
                [
                    pt.as_tensor_variable(data),
                ]
                + [pt.as_tensor_variable(dist_param) for dist_param in dist_params]
                + [pt.as_tensor_variable(gz)]
            )
            outputs = [inp.type() for inp in inputs[1:-1]]

            return Apply(self, inputs, outputs)

        def perform(self, node, inputs, outputs):
            results = logp_vjp(inputs[0], *inputs[1:-1], gz=inputs[-1])

            for i, result in enumerate(results):
                outputs[i][0] = np.asarray(result, dtype=node.outputs[i].dtype)

    lan_logp_op = LANLogpOp()
    lan_logp_vjp_op = LANLogpVJPOp()

    # Unwraps the JAX function for sampling with JAX backend.
    @jax_funcify.register(LANLogpOp)
    def logp_op_dispatch(op, **kwargs):  # pylint: disable=W0612,W0613
        return logp_nojit

    return lan_logp_op


def make_pytensor_logp(
    model: str | PathLike | onnx.ModelProto, params_is_reg: List[bool]
) -> Callable[..., ArrayLike]:
    """
    Converting onnx model file to pytensor

    Parameters
    ----------
        model:
            A path or url to the ONNX model, or an ONNX Model object that's
            already loaded.
        params_is_reg:
            A list of booleans indicating whether the parameters are regressions.
            Parameters that are regressions will not be vectorized in likelihood
            calculations.

    Returns
    -------
        The logp function that applies the ONNX model to data
    """
    loaded_model: onnx.ModelProto = (
        onnx.load(str(model)) if isinstance(model, (str, PathLike)) else model
    )

    def logp(data: np.ndarray, *dist_params) -> ArrayLike:

        # Specify input layer of MLP
        data = data.reshape((-1, 2))
        inputs = pt.zeros((data.shape[0], len(params_is_reg) + 2))
        for i, dist_param in enumerate(dist_params):
            inputs = pt.set_subtensor(
                inputs[:, i],
                dist_param,
            )
        inputs = pt.set_subtensor(inputs[:, -2:], data)
        return pt.squeeze(pt_interpret_onnx(loaded_model.graph, inputs)[0])

    return logp

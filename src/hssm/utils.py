"""Utility functions and classes.

HSSM has to reconcile with two representations: it's own representation as an HSSM and
the representation acceptable to Bambi. The two are not equivalent. This file contains
the Param class that reconcile these differences.

The Param class is an abstraction that stores the parameter specifications and turns
these representations in Bambi-compatible formats through convenience function
_parse_bambi().
"""

import logging
from typing import Any, Literal, cast

import bambi as bmb
import jax
import numpy as np
import pandas as pd
import pytensor
import xarray as xr
from bambi.terms import CommonTerm, GroupSpecificTerm, HSGPTerm, OffsetTerm
from huggingface_hub import hf_hub_download

from .param import Param

_logger = logging.getLogger("hssm")

REPO_ID = "franklab/HSSM"


def download_hf(path: str):
    """
    Download a file from a HuggingFace repository.

    Parameters
    ----------
    path : str
        The path of the file to download in the repository.

    Returns
    -------
    str
        The local path where the file is downloaded.

    Notes
    -----
    The repository is specified by the REPO_ID constant,
    which should be a valid HuggingFace.co repository ID.
    The file is downloaded using the HuggingFace Hub's
     hf_hub_download function.
    """
    return hf_hub_download(repo_id=REPO_ID, filename=path)


def make_alias_dict_from_parent(parent: Param) -> dict[str, str]:
    """Make aliases from the parent parameter.

    From a Param object that represents a parent parameter in Bambi,
    returns a dict that represents how Bambi should alias its parameters to
    make it more HSSM-friendly.

    Parameters
    ----------
    parent: A Param object that represents a parent parameter.

    Returns
    -------
        A dict that indicates how Bambi should alias its parameters.
    """
    assert parent.is_parent, "This Param object should be a parent!"
    assert parent.name is not None

    result_dict = {"c(rt, response)": "rt,response"}

    # The easy case. We will just alias "Intercept" as the actual name of the
    # parameter
    if not parent.is_regression:
        result_dict |= {"Intercept": parent.name}

        return result_dict

    # The regression case:
    # In this case, the name of the response variable should actually be
    # the name of the parent parameter
    result_dict["c(rt, response)"] = parent.name

    return result_dict


def _get_alias_dict(
    model: bmb.Model, parent: Param, response_c: str, response_str: str
) -> dict[str, str | dict]:
    """Make a list of aliases.

    Iterates through a list of Param objects, and aliases a Bambi model's parameters
    to make it more HSSM-friendly.

    Parameters
    ----------
    model
        A Bambi model.
    parent
        The Param representation of the parent parameter.
    response_c
        The name of the response parameters in the c() format.
    response_str
        The name of the response parameters in the comma-separated format.

    Returns
    -------
    dict[str, str | dict]
        A dict that indicates how Bambi should alias its parameters.
    """
    parent_name = cast(str, parent.name)
    alias_dict: dict[str, Any] = {response_c: response_str}

    if len(model.distributional_components) == 1:
        if not parent.is_regression or (
            parent.is_regression and parent.formula is None
        ):
            alias_dict[parent_name] = f"{parent_name}_mean"
            alias_dict["Intercept"] = parent_name
        else:
            for name, term in model.components[parent_name].terms.items():
                if isinstance(
                    term, (CommonTerm, GroupSpecificTerm, HSGPTerm, OffsetTerm)
                ):
                    alias_dict[name] = f"{parent_name}_{name}"

        return alias_dict

    for component_name, component in model.distributional_components.items():
        if component_name == parent_name:
            alias_dict[component_name] = {}
            if not parent.is_regression:
                # Most likely this branch will not be reached
                alias_dict[component_name]["Intercept"] = f"{parent_name}_Intercept"
            else:
                for name, term in component.terms.items():
                    if isinstance(
                        term, (CommonTerm, GroupSpecificTerm, HSGPTerm, OffsetTerm)
                    ):
                        alias_dict[component_name] |= {name: f"{parent_name}_{name}"}
            break

    return alias_dict


def set_floatX(dtype: Literal["float32", "float64"], update_jax: bool = True):
    """Set float types for pytensor and Jax.

    Often we wish to work with a specific type of float in both PyTensor and JAX.
    This function helps set float types in both packages.

    Parameters
    ----------
    dtype
        Either `float32` or `float64`. Float type for pytensor (and jax if `jax=True`).
    update_jax : optional
        Whether this function also sets float type for JAX by changing the
        `jax_enable_x64` setting in JAX config. Defaults to True.
    """
    if dtype not in ["float32", "float64"]:
        raise ValueError('`dtype` must be either "float32" or "float64".')

    pytensor.config.floatX = dtype
    _logger.info("Setting PyTensor floatX type to %s.", dtype)

    if update_jax:
        jax_enable_x64 = dtype == "float64"
        jax.config.update("jax_enable_x64", jax_enable_x64)

        _logger.info(
            'Setting "jax_enable_x64" to %s. '
            + "If this is not intended, please set `jax` to False.",
            jax_enable_x64,
        )


def _print_prior(term: CommonTerm | GroupSpecificTerm) -> str:
    """Make the output string of a term.

    If prior is a float, print x: prior. Otherwise, print x ~ prior.

    Parameters
    ----------
    term
        A BaseTerm in Bambi

    Returns
    -------
        A string representing the term_name ~ prior pair
    """
    term_name = term.alias or term.name
    prior = term._prior

    if isinstance(prior, float):
        return f"        {term_name}: {prior}"

    return f"        {term_name} ~ {prior}"


def _process_param_in_kwargs(
    name, prior: float | dict | bmb.Prior | Param
) -> dict | Param:
    """Process parameters specified in kwargs.

    Parameters
    ----------
    name
        The name of the parameters.
    prior
        The prior specified.

    Returns
    -------
    dict
        A `dict` that complies with ways to specify parameters in `include`.

    Raises
    ------
    ValueError
        When `prior` is not a `float`, a `dict`, or a `bmb.Prior` object.
    """
    if isinstance(prior, (int, float, bmb.Prior)):
        return {"name": name, "prior": prior}
    elif isinstance(prior, dict):
        if ("prior" in prior) or ("bounds" in prior):
            return prior | {"name": name}
        else:
            return {"name": name, "prior": prior}
    elif isinstance(prior, Param):
        prior["name"] = name
        return prior
    else:
        raise ValueError(
            f"Parameter {name} must be a float, a dict, a bmb.Prior, "
            + "or a hssm.Param object."
        )


def _generate_random_indices(
    n_samples: int | float | None, n_draws: int
) -> np.ndarray | None:
    """Generate random indices for sampling an InferenceData object.

    Parameters
    ----------
    n_samples
        When an interger >= 1, the number of samples to be extracted from the draw
        dimension. If this integer is larger than n_draws, returns None, which means
        all samples are extracted. When a float between 0 and 1, the proportion of
        samples to be extracted from the draw dimension. If this proportion is very
        small, at least one sample will be drawn. When None, returns None.
    n_draws
        The number of total draws in the InferenceData object.

    Returns
    -------
    np.ndarray
        A 2D array of shape (n_chains, n_draws) with random indices or None, which means
        using the entire dataset without random sampling.
    """
    if n_draws <= 0:
        raise ValueError("n_draws must be >= 1.")

    if n_samples is None:
        return None

    if n_samples > n_draws:
        _logger.warning("n_samples > n_draws. Using the entire dataset.")
        return None

    if isinstance(n_samples, float):
        if n_samples <= 0 or n_samples > 1:
            raise ValueError("When a float, n_samples must be between 0 and 1.")
        n_samples = max(int(n_samples * n_draws), 1)

    if n_samples < 1:
        raise ValueError("When an int, n_samples must be >= 1.")

    sampling_indices = np.random.choice(n_draws, size=n_samples, replace=False)

    return sampling_indices


def _random_sample(
    data: xr.DataArray | xr.Dataset, n_samples: int | float | None
) -> xr.DataArray | xr.Dataset:
    """Randomly sample a DataArray or Dataset.

    Parameters
    ----------
    data
        A DataArray or Dataset to be sampled.
    n_samples
        When an interger >= 1, the number of samples to be extracted from the draw
        dimension. If this integer is larger than n_draws, returns None, which means
        all samples are extracted. When a float between 0 and 1, the proportion of
        samples to be extracted from the draw dimension. If this proportion is very
        small, at least one sample will be drawn. When None, returns None.

    Returns
    -------
    xr.DataArray | xr.Dataset
        The sampled InferenceData object.
    """
    n_draws = data.draw.size
    sampling_indices = _generate_random_indices(n_samples, n_draws)

    if sampling_indices is None:
        return data
    return data.isel(draw=sampling_indices)


def _rearrange_data(data: pd.DataFrame | np.ndarray) -> pd.DataFrame | np.ndarray:
    """Rearrange a dataframe so that missing values are on top.

    We assume the dataframe's first column can contain missing values coded as -999.0.

    Parameters
    ----------
    df
        The dataframe or numpy array to be rearranged.

    Returns
    -------
    pd.DataFrame | np.ndarray
        The rearranged dataframe.
    """
    if isinstance(data, pd.DataFrame):
        missing_indices = data.iloc[:, 0] == -999.0
        split_missing = data.loc[missing_indices, :]
        split_not_missing = data.loc[~missing_indices, :]

        return pd.concat([split_missing, split_not_missing])

    missing_indices = data[:, 0] == -999.0
    split_missing = data[missing_indices, :]
    split_not_missing = data[~missing_indices, :]

    return np.concatenate([split_missing, split_not_missing])


def _split_array(data: np.ndarray | list[int], divisor: int) -> list[np.ndarray]:
    num_splits = len(data) // divisor + (1 if len(data) % divisor != 0 else 0)
    return [tmp.astype(int) for tmp in np.array_split(data, num_splits)]

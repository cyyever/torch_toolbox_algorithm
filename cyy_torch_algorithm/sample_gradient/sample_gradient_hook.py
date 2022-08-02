import functools
from typing import Callable

import torch
from cyy_torch_algorithm.evaluation import eval_model
from cyy_torch_algorithm.sample_computation_hook import SampleComputationHook
from cyy_torch_toolbox.device import put_data_to_device
from functorch import grad, vmap


def sample_gradient_worker_fun(
    model_with_loss,
    sample_indices,
    inputs,
    input_features,
    targets,
    worker_device,
):
    gradient_lists = vmap(
        grad(
            functools.partial(
                eval_model, device=worker_device, model_with_loss=model_with_loss
            )
        ),
        in_dims=(None, 0, 0),
        randomness="same",
    )(
        model_with_loss.model_util.get_parameter_list(detach=True),
        torch.stack(
            put_data_to_device(inputs, device=worker_device, non_blocking=True)
        ),
        torch.stack(targets),
    )
    return dict(zip(sample_indices, gradient_lists))


class SampleGradientHook(SampleComputationHook):
    def _get_worker_fun(self):
        return sample_gradient_worker_fun

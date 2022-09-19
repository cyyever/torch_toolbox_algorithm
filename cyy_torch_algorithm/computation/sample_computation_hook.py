import functools
from typing import Callable

import torch
from cyy_torch_algorithm.computation.computation_hook import ComputationHook
# from cyy_naive_lib.time_counter import TimeCounter
from cyy_torch_toolbox.hooks.add_index_to_dataset import AddIndexToDataset
from cyy_torch_toolbox.tensor import tensor_to


class SampleComputationHook(ComputationHook):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.dataset_index_hook = AddIndexToDataset()
        self.__sample_selector = None
        self.__input_transform: Callable | None = None

    def set_sample_selector(self, selector: Callable) -> None:
        self.__sample_selector = selector

    def set_input_transform(self, transform: Callable) -> None:
        self.__input_transform = transform

    def set_computed_indices(self, indices):
        self.set_sample_selector(lambda sample_index, *args: sample_index in indices)

    def add_task(self, model_executor, sample_indices, inputs, input_features, targets):
        inputs, batch_dim = model_executor.split_batch_input(
            inputs=inputs, targets=targets
        )
        if (
            batch_dim != 0
            and input_features is not None
            and isinstance(input_features, torch.Tensor)
        ):
            input_features = input_features.permute(batch_dim, 0, 2)
        if input_features is None:
            input_features = [None] * len(sample_indices)

        processed_indices = []
        processed_inputs = []
        processed_features = []
        processed_targets = []
        for (sample_index, sample_input, input_feature, sample_target) in zip(
            sample_indices, inputs, input_features, targets
        ):
            if self.__sample_selector is not None and not self.__sample_selector(
                sample_index, sample_input
            ):
                continue
            if isinstance(sample_input, torch.Tensor):
                sample_input = sample_input.unsqueeze(batch_dim)
            if input_feature is not None:
                input_feature = input_feature.unsqueeze(batch_dim)
            sample_target = sample_target.unsqueeze(0)
            if self.__input_transform is not None:
                res = self.__input_transform(
                    sample_index=sample_index,
                    sample_input=sample_input,
                    input_feature=input_feature,
                )
                match res:
                    case None:
                        pass
                    case list():
                        for new_input in res:
                            processed_indices.append(
                                new_input.get("sample_index", sample_index)
                            )
                            if "sample_input" in new_input:
                                processed_inputs.append(new_input["sample_input"])
                            else:
                                processed_inputs.append(sample_input.clone())
                            processed_features.append(
                                new_input.get("input_feature", None)
                            )
                            processed_targets.append(sample_target.clone())
                    case _:
                        raise NotImplementedError()
            else:
                processed_indices.append(sample_index)
                processed_inputs.append(sample_input)
                processed_features.append(input_feature)
                processed_targets.append(sample_target)
        if not processed_indices:
            return

        self._fetch_result()
        model_with_loss = model_executor.model_with_loss
        if model_with_loss.model.training:
            model_with_loss = model_executor.copy_model_with_loss(deepcopy=True)
        model_with_loss.model.zero_grad(set_to_none=True)
        model_with_loss.model.requires_grad_(requires_grad=False)
        model_with_loss.model.share_memory()
        worker_fun = functools.partial(
            SampleComputationHook.common_worker_fun,
            self._result_transform,
            self._get_worker_fun(),
        )
        for task in self._split_data(
            [processed_indices, processed_inputs, processed_features, processed_targets]
        ):
            self._add_task(
                worker_fun=worker_fun,
                task=(model_with_loss, *task),
            )

    def _after_forward(
        self, model_executor, inputs, input_features, targets, sample_indices, **kwargs
    ):
        self.add_task(
            model_executor=model_executor,
            sample_indices=sample_indices.tolist(),
            inputs=inputs,
            input_features=input_features,
            targets=targets,
        )

    @classmethod
    def common_worker_fun(cls, result_transform, worker_fun, task, args):
        # counter = TimeCounter()
        worker_device, worker_stream = ComputationHook._setup_cuda_device(
            args["device"]
        )
        model_with_loss, sample_indices, inputs, input_features, targets = task
        res = None

        with (torch.cuda.device(worker_device), torch.cuda.stream(worker_stream)):
            targets = tensor_to(
                targets, device=worker_device, non_blocking=True, check_pin=True
            )
            model_with_loss.to(device=worker_device, non_blocking=True)

            res = worker_fun(
                model_with_loss=model_with_loss,
                sample_indices=sample_indices,
                inputs=inputs,
                input_features=input_features,
                targets=targets,
                worker_device=worker_device,
            )
            if result_transform is not None:
                for sample_index, input_tensor, input_feature, target in zip(
                    sample_indices, inputs, input_features, targets
                ):
                    res[sample_index] = result_transform(
                        sample_index=sample_index,
                        result=res[sample_index],
                        input_tensor=input_tensor,
                        input_feature=input_feature,
                        target=target,
                    )
        for k, v in res.items():
            if isinstance(v, torch.Tensor):
                if v.numel() == 1:
                    res[k] = v.item()
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    if v2.numel() == 1:
                        v[k2] = v2.item()
        # get_logger().error("use %s ms", counter.elapsed_milliseconds())
        return res


def sample_dot_product(
    sample_index, result, input_tensor, input_feature, target, vector
):
    vector = tensor_to(vector, device=result.device, non_blocking=True)
    return result.dot(vector)

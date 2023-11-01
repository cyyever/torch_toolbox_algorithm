import copy
import functools
import os
import threading
from typing import Any, Callable

import torch
from cyy_naive_lib.data_structure.task_queue import QueueType
from cyy_naive_lib.log import get_logger
from cyy_naive_lib.time_counter import TimeCounter
from cyy_torch_toolbox.data_structure.torch_process_task_queue import \
    TorchProcessTaskQueue
from cyy_torch_toolbox.hook import Hook
from cyy_torch_toolbox.model_evaluator import ModelEvaluator
from cyy_torch_toolbox.tensor import tensor_to


class ComputationHook(Hook):
    __local_data = threading.local()

    def __init__(self, **kwargs) -> None:
        super().__init__(stripable=True, **kwargs)
        self.__result_dict: dict = {}
        self.__task_queue: TorchProcessTaskQueue | None = None
        self.__model_queue: TorchProcessTaskQueue | None = None
        self._result_transform: Callable | None = None
        self.__pending_task_cnt: int = 0
        self.__prev_tasks: list = []
        self.__result_collection_fun: Callable | None = None
        self.__shared_models: dict = {}

    def set_result_transform(self, f: Callable) -> None:
        self._result_transform = f

    def set_result_collection_fun(self, f: Callable) -> None:
        self.__result_collection_fun = f

    def _get_worker_fun(self) -> Callable:
        raise NotImplementedError()

    def _model_worker_fun(self, task, *args, **kwargs) -> Any:
        match task:
            case dict():
                self.__shared_models.clear()
                batch_index = task["batch_index"]
                if batch_index != 0:
                    task["parameter_dict"] = {
                        k: v.clone() for k, v in task["parameter_dict"].items()
                    }
                self.__shared_models[batch_index] = task
            case _:
                batch_index = task
                return self.__shared_models[batch_index]

    def reset_result(self) -> None:
        self._drop_result()
        del self.__result_dict
        self.__result_dict = {}

    @property
    def result_dict(self) -> dict:
        return self.__fetch_result()

    def has_unfetched_result(self):
        return self.__pending_task_cnt != 0

    def _drop_result(self) -> None:
        self.__fetch_result(drop=True)

    def __fetch_result(self, drop: bool = False) -> dict:
        results: dict = {}
        assert self.__pending_task_cnt >= 0
        while self.has_unfetched_result():
            assert self.__task_queue is not None
            res = self.__task_queue.get_data()
            assert res is not None
            res = res[0]
            self.__pending_task_cnt -= res[0]
            assert self.__pending_task_cnt >= 0
            if not drop:
                if self.__result_collection_fun is not None:
                    self.__result_collection_fun(res[1])
                else:
                    results |= res[1]
            else:
                del res
        self.__prev_tasks = []
        self.__result_dict |= results
        return self.__result_dict

    def _get_task_queue(self) -> TorchProcessTaskQueue:
        if self.__task_queue is None:
            worker_num: int | None | str = os.getenv("cuda_device_num", None)
            if worker_num is not None:
                worker_num = int(worker_num)
            self.__task_queue = TorchProcessTaskQueue(
                worker_num=worker_num,
                batch_process=True,
            )
            self.__task_queue.start(
                worker_fun=functools.partial(
                    self._get_worker_fun(),
                    task_queue=self.__task_queue,
                    model_queue=self.__get_model_queue(),
                ),
                worker_queue_type=QueueType.Queue,
            )
        return self.__task_queue

    def __get_model_queue(self) -> TorchProcessTaskQueue:
        if self.__model_queue is None:
            self.__model_queue = TorchProcessTaskQueue(
                worker_num=1,
            )
            self.__model_queue.start(
                worker_fun=self._model_worker_fun,
                worker_queue_type=QueueType.Queue,
            )
        return self.__model_queue

    def _add_task(self, task: Any) -> None:
        self.__prev_tasks.append(task)
        self.__pending_task_cnt += 1
        self._get_task_queue().add_task(task)

    def _broadcast_one_shot_data(
        self, batch_index: int, model_evaluator: ModelEvaluator, **kwargs
    ) -> None:
        with TimeCounter() as cnt:
            task_queue = self.__get_model_queue()
            assert batch_index >= 0
            data: dict = dict(kwargs)
            if batch_index == 0:
                data["model_evaluator"] = copy.deepcopy(model_evaluator)
                data["model_evaluator"].model.cpu()
                data["model_evaluator"].model.zero_grad(set_to_none=True)
                data["model_evaluator"].model.requires_grad_(False)
                data["model_evaluator"].model.share_memory()
            else:
                data["parameter_dict"] = model_evaluator.model_util.get_parameter_dict(
                    detach=True
                )
                for v in data["parameter_dict"].values():
                    v.grad = None
                    v.requires_grad_(False)
                    v.share_memory_()
            data["batch_index"] = batch_index
            task_queue.add_task(data)
            get_logger().debug(
                "_broadcast_one_shot_data use %s", cnt.elapsed_milliseconds()
            )

    def _before_execute(self, **_):
        self.reset()

    def __del__(self):
        self.reset()

    def release_queue(self):
        self.reset()

    def reset(self) -> None:
        assert not self.has_unfetched_result()
        self.reset_result()
        if self.__task_queue is not None:
            self.__task_queue.release()
            self.__task_queue = None
        if self.__model_queue is not None:
            self.__model_queue.release()
            self.__task_queue = None

    @classmethod
    def _setup_device(cls, advised_device) -> tuple:
        worker_device = getattr(cls.__local_data, "worker_device", None)
        if worker_device is None:
            worker_device = advised_device
            cls.__local_data.worker_device = worker_device
        if not torch.cuda.is_available():
            return worker_device, None
        worker_stream = getattr(cls.__local_data, "worker_stream", None)
        if worker_stream is None:
            worker_stream = torch.cuda.Stream(device=worker_device)
            cls.__local_data.worker_stream = worker_stream
        torch.cuda.set_device(worker_device)
        return worker_device, worker_stream

    @classmethod
    def get_cached_item(cls, name: str, value: Any, worker_device) -> Any:
        if not hasattr(cls.__local_data, name):
            value = tensor_to(
                value,
                device=worker_device,
                non_blocking=True,
            )
            setattr(cls.__local_data, name, value)
            return value
        return getattr(cls.__local_data, name)

    def _cancel_forward(self, **kwargs) -> None:
        get_logger().warning("discard results")
        self._drop_result()

    @classmethod
    def get_cached_one_shot_data(
        cls,
        batch_index: int,
        worker_device: torch.device,
        task_queue: TorchProcessTaskQueue,
        model_queue: TorchProcessTaskQueue,
        worker_id: int,
    ) -> dict:
        data = getattr(ComputationHook.__local_data, "data", {})
        if (
            hasattr(ComputationHook.__local_data, "batch_index")
            and ComputationHook.__local_data.batch_index == batch_index
        ):
            return data
        task_queue.get_worker_queue_name(worker_id)
        model_queue.add_task(batch_index)
        if data:
            data = tensor_to(data, device=worker_device, non_blocking=True)
        new_data: dict = model_queue.get_data()[0]
        get_logger().error("keys %s", new_data)

        setattr(ComputationHook.__local_data, "batch_index", batch_index)
        if "model_evaluator" in new_data:
            data["model_evaluator"] = copy.deepcopy(new_data["model_evaluator"])
            data["model_evaluator"].to(device=worker_device, non_blocking=True)
            data["parameter_dict"] = data[
                "model_evaluator"
            ].model_util.get_parameter_dict(detach=False)
        else:
            data["parameter_dict"] = tensor_to(
                new_data["parameter_dict"], device=worker_device, non_blocking=True
            )

        if data:
            setattr(ComputationHook.__local_data, "data", data)
        return data

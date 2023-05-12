import copy
import os
import threading
from typing import Any, Callable

import torch
from cyy_naive_lib.log import get_logger
# from cyy_naive_lib.profiling import Profile
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
        self._result_transform: Callable | None = None
        self.__pending_task_cnt: int = 0
        self.__prev_tasks: list = []
        self.__result_collection_fun: Callable | None = None
        self.__shared_model: None | ModelEvaluator = None

    def set_result_transform(self, f: Callable) -> None:
        self._result_transform = f

    def set_result_collection_fun(self, f: Callable) -> None:
        self.__result_collection_fun = f

    def _get_worker_fun(self) -> Callable:
        raise NotImplementedError()

    def reset_result(self) -> None:
        self.__fetch_result()
        del self.__result_dict
        self.__result_dict = {}

    @property
    def result_dict(self) -> dict:
        self.__result_dict |= self.__fetch_result()
        return self.__result_dict

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
            self.__pending_task_cnt -= res[0]
            assert self.__pending_task_cnt >= 0
            if not drop:
                if self.__result_collection_fun is not None:
                    self.__result_collection_fun(res[1])
                else:
                    results |= res[1]
        self.__prev_tasks = []
        return results

    def _get_task_queue(self) -> TorchProcessTaskQueue:
        if self.__task_queue is None:
            worker_num: int | None | str = os.getenv("cuda_device_num", None)
            if worker_num is not None:
                worker_num = int(worker_num)
            self.__task_queue = TorchProcessTaskQueue(
                worker_fun=self._get_worker_fun(),
                use_manager=False,
                worker_num=worker_num,
                use_worker_queue=True,
                batch_process=True,
            )
            self.__task_queue.start()
            torch.cuda.empty_cache()
        return self.__task_queue

    def _add_task(self, task: Any) -> None:
        self.__prev_tasks.append(task)
        self.__pending_task_cnt += 1
        self._get_task_queue().add_task(task)

    def _broadcast_one_shot_data(
        self, batch_index: int, model_evaluator, **kwargs
    ) -> None:
        with TimeCounter() as cnt:
            task_queue = self._get_task_queue()
            new_kwargs = kwargs
            if self.__shared_model is None:
                self.__shared_model = copy.deepcopy(model_evaluator)
                self.__shared_model.model.zero_grad(set_to_none=True)
                self.__shared_model.model.share_memory()
                new_kwargs |= {"model_evaluator": self.__shared_model}
            else:
                assert self.__shared_model is not None
                shared_state_dict = self.__shared_model.model.state_dict()
                for k, v in model_evaluator.model.state_dict().items():
                    shared_state_dict[k].copy_(v)

            if not new_kwargs:
                return
            for worker_id in range(task_queue.worker_num):
                worker_queue = task_queue.get_worker_queue(worker_id=worker_id)
                worker_queue.put((batch_index, new_kwargs))
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
        self.__prev_tasks = []
        if self.__task_queue is not None:
            self.__task_queue.release()
            self.__task_queue = None
        self.__shared_model = None

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

    def _after_optimizer_step(self, step_skipped: bool, **kwargs) -> None:
        if step_skipped:
            self._drop_result()

    @classmethod
    def get_cached_one_shot_data(cls, batch_index, worker_device, worker_queue) -> dict:
        data = getattr(ComputationHook.__local_data, "data", {})
        if (
            hasattr(ComputationHook.__local_data, "batch_index")
            and ComputationHook.__local_data.batch_index == batch_index
        ):
            return data
        new_data = {}
        model_evaluator = None
        while not worker_queue.empty():
            res = worker_queue.get()
            new_data = res[1]
            if "model_evaluator" in new_data:
                model_evaluator = new_data.pop("model_evaluator")
            assert res[0] <= batch_index
            if res[0] == batch_index:
                break
        setattr(ComputationHook.__local_data, "batch_index", batch_index)
        if model_evaluator is not None:
            assert next(iter(model_evaluator.model.parameters())).is_shared()
            data["model_evaluator"] = copy.copy(model_evaluator)
            data["model_evaluator"].to(device=worker_device, non_blocking=True)
            data["parameter_dict"] = data[
                "model_evaluator"
            ].model_util.get_parameter_dict(detach=False)

        if new_data:
            data |= tensor_to(new_data, device=worker_device, non_blocking=True)

        if data:
            setattr(ComputationHook.__local_data, "data", data)
        return data

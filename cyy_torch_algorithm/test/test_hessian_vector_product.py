import torch
from cyy_torch_toolbox.default_config import DefaultConfig
from hessian_vector_product import (get_hessian_vector_product_func,
                                    stop_task_queue)

# from cyy_naive_lib.time_counter import TimeCounter
# from cyy_naive_lib.profiling import Profile


def test_hessian_vector_product():
    torch.autograd.set_detect_anomaly(True)
    trainer = DefaultConfig("MNIST", "LeNet5").create_trainer()
    training_data_loader = torch.utils.data.DataLoader(
        trainer.dataset,
        batch_size=16,
        shuffle=False,
    )
    parameter_vector = trainer.model_util.get_parameter_list()
    trainer.model_util.load_parameter_list(torch.ones_like(parameter_vector))

    v = torch.ones_like(parameter_vector)
    for batch in training_data_loader:
        hvp_function = get_hessian_vector_product_func(
            trainer.copy_model_with_loss(deepcopy=False),
            batch,
            main_device=trainer.device,
        )
        a = hvp_function([v * (i + 1) for i in range(11)])
        assert len(a) == 11
        # print(a)
        assert torch.linalg.norm(a[1] - 2 * a[0], ord=2).data.item() < 0.0005
        assert torch.linalg.norm(a[2] - 3 * a[0], ord=2).data.item() < 0.0005
        assert torch.linalg.norm(a[10] - 11 * a[0], ord=2).data.item() < 0.0005
        del a

        # with TimeCounter() as c:
        #     a = hvp_function(v)
        #     print("one use time ", c.elapsed_milliseconds())
        #     print(a)
        #     c.reset_start_time()
        #     a = hvp_function([v, 2 * v])
        #     print("two use time ", c.elapsed_milliseconds())
        #     print(a)
        #     c.reset_start_time()
        #     a = hvp_function([v, 2 * v, 3 * v])
        #     print("3 use time ", c.elapsed_milliseconds())
        #     print(a)
        #     c.reset_start_time()
        #     a = hvp_function([v] * 4)
        #     print("4 use time ", c.elapsed_milliseconds())
        #     c.reset_start_time()
        #     a = hvp_function([v] * 4)
        #     print("4 use time ", c.elapsed_milliseconds())
        #     c.reset_start_time()
        #     a = hvp_function([v] * 64)
        #     print("64 use time ", c.elapsed_milliseconds())
        #     c.reset_start_time()
        #     a = hvp_function([v] * 64)
        #     print("64 use time ", c.elapsed_milliseconds())
        #     c.reset_start_time()
        #     a = hvp_function([v] * 64)
        #     print("64 use time ", c.elapsed_milliseconds())
        #     c.reset_start_time()
        #     a = hvp_function([v] * 100)
        #     print("100 use time ", c.elapsed_milliseconds())
        #     c.reset_start_time()
        #     a = hvp_function([v] * 100)
        #     print("100 use time ", c.elapsed_milliseconds())
        #     # with Profile():
        #     #     c.reset_start_time()
        #     #     a = hvp_function([v] * 100)
        #     #     print("100 use time ", c.elapsed_milliseconds())
        # del a
        break
    stop_task_queue()

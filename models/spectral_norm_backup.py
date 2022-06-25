import jittor as jt
from jittor.misc import normalize
from typing import Any, Optional, TypeVar
import jittor.nn as nn
from jittor.nn import Module


class SpectralNorm:
    # Invariant before and after each forward call:
    #   u = normalize(W @ v)
    # NB: At initialization, this invariant is not enforced

    _version: int = 1
    # At version 1:
    #   made  `W` not a buffer,
    #   added `v` as a buffer, and
    #   made eval mode use `W = u @ W_orig @ v` rather than the stored `W`.
    name: str
    dim: int
    n_power_iterations: int
    eps: float

    def __init__(self, name: str = 'weight', n_power_iterations: int = 1, dim: int = 0, eps: float = 1e-12) -> None:
        self.name = name
        self.dim = dim
        if n_power_iterations <= 0:
            raise ValueError('Expected n_power_iterations to be positive, but '
                             'got n_power_iterations={}'.format(n_power_iterations))
        self.n_power_iterations = n_power_iterations
        self.eps = eps

    def reshape_weight_to_matrix(self, weight: jt.Var) -> jt.Var:
        weight_mat = weight
        if self.dim != 0:
            # permute dim to front
            weight_mat = weight_mat.permute(self.dim,
                                            *[d for d in range(weight_mat.dim()) if d != self.dim])
        height = weight_mat.size(0)
        return weight_mat.reshape(height, -1)

    def compute_weight(self, module: Module, do_power_iteration: bool) -> jt.Var:
        # NB: If `do_power_iteration` is set, the `u` and `v` vectors are
        #     updated in power iteration **in-place**. This is very important
        #     because in `DataParallel` forward, the vectors (being buffers) are
        #     broadcast from the parallelized module to each module replica,
        #     which is a new module object created on the fly. And each replica
        #     runs its own spectral norm power iteration. So simply assigning
        #     the updated vectors to the module this function runs on will cause
        #     the update to be lost forever. And the next time the parallelized
        #     module is replicated, the same randomly initialized vectors are
        #     broadcast and used!
        #
        #     Therefore, to make the change propagate back, we rely on two
        #     important behaviors (also enforced via tests):
        #       1. `DataParallel` doesn't clone storage if the broadcast tensor
        #          is already on correct device; and it makes sure that the
        #          parallelized module is already on `device[0]`.
        #       2. If the out tensor in `out=` kwarg has correct shape, it will
        #          just fill in the values.
        #     Therefore, since the same power iteration is performed on all
        #     devices, simply updating the tensors in-place will make sure that
        #     the module replica on `device[0]` will update the _u vector on the
        #     parallized module (by shared storage).
        #
        #    However, after we update `u` and `v` in-place, we need to **clone**
        #    them before using them to normalize the weight. This is to support
        #    backproping through two forward passes, e.g., the common pattern in
        #    GAN training: loss = D(real) - D(fake). Otherwise, engine will
        #    complain that variables needed to do backward for the first forward
        #    (i.e., the `u` and `v` vectors) are changed in the second forward.
        weight = getattr(module, self.name + '_orig')
        u = getattr(module, self.name + '_u')
        v = getattr(module, self.name + '_v')
        weight_mat = self.reshape_weight_to_matrix(weight)

        if do_power_iteration:
            with jt.no_grad():
                for _ in range(self.n_power_iterations):
                    # Spectral norm of weight equals to `u^T W v`, where `u` and `v`
                    # are the first left and right singular vectors.
                    # This power iteration produces approximations of `u` and `v`.
                    v = normalize(nn.matmul(weight_mat.t(), u), dim=0, eps=self.eps)
                    u = normalize(nn.matmul(weight_mat, v), dim=0, eps=self.eps)
                if self.n_power_iterations > 0:
                    # See above on why we need to clone
                    u = u.clone()
                    v = v.clone()

        sigma = jt.matmul(u, jt.matmul(weight_mat, v))
        weight = weight / sigma
        return weight

    def remove(self, module: Module) -> None:
        with jt.no_grad():
            weight = self.compute_weight(module, do_power_iteration=False)
        delattr(module, self.name)
        delattr(module, self.name + '_u')
        delattr(module, self.name + '_v')
        delattr(module, self.name + '_orig')
        # module.register_parameter(self.name, torch.nn.Parameter(weight.detach()))
        # 计图中detach会把requires_grad变为True，且返回的值与输入参数的存储空间不同。
        # 与torch刚好相反：从True变为False，且存储空间相同
        setattr(module, self.name, weight.detach_inplace())

    def __call__(self, module: Module, inputs: Any) -> None:
        # self.compute_weight(module, do_power_iteration=module.is_training())
        setattr(module, self.name, self.compute_weight(module, do_power_iteration=module.is_train))

    def _solve_v_and_rescale(self, weight_mat, u, target_sigma):
        # Tries to returns a vector `v` s.t. `u = normalize(W @ v)`
        # (the invariant at top of this class) and `u @ W @ v = sigma`.
        # This uses pinverse in case W^T W is not invertible.
        v = jt.matmul(jt.matmul(weight_mat.t().mm(weight_mat).pinverse(), jt.matmul(weight_mat.t(), u.unsqueeze(1)))).squeeze(1)
        return v.mul_(target_sigma / jt.matmul(u, jt.matmul(weight_mat, v)))

    @staticmethod
    def apply(module: Module, name: str, n_power_iterations: int, dim: int, eps: float) -> 'SpectralNorm':
        # for k, hook in module._forward_pre_hooks.items():
        #     if isinstance(hook, SpectralNorm) and hook.name == name:
        #         raise RuntimeError("Cannot register two spectral_norm hooks on "
        #                            "the same parameter {}".format(name))

        fn = SpectralNorm(name, n_power_iterations, dim, eps)
        weight = module._parameters[name]
        if weight is None:
            raise ValueError(f'`SpectralNorm` cannot be applied as parameter `{name}` is None')
 
        with jt.no_grad():
            weight_mat = fn.reshape_weight_to_matrix(weight)
            h, w = weight_mat.size()
            # randomly initialize `u` and `v`
            u = normalize(jt.randn([h]), dim=0, eps=fn.eps)
            v = normalize(jt.randn([w]), dim=0, eps=fn.eps)

        delattr(module, fn.name)
        # module.register_parameter(fn.name + "_orig", weight)
        setattr(module, fn.name + "_orig", weight)

        # We still need to assign weight back as fn.name because all sorts of
        # things may assume that it exists, e.g., when initializing weights.
        # However, we can't directly assign as it could be an nn.Parameter and
        # gets added as a parameter. Instead, we register weight.data as a plain
        # attribute.
        # setattr(module, fn.name, weight.data)
        # 不能stop_grad，由于指向同一个Var，weight.stop_grad将weight_orig也设置为requires_grad=False了
        # setattr(module, fn.name, weight.stop_grad())
        setattr(module, fn.name, weight)

        # module.register_buffer(fn.name + "_u", u)
        # module.register_buffer(fn.name + "_v", v)
        setattr(module, fn.name + "_u", u)
        setattr(module, fn.name + "_v", v)

        # module.register_forward_pre_hook(fn)
        module.register_pre_forward_hook(fn)
        return fn
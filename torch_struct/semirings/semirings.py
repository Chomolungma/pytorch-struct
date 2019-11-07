import torch
import torch.distributions
import numpy as np
from pytorch_memlab import MemReporter
from .sparse import *

class Semiring:
    """
    Base semiring class.

    Based on description in:

    * Semiring parsing :cite:`goodman1999semiring`

    """

    @classmethod
    def size(cls):
        "Additional *ssize* first dimension needed."
        return 1

    @classmethod
    def dot(cls, *ls):
        "Dot product along last dim."
        return cls.sum(cls.times(*ls))

    # @classmethod
    # def banded_dot(cls, a, b, band, offset_a, offset_b):
    #     return sparse_banded_combine(a, b, band, offset_a, offset_b,
    #                                  semiring=cls,fn=cls.dot)

    @classmethod
    def banded_dot2(cls, a, b, band, offset_a, offset_b):
        return sparse_banded_combine2(a, b, band,
                                      offset_a, offset_b,
                                      semiring=cls,fn=cls.dot)


    @classmethod
    def times(cls, *ls):
        "Multiply a list of tensors together"
        cur = ls[0]
        for l in ls[1:]:
            cur = cls.mul(cur, l)
        return cur

    @classmethod
    def convert(cls, potentials):
        "Convert to semiring by adding an extra first dimension."
        return potentials.unsqueeze(0)

    @classmethod
    def unconvert(cls, potentials):
        "Unconvert from semiring by removing extra first dimension."
        return potentials.squeeze(0)

    @staticmethod
    def zero_(xs):
        "Fill *ssize x ...* tensor with additive identity."
        raise NotImplementedError()

    @staticmethod
    def one_(xs):
        "Fill *ssize x ...* tensor with multiplicative identity."
        raise NotImplementedError()

    @staticmethod
    def sum(xs, dim=-1):
        "Sum over *dim* of tensor."
        raise NotImplementedError()

    @classmethod
    def plus(cls, a, b):
        return cls.sum(torch.stack([a, b], dim=-1))

    dg = False


class _Base(Semiring):
    Log = False
    @staticmethod
    def mul(a, b):
        return torch.mul(a, b)

    @staticmethod
    def prod(a, dim=-1):
        return torch.prod(a, dim=dim)

    @staticmethod
    def zero_(xs):
        return xs.fill_(0)

    @staticmethod
    def one_(xs):
        return xs.fill_(1)


class _BaseLog(Semiring):
    Log = True

    @staticmethod
    def mul(a, b):
        return a + b

    @staticmethod
    def zero_(xs):
        return xs.fill_(-1e5)

    @staticmethod
    def one_(xs):
        return xs.fill_(0.0)

    @staticmethod
    def prod(a, dim=-1):
        return torch.sum(a, dim=dim)


class StdSemiring(_Base):
    """
    Implements the counting semiring (+, *, 0, 1).

    """

    @staticmethod
    def sum(xs, dim=-1):
        return torch.sum(xs, dim=dim)


class LogSemiring(_BaseLog):
    """
    Implements the log-space semiring (logsumexp, +, -inf, 0).

    Gradients give marginals.
    """

    dg = True

    @staticmethod
    def sum(xs, dim=-1):
        return torch.logsumexp(xs, dim=dim)

    @classmethod
    def dot_grad(cls, a, b):
        "Dot product along last dim."
        c = a + b
        part = torch.logsumexp(c, dim=-1)
        return part, (c - part.unsqueeze(-1)).exp()


class MaxSemiring(_BaseLog):
    """
    Implements the max semiring (max, +, -inf, 0).

    Gradients give argmax.
    """

    dg = True

    @staticmethod
    def sum(xs, dim=-1):
        return torch.max(xs, dim=dim)[0]

    @classmethod
    def dot_grad(cls, a, b):
        "Dot product along last dim."
        c = a + b
        part, argmax = torch.max(c, dim=-1)
        return part, torch.nn.functional.one_hot(argmax, a.shape[-1])

    @staticmethod
    def sparse_sum(xs, dim=-1):
        m, a = torch.max(xs, dim=dim)
        return m, (torch.zeros(a.shape).long(), a)

def TempMax(alpha):
    pass

def KMaxSemiring(k):
    """
    Implements the k-max semiring (kmax, +, [-inf, -inf..], [0, -inf, ...]).

    Gradients give k-argmax.
    """

    class KMaxSemiring(_BaseLog):
        @staticmethod
        def size():
            return k

        @classmethod
        def convert(cls, orig_potentials):
            potentials = torch.zeros(
                (k,) + orig_potentials.shape,
                dtype=orig_potentials.dtype,
                device=orig_potentials.device,
            )
            cls.zero_(potentials)
            potentials[0] = orig_potentials
            return potentials

        @classmethod
        def one_(cls, xs):
            cls.zero_(xs)
            xs[0].fill_(0)
            return xs

        @staticmethod
        def unconvert(potentials):
            return potentials[0]

        @staticmethod
        def sum(xs, dim=-1):
            if dim == -1:
                xs = xs.permute(tuple(range(1, xs.dim())) + (0,))
                xs = xs.contiguous().view(xs.shape[:-2] + (-1,))
                xs = torch.topk(xs, k, dim=-1)[0]
                xs = xs.permute((xs.dim() - 1,) + tuple(range(0, xs.dim() - 1)))
                assert xs.shape[0] == k
                return xs
            assert False

        @staticmethod
        def sparse_sum(xs, dim=-1):
            if dim == -1:
                xs = xs.permute(tuple(range(1, xs.dim())) + (0,))
                xs = xs.contiguous().view(xs.shape[:-2] + (-1,))
                xs, xs2 = torch.topk(xs, k, dim=-1)
                xs = xs.permute((xs.dim() - 1,) + tuple(range(0, xs.dim() - 1)))
                xs2 = xs2.permute((xs.dim() - 1,) + tuple(range(0, xs.dim() - 1)))
                assert xs.shape[0] == k
                return xs, (xs2 % k, xs2 // k)
            assert False

        @staticmethod
        def mul(a, b):
            a = a.view((k, 1) + a.shape[1:])
            b = b.view((1, k) + b.shape[1:])
            c = a + b
            c = c.contiguous().view((k * k,) + c.shape[2:])
            ret = torch.topk(c, k, 0)[0]
            assert ret.shape[0] == k
            return ret

    return KMaxSemiring


class EntropySemiring(Semiring):
    """
    Implements an entropy expectation semiring.

    Computes both the log-values and the running distributional entropy.

    Based on descriptions in:

    * Parameter estimation for probabilistic finite-state transducers :cite:`eisner2002parameter`
    * First-and second-order expectation semirings with applications to minimum-risk training on translation forests :cite:`li2009first`
    """

    @staticmethod
    def size():
        return 2

    @staticmethod
    def convert(xs):
        values = torch.zeros((2,) + xs.shape).type_as(xs)
        values[0] = xs
        values[1] = 0
        return values

    @staticmethod
    def unconvert(xs):
        return xs[1]

    @staticmethod
    def sum(xs, dim=-1):
        assert dim != 0
        d = dim - 1 if dim > 0 else dim
        part = torch.logsumexp(xs[0], dim=d)
        log_sm = xs[0] - part.unsqueeze(d)
        sm = log_sm.exp()
        return torch.stack((part, torch.sum(xs[1].mul(sm) - log_sm.mul(sm), dim=d)))

    @staticmethod
    def mul(a, b):
        return torch.stack((a[0] + b[0], a[1] + b[1]))

    @classmethod
    def prod(cls, xs, dim=-1):
        return xs.sum(dim)

    @staticmethod
    def zero_(xs):
        xs[0].fill_(-1e5)
        xs[1].fill_(0)
        return xs

    @staticmethod
    def one_(xs):
        xs[0].fill_(0)
        xs[1].fill_(0)
        return xs


def back(x):
    x.backward()

import torch
from .helpers import _Struct
from .semirings import LogSemiring
import math
# from pytorch_memlab import MemReporter


def pad_conv(x, k, dim, sr, extra_b=0, extra_t=0):
    return pad(x, (k - 1) // 2 + extra_b, (k - 1) // 2 + extra_t, dim, sr).unfold(
        dim, k, 1
    )


def pad(x, n_bot, n_top, dim, sr):
    shape = list(x.shape)
    base = sr.zero_(torch.zeros([1] * len(shape), dtype=x.dtype, device=x.device))

    shape[dim] = n_bot
    padb = base.expand(shape)
    if n_top == n_bot:
        padt = padb
    else:
        shape[dim] = n_top
        padt = base.expand(shape)

    return torch.cat([padb, x, padt], dim=dim)


def demote(x, index):
    total = x.dim()
    order = tuple(range(index)) + tuple(range(index + 1, total)) + (index,)
    return x.permute(order)


class Alignment(_Struct):
    def __init__(self, semiring=LogSemiring, local=False, max_gap=None,
                 _custom_grad=False):
        self.semiring = semiring
        self.local = local
        self.max_gap = max_gap
        self._custom_grad = _custom_grad

    def _check_potentials(self, edge, lengths=None):
        batch, N_1, M_1, x = edge.shape
        assert x == 3
        if self.local:
            assert (edge[..., 0] <= 0).all(), "skips must be negative"
            assert (edge[..., 1] >= 0).all(), "alignment must be positive"
            assert (edge[..., 2] <= 0).all(), "skips must be negative"
        edge = self.semiring.convert(edge)
        N = N_1
        M = M_1
        if lengths is None:
            lengths = torch.LongTensor([N] * batch)

        assert max(lengths) <= N, "Length longer than edge scores"
        assert max(lengths) == N, "One length must be at least N"
        return edge, batch, N, M, lengths

    def _dp(self, log_potentials, lengths=None, force_grad=False):
        return self._dp_scan(log_potentials, lengths, force_grad)

    def _dp_scan(self, log_potentials, lengths=None, force_grad=False):
        "Compute forward pass by linear scan"
        # Setup
        semiring = self.semiring
        log_potentials.requires_grad_(True)
        ssize = semiring.size()
        log_potentials, batch, N, M, lengths = self._check_potentials(
            log_potentials, lengths
        )
        assert self.max_gap is None or self.max_gap > abs(N - M)

        steps = N + M
        log_MN = int(math.ceil(math.log(steps, 2)))
        bin_MN = int(math.pow(2, log_MN))

        Down, Mid, Up = 0, 1, 2
        Open, Close = 0, 1
        LOC = 2 if self.local else 1

        # Grid
        grid_x = torch.arange(N).view(N, 1).expand(N, M)
        grid_y = torch.arange(M).view(1, M).expand(N, M)
        rot_x = grid_x + grid_y
        rot_y = grid_y - grid_x + N

        # Helpers
        ind = torch.arange(bin_MN)
        ind_M = ind
        ind_U = torch.arange(1, bin_MN)
        ind_D = torch.arange(bin_MN - 1)

        charta = [
            self._make_chart(
                1,
                (
                    batch,
                    bin_MN // pow(2, i),
                    2 * bin_MN // pow(2, log_MN - i) - 1,
                    bin_MN,
                    LOC,
                    LOC,
                    3,
                ),
                log_potentials,
                force_grad,
            )[0]
            if i <= 1
            else None
            for i in range(log_MN + 1)
        ]

        chartb = [
            self._make_chart(
                1,
                (
                    batch,
                    bin_MN // pow(2, i),
                    bin_MN,
                    2 * bin_MN // pow(2, log_MN - i) - 1,
                    LOC,
                    LOC,
                    3,
                ),
                log_potentials,
                force_grad,
            )[0]
            if i <= 1
            else None
            for i in range(log_MN + 1)
        ]

        def reflect(x, size):
            ex = x.shape[3]
            f, r = torch.arange(ex), torch.arange(ex - 1, -1, -1)
            sp = pad_conv(x, ex, 4, semiring)
            sp.view(ssize, batch, size, ex, bin_MN, LOC, LOC, 3, ex)
            sp = (
                sp[:, :, :, r, :, :, :, :, f]
                .permute(1, 2, 3, 4, 0, 5, 6, 7)
                .view(ssize, batch, size, bin_MN, ex, LOC, LOC, 3)
            )
            return sp

        # Init
        # This part is complicated. Rotate the scores by 45% and
        # then compress one.

        for b in range(lengths.shape[0]):
            end = lengths[b]
            point = (end + M) // 2
            lim = point * 2

            charta[0][:, b, rot_x[:lim], 0, rot_y[:lim], :, :, :] = (
                log_potentials[:, b, :lim].unsqueeze(-2).unsqueeze(-2)
            )
            chartb[0][:, b, rot_x[:lim], rot_y[:lim], 0, :, :, :] = (
                log_potentials[:, b, :lim].unsqueeze(-2).unsqueeze(-2)
            )

            charta[1][:, b, point:, 1, ind, :, :, Mid] = semiring.one_(
                charta[1][:, b, point:, 1, ind, :, :, Mid]
            )

        for b in range(lengths.shape[0]):
            end = lengths[b]
            point = (end + M) // 2
            lim = point * 2

            left2_ = charta[0][:, b, 0:lim:2]
            right2 = chartb[0][:, b, 1:lim:2]

            charta[1][:, b, :point, 1, ind_M, :, :, :] = torch.stack(
                [
                    left2_[:, :, 0, ind_M, :, :, Down],
                    semiring.plus(
                        left2_[:, :, 0, ind_M, :, :, Mid],
                        right2[:, :, ind_M, 0, :, :, Mid],
                    ),
                    left2_[:, :, 0, ind_M, :, :, Up],
                ],
                dim=-1,
            )

            y = torch.stack([ind_D, ind_U], dim=0)
            z = y.clone()
            z[0, :] = 2
            z[1, :] = 0

            z2 = y.clone()
            z2[0, :] = 0
            z2[1, :] = 2

            tmp = torch.stack(
                [
                    semiring.times(
                        left2_[:, :, 0, ind_D, Open : Open + 1 :, :],
                        right2[:, :, ind_U, 0, :, Open : Open + 1, Down : Down + 1],
                    ),
                    semiring.times(
                        left2_[:, :, 0, ind_U, Open : Open + 1, :, :],
                        right2[:, :, ind_D, 0, :, Open : Open + 1, Up : Up + 1],
                    ),
                ],
                dim=2,
            )
            charta[1][:, b, :point, z, y, :, :, :] = tmp

        charta[1] = charta[1][:, :, :, :3]
        chartb[1] = reflect(charta[1], bin_MN // 2)

        if self._custom_grad and semiring.dg:
            class Merge(torch.autograd.Function):
                @staticmethod
                def forward(ctx, left, right, rsize, nrsize):
                    st = []
                    grads = []
                    for op in (Up, Down, Mid):
                        top, bot = rsize + 1, 1
                        if op == Up:
                            top, bot = rsize + 2, 2
                        if op == Down:
                            top, bot = rsize, 0

                        combine, grad = semiring.dot_grad(
                            left[:, :, :, :, :, Open, :, :, :, bot:top],
                            right[:, :, :, :, :, Open, :, :, op, :, :],
                        )


                        combine = combine.view(
                            ssize, batch, size, bin_MN, LOC, LOC, 3, nrsize
                        ).permute(0, 1, 2, 7, 3, 4, 5, 6)

                        grad = grad.view(
                            ssize, batch, size, bin_MN, LOC, LOC, 3, nrsize, rsize
                        ).permute(0, 1, 2, 7, 3, 4, 5, 6, 8)
                        
                        grads.append(grad)
                        st.append(combine)
                    ctx.save_for_backward(torch.stack(grads, dim=-1),
                                          torch.tensor(left.shape),
                                          torch.tensor(right.shape),
                                          torch.tensor([rsize, nrsize]))
                    return torch.stack(st, dim=-1)


                @staticmethod
                def backward(ctx, grad_output):
                    grad, ls, rs, v = ctx.saved_tensors
                    rsize, nrsize = v.tolist()
                    grad_in = grad.mul(grad_output.unsqueeze(-2))
                    left = torch.zeros(*ls.tolist(),
                                       dtype=grad_output.dtype, device=grad_output.device)
                    right = torch.zeros(*rs.tolist(),
                                        dtype=grad_output.dtype, device=grad_output.device)
                    # grad_in = grad_in.permute(0, 1, 2, 7, 3, 4, 5, 6, 8)
                    grad_in = grad_in.permute(9, 0, 1, 2, 4, 5, 6, 7, 3, 8)
                    for i, op in enumerate((Up, Down, Mid)):
                        top, bot = rsize + 1, 1
                        if op == Up:
                            top, bot = rsize + 2, 2
                        if op == Down:
                            top, bot = rsize, 0
                            
                        left[:, :, :, :, :, Open, :, :, :, bot:top] += grad_in[i]
                        right[:, :, :, :, :, Open, :, 0, op, :, :] += grad_in[i].sum(-3)
                    return left, right, None, None, None

            merge = Merge.apply
        else:
            def merge(left, right, rsize, nrsize):
                st = []
                for op in (Up, Down, Mid):
                    top, bot = rsize + 1, 1
                    if op == Up:
                        top, bot = rsize + 2, 2
                    if op == Down:
                        top, bot = rsize, 0

                    combine = semiring.dot(
                        left[:, :, :, :, :, Open, :, :, :, bot:top],
                        right[:, :, :, :, :, Open, :, :, op, :, :],
                    )

                    combine = combine.view(
                        ssize, batch, size, bin_MN, LOC, LOC, 3, nrsize
                    ).permute(0, 1, 2, 7, 3, 4, 5, 6)
                    st.append(combine)
                return torch.stack(st, dim=-1)
    
        # Scan
        def merge2(xa, xb, size, rsize):
            nrsize = (rsize - 1) * 2 + 3
            rsize += 2
            st = []
            left = (
                pad_conv(
                    demote(xa[:, :, 0 : size * 2 : 2, :], 3), nrsize, 7, semiring, 2, 2
                )
                .transpose(-1, -2)
                .view(ssize, batch, size, bin_MN, 1, LOC, LOC, 3, nrsize, rsize + 2)
            )

            right = (
                pad(
                    pad_conv(
                        demote(xb[:, :, 1 : size * 2 : 2, :, :], 4), nrsize, 3, semiring
                    ),
                    1,
                    1,
                    -2,
                    semiring,
                )
                .transpose(-1, -2)
                .view(ssize, batch, size, bin_MN, LOC, 1, LOC, 1, 3, nrsize, rsize)
            )
            
            st = merge(left, right, rsize, nrsize)
                
            if self.local:
                left_ = pad(
                    xa[:, :, 0::2, :, :, Close, :, :],
                    rsize // 2,
                    rsize // 2,
                    3,
                    semiring,
                )
                right = pad(
                    xa[:, :, 1::2, :, :, :, Close, :],
                    rsize // 2,
                    rsize // 2,
                    3,
                    semiring,
                )
                st2 = []
                st2.append(torch.stack([semiring.zero_(left_.clone()), left_], dim=-3))
                st2.append(torch.stack([semiring.zero_(right.clone()), right], dim=-2))
                st = torch.cat([st, torch.stack(st2, dim=-1)], dim=-1)
            return semiring.sum(st)

        size = bin_MN // 2
        rsize = 2
        for n in range(2, log_MN + 1):
            size = int(size / 2)
            rsize *= 2
            q = merge2(charta[n - 1], chartb[n - 1], size, charta[n - 1].shape[3])
            charta[n] = q
            gap = charta[n].shape[3]
            if self.max_gap is not None and (gap - 1) // 2 > self.max_gap:
                reduced = (gap - 1) // 2 - self.max_gap
                charta[n] = charta[n][:, :, :, reduced:-reduced]
                chartb[n] = reflect(charta[n], size)
            else:
                chartb[n] = reflect(q, size)

        if self.local:
            v = semiring.sum(semiring.sum(charta[-1][:, :, 0, :, :, Close, Close, Mid]))
        else:
            v = charta[-1][
                :, :, 0, M - N + (charta[-1].shape[3] // 2), N, Open, Open, Mid
            ]

        # reporter = MemReporter()
        # reporter.report()
        return v, [log_potentials], None

    @staticmethod
    def _rand(min_n=2):
        b = torch.randint(2, 3, (1,))
        N = torch.randint(min_n, 6, (1,))
        M = torch.randint(min_n, 6, (1,))
        return torch.rand(b, N, M, 3), (b.item(), (N).item())

    def enumerate(self, edge, lengths=None):
        semiring = self.semiring
        edge, batch, N, M, lengths = self._check_potentials(edge, lengths)
        d = {}
        d[0, 0] = [([(0, 0, 1)], edge[:, :, 0, 0, 1])]
        # enum_lengths = torch.LongTensor(lengths.shape)
        if self.local:
            for i in range(N):
                for j in range(M):
                    d.setdefault((i, j), [])
                    d[i, j].append(([(i, j, 1)], edge[:, :, i, j, 1]))

        for i in range(N):
            for j in range(M):
                d.setdefault((i + 1, j + 1), [])
                d.setdefault((i, j + 1), [])
                d.setdefault((i + 1, j), [])
                for chain, score in d[i, j]:
                    if i + 1 < N and j + 1 < M:
                        d[i + 1, j + 1].append(
                            (
                                chain + [(i + 1, j + 1, 1)],
                                semiring.mul(score, edge[:, :, i + 1, j + 1, 1]),
                            )
                        )
                    if i + 1 < N:

                        d[i + 1, j].append(
                            (
                                chain + [(i + 1, j, 2)],
                                semiring.mul(score, edge[:, :, i + 1, j, 2]),
                            )
                        )
                    if j + 1 < M:
                        d[i, j + 1].append(
                            (
                                chain + [(i, j + 1, 0)],
                                semiring.mul(score, edge[:, :, i, j + 1, 0]),
                            )
                        )
        if self.local:
            positions = [x[0] for i in range(N) for j in range(M) for x in d[i, j]]
            all_val = torch.stack(
                [x[1] for i in range(N) for j in range(M) for x in d[i, j]], dim=-1
            )
            _, ind = all_val.max(dim=-1)
            print(positions[ind[0, 0]])
        else:
            all_val = torch.stack([x[1] for x in d[N - 1, M - 1]], dim=-1)

        return semiring.unconvert(semiring.sum(all_val)), None

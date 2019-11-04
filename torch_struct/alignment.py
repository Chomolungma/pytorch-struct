import torch
from .helpers import _Struct
import math
from .sparse import *
from pytorch_memlab import MemReporter

class Alignment(_Struct):
    def _check_potentials(self, edge, lengths=None):
        batch, N_1, M_1, x = edge.shape
        assert x == 3
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
        steps = N + M
        log_MN = int(math.ceil(math.log(steps, 2)))
        bin_MN = int(math.pow(2, log_MN))

        Down, Mid, Up = 0, 1, 2
        Open, Close = 0, 1
        # Create a chart N, N, back


        chart = [None for i in range(log_MN + 1)]
        charta = [None for i in range(log_MN + 1)]
        chartb = [None for i in range(log_MN + 1)]
        charta[0] = self._make_chart(
            1, (batch, bin_MN, 1, bin_MN,  2, 2, 3), log_potentials, force_grad
        )[0]
        chartb[0] = self._make_chart(
            1, (batch, bin_MN, bin_MN, 1,  2, 2, 3), log_potentials, force_grad
        )[0]

        charta[1] = self._make_chart(
            1, (batch, bin_MN // 2, 3, bin_MN, 2, 2, 3), log_potentials, force_grad
        )[0]

        # Init
        # This part is complicated. Rotate the scores by 45% and
        # then compress one.
        grid_x = torch.arange(N).view(N, 1).expand(N, M)
        grid_y = torch.arange(M).view(1, M).expand(N, M)
        rot_x = grid_x + grid_y
        rot_y = grid_y - grid_x + N
        ind = torch.arange(bin_MN)
        ind_M = ind
        ind_U = torch.arange(1, bin_MN)
        ind_D = torch.arange(bin_MN - 1)

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


        # for b in range(lengths.shape[0]):
        #     end = lengths[b]
        #     # Add path to end.
        #     point = (end + M) // 2
        #     point = (end + M) // 2
        #     lim = point * 2
        #     chart[1][:, b, point : bin_MN // 2, ind, ind, Mid] = semiring.one_(
        #         chart[1][:, b, point : bin_MN // 2, ind, ind, Mid]
        #     )
        #     chart[0][
        #         :, b, rot_x[: end + M], rot_y[:lim], rot_y[:lim], :
        #     ] = log_potentials[:, b, : end + M]

        # for b in range(lengths.shape[0]):
        #     end = lengths[b]
        #     point = (end + M) // 2
        #     lim = point * 2
        #     chart[1][:, b, :point, ind_M, ind_M, :] = torch.stack(
        #         [
        #             chart[0][:, b, :lim:2, ind_M, ind_M, Down],
        #             semiring.sum(
        #                 torch.stack(
        #                     [
        #                         chart[0][:, b, :lim:2, ind_M, ind_M, Mid],
        #                         chart[0][:, b, 1:lim:2, ind_M, ind_M, Mid],
        #                     ],
        #                     dim=-1,
        #                 )
        #             ),
        #             chart[0][:, b, :lim:2, ind_M, ind_M, Up],
        #         ],
        #         dim=-1,
        #     )

        #     x = torch.stack([ind_U, ind_D], dim=0)
        #     y = torch.stack([ind_D, ind_U], dim=0)
        #     q = torch.stack(
        #         [
        #             semiring.times(
        #                 chart[0][:, b, :lim:2, ind_D, ind_D, :],
        #                 chart[0][:, b, 1:lim:2, ind_U, ind_U, Down : Down + 1],
        #             ),
        #             semiring.times(
        #                 chart[0][:, b, :lim:2, ind_U, ind_U, :],
        #                 chart[0][:, b, 1:lim:2, ind_D, ind_D, Up : Up + 1],
        #             ),
        #         ],
        #         dim=2,
        #     )

        #     chart[1][:, b, :point, x, y, :] = q


        # print(chart[1])
        # print(charta[1])
        # Scan

        def merge(x, size, sparse=False):

            tsize = x.shape[3]
            inner = bin_MN
            if sparse:
                inner = tsize

            left = (
                x[:, :, 0 : size * 2 : 2]
                .permute(0, 1, 2, 5, 4, 3)
                .view(ssize, batch, size, 3, bin_MN,  inner)
            )
            right = (
                x[:, :, 1 : size * 2 : 2]
                .permute(0, 1, 2, 5, 4, 3)
                .view(ssize, batch, size, 1, 3, bin_MN, inner)
            )
            st = []
            for op in (Mid,Up, Down):
                a, b, c, d = 0, bin_MN, 0, bin_MN
                if op == Up:
                    a, b, c, d = 1, bin_MN, 0, bin_MN - 1
                if op == Down:
                    a, b, c, d = 0, bin_MN - 1, 1, bin_MN

                if not sparse:
                    # print(op, a, b)
                    # sp_left = dense_to_sparse(left, tsize, semiring=semiring)
                    # sp_right = dense_to_sparse(right, tsize, semiring=semiring)
                    # print("d r", sp_right[0,0,0,0])
                    # print("d l", sp_left[0,0,0,0])

                    v = semiring.dot(left[..., a:b].unsqueeze(-2),
                                     right.transpose(-2, -1)[..., op, :,  c:d].unsqueeze(-3))
                    # v4 = dense_to_sparse(v, rsize, semiring=semiring)
                    # v2 = semiring.banded_dot2(
                    #     sp_left,
                    #     sp_right[..., op,  :, :],
                    #     tsize, c, a)
                    # print("sizes", v2.shape, tsize, rsize)
                    # v3 = sparse_to_dense(v2, semiring=semiring)
                    # print(v2[0,0,0,0].exp(), v4[0,0,0,0].exp())

                    # assert torch.isclose(v2, v4).all(), \
                    #     "%s %s %s %s %s\n%s\n%s"%(a, c, tsize, v2[0,0,0,0], v4[0,0,0,0], left[0,0,0,0], right[0,0,0,0])
                    # assert torch.isclose(v, v3).all()
                    # print("succ")
                else:
                    # print("s r", flip(right[0,0,0,0], inner, semiring=semiring))
                    # print("s l", flip(left[0,0,0,0], inner, semiring=semiring))

                    v = semiring.banded_dot2(
                        flip(left[..., :], tsize, semiring=semiring),
                        flip(right[..., op,  :, :], tsize, semiring=semiring),#.transpose(-2, -1),
                        tsize, c, a)
                    rsize = v.shape[-1]
                    v = flip(v, rsize, semiring=semiring)
                    # print("final dot", v[0,0,0,0, :])
                if sparse:
                    # print(v.shape)
                    v = v.view(ssize, batch, size, 3, bin_MN, rsize) \
                        .permute(0, 1, 2, 5, 4, 3) \
                        .view(ssize, batch, size, rsize, bin_MN, 3)
                else:
                    v = v.view(ssize, batch, size, 3, bin_MN, bin_MN) \
                        .permute(0, 1, 2, 5, 4, 3)

                st.append(v)
            st = torch.stack(st, dim=-1)
            return semiring.sum(st)

        size = bin_MN // 2
        chart[1] = charta[1][..., 0, 0, :]
        # c = chart[1].view(ssize, batch, size, rsize+1, bin_MN, 3) \
        #          .permute(0, 1, 2, 5, 4, 3)  \
        #          .view(ssize, batch, size, 3, bin_MN, rsize+1)

        # c = flip(c, 3, semiring=semiring)

        # chart[1] = c.view(ssize, batch, size, 3, bin_MN, rsize+1) \
        #          .permute(0, 1, 2, 5, 4, 3)

        sparse = True

        def convert(chart, size):
            rsize = chart.shape[3]
            c = chart.view(ssize, batch, size, rsize, bin_MN, 3) \
                          .permute(0, 1, 2, 5, 4, 3)  \
                          .view(ssize, batch, size, 3, bin_MN, rsize)
            c2 = sparse_to_dense(c, semiring=semiring)
            return c2.view(ssize, batch, size, 3, bin_MN, bin_MN) \
                     .permute(0, 1, 2, 4, 5, 3) \
                     .view(ssize, batch, size, bin_MN, bin_MN, 3)

        for n in range(2, log_MN + 1):
            if n == min(3, log_MN):
                # charta[n-1] = convert(chart[n-1], size)
                # # print("covert")
                chart[n-1] = convert(chart[n-1], size)
                sparse = False
            size = int(size / 2)

            chart[n] = merge(chart[n - 1], size, sparse=sparse)

            # charta[n] = merge(charta[n - 1], size,  sparse=False)
            # chart[n] = merge(chart[n - 1], size, sparse=True)

            # back = convert(chart[n], size)
            # assert charta[n].shape == back.shape

            # print("a", charta[n][0,0,0,:, :, 1])
            # print("c", chart[n][0,0,0,:, :, 1])
            # print("b", back[0,0,0,:, :, 1])
            # assert torch.isclose(charta[n], back).all()
            # print("Success")

        # reporter = MemReporter()
        # reporter.report()
        print("answer",chart[-1][:, :, 0, :, :, Mid])
        print("return", M, N, chart[-1][:, :, 0, M, N, Mid])
        v = chart[-1][:, :, 0, M, N, Mid]
        return v, [log_potentials], None

    @staticmethod
    def _rand(min_n=2):
        b = torch.randint(2, 4, (1,))
        N = torch.randint(min_n, 4, (1,))
        M = torch.randint(min_n, 4, (1,))
        return torch.rand(b, N, M, 3), (b.item(), (N).item())

    def enumerate(self, edge, lengths=None):
        semiring = self.semiring
        edge, batch, N, M, lengths = self._check_potentials(edge, lengths)
        d = {}
        d[0, 0] = [([(0, 0)], edge[:, :, 0, 0, 1])]
        # enum_lengths = torch.LongTensor(lengths.shape)
        for i in range(N):
            for j in range(M):
                d.setdefault((i + 1, j + 1), [])
                d.setdefault((i, j + 1), [])
                d.setdefault((i + 1, j), [])
                for chain, score in d[i, j]:
                    if i + 1 < N and j + 1 < M:
                        d[i + 1, j + 1].append(
                            (
                                chain + [(i + 1, j + 1)],
                                semiring.mul(score, edge[:, :, i + 1, j + 1, 1]),
                            )
                        )
                    if i + 1 < N:

                        d[i + 1, j].append(
                            (
                                chain + [(i + 1, j)],
                                semiring.mul(score, edge[:, :, i + 1, j, 2]),
                            )
                        )
                    if j + 1 < M:
                        d[i, j + 1].append(
                            (
                                chain + [(i, j + 1)],
                                semiring.mul(score, edge[:, :, i, j + 1, 0]),
                            )
                        )
        all_val = torch.stack([x[1] for x in d[N - 1, M - 1]], dim=-1)
        return semiring.unconvert(semiring.sum(all_val)), None

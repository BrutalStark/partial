import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn import init
from torch.nn.utils.rnn import pad_sequence


def init_parameters(net):
    for name, param in net.named_parameters():
        if 'weight' in name:
            init.xavier_normal_(param)
        else:
            nn.init.zeros_(param)


# ----- Dialogue Emotion Networks ----- #
class SimpleAttention(nn.Module):
    def __init__(self, input_dim):
        super(SimpleAttention, self).__init__()
        self.input_dim = input_dim
        self.scalar = nn.Linear(self.input_dim, 1, bias=False)

    def forward(self, M):
        """
        M -> (seq_len, batch, vector)
        """
        scale = self.scalar(M)  # seq_len, batch, 1
        alpha = F.softmax(scale, dim=0).permute(1, 2, 0)  # batch, 1, seq_len
        attn_pool = torch.bmm(alpha, M.transpose(0, 1))[:, 0, :]  # batch, vector

        return attn_pool, alpha


class MatchingAttention(nn.Module):

    def __init__(self, mem_dim, cand_dim, alpha_dim=None, att_type='general'):
        super(MatchingAttention, self).__init__()
        assert att_type != 'concat' or alpha_dim is not None
        assert att_type != 'dot' or mem_dim == cand_dim
        self.mem_dim = mem_dim
        self.cand_dim = cand_dim
        self.att_type = att_type
        if att_type == 'general':
            self.transform = nn.Linear(cand_dim, mem_dim, bias=False)
        if att_type == 'general2':
            self.transform = nn.Linear(cand_dim, mem_dim, bias=True)
            # torch.nn.init.normal_(self.transform.weight,std=0.01)
        elif att_type == 'concat':
            self.transform = nn.Linear(cand_dim + mem_dim, alpha_dim, bias=False)
            self.vector_prod = nn.Linear(alpha_dim, 1, bias=False)

    def forward(self, M, x, mask=None):
        """
        M -> (seq_len, batch, mem_dim), g_hist..., key and value
        x -> (batch, cand_dim), U..., query
        mask -> (batch, seq_len)
        """
        if type(mask) == type(None):
            mask = torch.ones(M.size(1), M.size(0)).type(M.type())

        if self.att_type == 'dot':
            # vector = cand_dim = mem_dim
            M_ = M.permute(1, 2, 0)  # batch, vector, seqlen
            x_ = x.unsqueeze(1)  # batch, 1, vector
            alpha = F.softmax(torch.bmm(x_, M_), dim=2)  # batch, 1, seqlen
        elif self.att_type == 'general':
            M_ = M.permute(1, 2, 0)  # batch, mem_dim, seqlen
            x_ = self.transform(x).unsqueeze(1)  # batch, 1, mem_dim
            alpha = F.softmax(torch.bmm(x_, M_), dim=2)  # batch, 1, seqlen
        elif self.att_type == 'general2':
            print(M.shape)
            M_ = M.permute(1, 2, 0)  # batch, mem_dim, seqlen
            x_ = self.transform(x).unsqueeze(1)  # batch, 1, mem_dim
            alpha_ = F.softmax((torch.bmm(x_, M_)) * mask.unsqueeze(1), dim=2)  # batch, 1, seqlen
            alpha_masked = alpha_ * mask.unsqueeze(1)  # batch, 1, seqlen
            alpha_sum = torch.sum(alpha_masked, dim=2, keepdim=True)  # batch, 1, 1
            alpha = alpha_masked / alpha_sum  # batch, 1, 1 ; normalized
            # import ipdb;ipdb.set_trace()
        else:
            M_ = M.transpose(0, 1)  # batch, seqlen, mem_dim
            x_ = x.unsqueeze(1).expand(-1, M.size()[0], -1)  # batch, seqlen, cand_dim
            M_x_ = torch.cat([M_, x_], 2)  # batch, seqlen, mem_dim+cand_dim
            mx_a = F.tanh(self.transform(M_x_))  # batch, seqlen, alpha_dim
            alpha = F.softmax(self.vector_prod(mx_a), 1).transpose(1, 2)  # batch, 1, seqlen

        attn_pool = torch.bmm(alpha, M.transpose(0, 1))[:, 0, :]  # batch, mem_dim

        return attn_pool, alpha


def _select_parties(X, indices):
    q0_sel = []
    for idx, j in zip(indices, X):
        q0_sel.append(j[idx].unsqueeze(0))
    q0_sel = torch.cat(q0_sel, 0)
    return q0_sel


class DialogueRNNCell(nn.Module):

    def __init__(self, D_m, D_g, D_p, D_e, party,
                 context_attention=None, party_attention=None, D_a=128, dropout=0.5):
        super(DialogueRNNCell, self).__init__()

        self.D_m = D_m
        self.D_g = D_g
        self.D_p = D_p
        self.D_e = D_e
        self.party = party
        self.party_attention = party_attention

        self.g_cell = nn.GRUCell(D_m + D_p + D_e, D_g)
        self.p_cell = nn.GRUCell(D_m + D_g + D_e, D_p)
        if self.party_attention is None:
            self.e_cell = nn.GRUCell(D_p, D_e)
        else:
            self.e_cell = nn.GRUCell(D_p + D_p + D_g, D_e)

        self.dropout = nn.Dropout(dropout)

        if context_attention is not None:
            self.attention = MatchingAttention(D_g, D_m, D_a, context_attention)
        if party_attention is not None:
            self.attention_p1 = MatchingAttention(D_p, D_m, D_a, party_attention)
            self.attention_p2 = MatchingAttention(D_p, D_m, D_a, party_attention)

    def forward(self, U, qmask, g_hist, q0, q_hist, e0):
        """
        U -> batch, D_m
        qmask -> batch, party
        g_hist -> t-1, batch, D_g
        q_hist -> t-1, batch, party, D_p
        Q -> batch, party, D_p
        q0 -> batch, party, D_p
        e0 -> batch, party, D_e
        q0_sel -> batch, D_p
        U_c_ -> batch, party, D_m + D_g
        """
        if self.party_attention:
            e0 = torch.zeros(qmask.shape[0], self.party, self.D_e).type(U.type()) if e0.size()[0] == 0 else e0
        else:
            e0 = torch.zeros(qmask.shape[0], self.D_e).type(U.type()) if e0.size()[0] == 0 else e0

        q0 = torch.zeros(qmask.shape[0], self.party, self.D_p).type(U.type()) if q0.size()[0] == 0 else q0

        qm_idx = torch.argmax(qmask, 1)  # indicate which person
        q0_sel = _select_parties(q0, qm_idx)
        e0_sel = _select_parties(e0, qm_idx) if self.party_attention else e0

        if g_hist.size()[0] == 0:
            g_ = self.g_cell(torch.cat([U, q0_sel, e0_sel], dim=1),
                             torch.zeros((U.shape[0], self.D_g), dtype=torch.float32).type(U.type()))
        else:
            g_ = self.g_cell(torch.cat([U, q0_sel, e0_sel], dim=1), g_hist[-1])

        g_ = self.dropout(g_)
        g_hist = torch.cat([g_hist, g_.unsqueeze(0)], 0)

        if g_hist.shape[0] == 0:
            gc_ = torch.zeros((U.shape[0], self.D_g), dtype=torch.float32).type(U.type())
            alpha = None
        else:
            gc_, alpha = self.attention(g_hist, U)  # batch_size, D_g
        # c_ = torch.zeros(U.size()[0],self.D_g).type(U.type()) if g_hist.size()[0]==0\
        #         else self.attention(g_hist,U)[0] # batch, D_g
        U_gc_ = torch.cat((U, gc_, e0_sel), dim=1).unsqueeze(1).expand(-1, qmask.size()[1], -1)

        qs_ = self.p_cell(U_gc_.contiguous().view(-1, self.D_m + self.D_g + self.D_e),
                          q0.view(-1, self.D_p)).view(U.shape[0], -1, self.D_p)
        qs_ = self.dropout(qs_)

        ql_ = q0
        qmask_ = qmask.unsqueeze(2)
        q_ = ql_ * (1 - qmask_) + qs_ * qmask_
        # q_ = qs_
        q_hist = torch.cat([q_hist, q_.unsqueeze(0)], 0)

        if self.party_attention is not None:
            # party emotion section
            # personal attention for emotion context
            Q = torch.zeros((U.shape[0], self.party, self.D_p), dtype=torch.float32).type(U.type())
            Qp = torch.zeros((U.shape[0], self.party, self.D_p), dtype=torch.float32).type(U.type())
            if q_hist.size()[0] == 0:
                # batch, party, D_p
                alpha_p = None
            else:
                for p in range(self.party):
                    Q_, _ = self.attention_p1(q_hist[:, :, 1 - p, :], U)  # batch_size, D_p
                    Q[:, p, :] = Q_

                    Q_, _ = self.attention_p2(q_hist[:, :, p, :], U)  # batch_size, D_p
                    Qp[:, p, :] = Q_
                # Q = self.sa(q_, q_, q_)
            U_Q = torch.cat([Q, Qp, g_.unsqueeze(1).expand(-1, qmask.size()[1], -1)], dim=2)
            es_ = self.e_cell(U_Q.contiguous().view(-1, self.D_p + self.D_p + self.D_g),
                              e0.view(-1, self.D_e)).view(U.size()[0], -1, self.D_e)
            es_ = self.dropout(es_)

            el_ = e0
            e_ = el_ * (1 - qmask_) + es_ * qmask_

        else:
            e_ = self.e_cell(_select_parties(q_, qm_idx), e0_sel)
            e_ = self.dropout(e_)

        if self.party_attention:
            e_out = _select_parties(e_, qm_idx).detach()
        else:
            e_out = e_.detach()
        return g_, q_, e_, e_out, alpha


class DialogueRNN(nn.Module):

    def __init__(self, D_m, D_g, D_p, D_e, party,
                 context_attention='simple', party_attention=None, D_a=128, dropout=0.5):
        super(DialogueRNN, self).__init__()

        self.D_m = D_m
        self.D_g = D_g
        self.D_p = D_p
        self.D_e = D_e
        self.dropout = nn.Dropout(dropout)
        self.party_attention = party_attention

        self.dialogue_cell = DialogueRNNCell(D_m, D_g, D_p, D_e, party,
                                             context_attention, party_attention, D_a, dropout)

    def forward(self, U, qmask):
        """
        U -> seq_len, batch, D_m
        qmask -> seq_len, batch, party
        e -> seq_len, batch, party, D_e
        e_ret -> seq_len, batch, D_e
        c -> # seq_len, batch, D_e
        e_ -> # batch, party, D_e
        """

        g_hist = torch.zeros(0).type(U.type())  # 0-dimensional tensor
        q_hist = torch.zeros(0).type(U.type())  # 0-dimensional tensor

        q_ = torch.zeros(0).type(U.type())  # batch, party, D_p
        e_ = torch.zeros(0).type(U.type())
        c_ = torch.zeros(0).type(U.type())

        e, alpha, c = [], [], []
        for u_, qmask_ in zip(U, qmask):
            g_, q_, e_, c_, alpha_ = self.dialogue_cell(u_, qmask_, g_hist, q_, q_hist, e_)

            if self.party_attention is not None:
                qm_idx = torch.argmax(qmask_, dim=1)
                e_party = _select_parties(e_, qm_idx)
                e.append(e_party)
            else:
                e.append(e_)
            c.append(c_)

            if type(alpha_) != type(None):
                alpha.append(alpha_[:, 0, :])

        return torch.stack(e).type(U.type()), torch.stack(c).type(U.type()), alpha


def _reverse_seq(X, mask):
    """
    X -> seq_len, batch, dim
    mask -> batch, seq_len
    """
    X_ = X.transpose(0, 1)
    mask_sum = torch.sum(mask, 1).int()

    xfs = []
    for x, c in zip(X_, mask_sum):
        xf = torch.flip(x[:c], [0])
        xfs.append(xf)

    return pad_sequence(xfs)


class Model(nn.Module):
    def __init__(self, D_h, D_g, D_p, D_e, D_y, party,
                 n_classes, context_attention='simple', party_attention='simple',
                 D_a=100, dropout_rec=0.5, dropout=0.5):
        super(Model, self).__init__()

        self.D_h = D_h
        self.D_g = D_g
        self.D_p = D_p
        self.D_e = D_e
        self.D_y = D_y
        self.party = party
        self.n_classes = n_classes
        self.dropout = nn.Dropout(dropout)
        self.party_attention = party_attention
        # self.dropout_rec = nn.Dropout(0.2)
        self.dropout_rec = nn.Dropout(dropout + 0.15)
        self.dialog_rnn_f = DialogueRNN(D_h, D_g, D_p, D_e, self.party,
                                        context_attention, party_attention, D_a, dropout_rec)
        self.dialog_rnn_b = DialogueRNN(D_h, D_g, D_p, D_e, self.party,
                                        context_attention, party_attention, D_a, dropout_rec)
        self.linear1 = nn.Linear(2 * D_e, D_y)
        self.linear2 = nn.Linear(D_y, D_y // 2)
        # self.linear3     = nn.Linear(D_h, D_h)
        self.smax_fc = nn.Linear(D_y // 2, n_classes)

        self.matchatt = MatchingAttention(D_e, D_e, att_type='general2')

    def forward(self, U, qmask, umask=None, att2=False):
        """
        U -> seq_len, batch, D_
        qmask -> seq_len, batch, party
        """

        emotions_f, c_f, _ = self.dialog_rnn_f(U, qmask)  # seq_len, batch, D_e
        emotions_f = self.dropout_rec(emotions_f)

        rev_U = _reverse_seq(U, umask)
        rev_qmask = _reverse_seq(qmask, umask)

        emotions_b, c_b, _ = self.dialog_rnn_b(rev_U, rev_qmask)
        emotions_b = _reverse_seq(emotions_b, umask)
        emotions_b = self.dropout_rec(emotions_b)
        emotions = torch.cat([emotions_f, emotions_b], dim=-1)

        c = torch.cat((c_f, c_b), dim=-1)
        # emotions = emotions.unsqueeze(1)
        if att2 and self.party_attention == "simple":
            att_emotions = []
            for t in emotions:
                att_emotions.append(self.matchatt(emotions, t, mask=umask)[0].unsqueeze(0))
            att_emotions = torch.cat(att_emotions, dim=0)
            hidden = F.relu(self.linear1(att_emotions))
        else:
            hidden = F.relu(self.linear1(emotions))
        hidden = F.relu(self.linear2(hidden))
        # hidden = F.relu(self.linear3(hidden))
        hidden = self.dropout(hidden)
        log_prob = F.log_softmax(self.smax_fc(hidden), 2)  # seq_len, batch, n_classes
        return log_prob, c


class MaskedNLLLoss(nn.Module):

    def __init__(self, weight=None):
        super(MaskedNLLLoss, self).__init__()
        self.weight = weight
        self.loss = nn.NLLLoss(weight=weight,
                               reduction='sum')

    def forward(self, pred, target, mask):
        """
        pred -> batch*seq_len, n_classes
        target -> batch*seq_len
        mask -> batch, seq_len
        """
        mask_ = mask.view(-1, 1)  # batch*seq_len, 1
        if type(self.weight) == type(None):
            loss = self.loss(pred * mask_, target) / torch.sum(mask)
        else:
            loss = self.loss(pred * mask_, target) \
                   / torch.sum(self.weight[target] * mask_.squeeze())
        return loss


# ----- Partial Multi-view Networks ----- #
class CPMNets(nn.Module):  # The architecture of the CPM
    """
    build model
    """

    def __init__(self, view_num, trainLen, testLen, layer_size, v, lsd_dim=128, lamb=1):
        """
        :param view_num:view number
        :param layer_size:node of each net
        :param lsd_dim:latent space dimensionality
        :param trainLen:training dataset samples
        :param testLen:testing dataset samples
        """
        super(CPMNets, self).__init__()
        # initialize parameter
        self.view_num = view_num
        self.layer_size = layer_size
        self.lsd_dim = lsd_dim
        self.trainLen = trainLen
        self.testLen = testLen
        self.lamb = lamb
        # initialize forward methods
        self.net = self._make_view(v).cuda()

    def forward(self, h):
        h_views = self.net(h.cuda())
        return h_views

    def _make_view(self, v):
        dims_net = self.layer_size[v]
        net1 = nn.Sequential()
        w = torch.nn.Linear(self.lsd_dim, dims_net[0])
        a = torch.nn.ReLU()
        nn.init.xavier_normal_(w.weight)
        nn.init.constant_(w.bias, 0.0)
        net1.add_module('lin' + str(0), w)
        net1.add_module('act' + str(0), a)
        for num in range(1, len(dims_net)):
            w = torch.nn.Linear(dims_net[num - 1], dims_net[num])
            nn.init.xavier_normal_(w.weight)
            nn.init.constant_(w.bias, 0.0)
            net1.add_module('lin' + str(num), w)
            # net1.add_module('act' + str(num), a)
            net1.add_module('drop' + str(num), torch.nn.Dropout(p=0.1))

        return net1


class CpmGenerator(nn.Module):
    def __init__(self, layer_size, lsd_dim):
        super(CpmGenerator, self).__init__()
        self.model = nn.Sequential(
            nn.Linear(lsd_dim, layer_size[0]),
            nn.BatchNorm1d(layer_size[0], 0.8),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Linear(layer_size[0], layer_size[1]),
        )
        # init_parameters(self.model)
        self.model.cuda()

    def forward(self, x):
        return self.model(x)


class CpmDiscriminator(nn.Module):
    def __init__(self, layer_size):
        super(CpmDiscriminator, self).__init__()
        self.model = nn.Sequential(
            nn.Linear(layer_size[1], layer_size[1] // 2),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Linear(layer_size[1] // 2, 1),
        )
        # init_parameters(self.model)
        self.model.cuda()

    def forward(self, x):
        return self.model(x)


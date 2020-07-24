from torch import nn
from utils import compute_rouge_l

import torch

BERT_OUTPUT_SIZE = 768
BI_LSTM_OUTPUT_SIZE = 2


class ExtractorModel(nn.Module):
    def __init__(self, bi_lstm, ptr_lstm):
        super(ExtractorModel, self).__init__()
        self.bi_lstm = bi_lstm
        self.ptr_lstm = ptr_lstm
        self.softmax = nn.Softmax(dim=1)
        self.sigmoid = nn.Sigmoid()

        # Todo: Find suitable attn_dim
        attn_dim = self.ptr_lstm.hidden_size
        self.linear_h = torch.nn.Linear(self.ptr_lstm.hidden_size, attn_dim, bias=False)
        self.linear_e = torch.nn.Linear(self.ptr_lstm.hidden_size, attn_dim, bias=False)
        self.linear_v = torch.nn.Linear(attn_dim, 1, bias=False)
        self.tanh = torch.nn.Tanh()

    def forward(self, input_embeddings):
        """
        Todo: Convert from "dot" attention mechanism to "additive" to match paper
        Todo: Reference for the above Todo: http://web.stanford.edu/class/cs224n/slides/cs224n-2020-lecture08-nmt.pdf
        Todo: Provide better comments than labeling Eq numbers from paper

        :param input_embeddings: shape (n_documents, seq_len, embedding_dim)
        :return:
        """
        h, __ = self.bi_lstm(input_embeddings)
        z, __ = self.ptr_lstm(h)

        # Eq (3) Todo: Replace "dot" attention mechanism w/ "additive" like in paper
        attn = torch.bmm(h, z.transpose(1, 2))  # Dot attention mechanism

        # Eq (4)
        attn_weights = self.softmax(attn)

        # Eq (5)
        e = torch.bmm(attn_weights, h)

        # Eq (1)
        u = self.linear_h(h) + self.linear_e(e)
        u = self.tanh(u)
        u = self.linear_v(u)

        # Eq (2)
        # p = self.softmax(u).squeeze()
        p = self.sigmoid(u).squeeze()

        return p


# Adapted from: https://github.com/ChenRocks/fast_abs_rl/tree/b1b66c180a135905574fdb9e80a6da2cabf7f15c
def get_extract_label(art_sents, abs_sents):
    """ greedily match summary sentences to article sentences"""
    extracted = []
    scores = []
    indices = list(range(len(art_sents)))
    n_art_sents = len(art_sents)
    label_vec = torch.zeros(n_art_sents)

    for abst in abs_sents:
        rouges = [compute_rouge_l(output=art_sent, reference=abst, mode='r') for art_sent in art_sents]
        ext = max(indices, key=lambda i: rouges[i])
        indices.remove(ext)
        extracted.append(ext)
        scores.append(rouges[ext])
        if not indices:
            break

    label_vec[extracted] = 1

    return label_vec


bidirectional_lstm = nn.LSTM(
    input_size=BERT_OUTPUT_SIZE,
    hidden_size=BI_LSTM_OUTPUT_SIZE,
    num_layers=1,
    bidirectional=True
)

pointer_lstm = nn.LSTM(
    input_size=BI_LSTM_OUTPUT_SIZE * 2,
    hidden_size=BI_LSTM_OUTPUT_SIZE * 2,
    num_layers=1,
    bidirectional=False
)

import os

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as functional
from torch.autograd import Variable
from torch.nn.utils import clip_grad_norm

# Inference with SVR
import pickle

from utils.parameter_initialization import xavier_normal


class LSTMEncoder(nn.Module):

    def __init__(self, vocab_size, char_size, opt, is_train=False):
        super(LSTMEncoder, self).__init__()
        self.vocab_size = vocab_size
        self.char_size = char_size
        self.opt = opt
        # self.batch_size = batch_size
        self.name = 'sentence representation'

        self.embedding_table = nn.Embedding(num_embeddings=self.vocab_size, embedding_dim=self.opt.embedding_dims,
                                            padding_idx=0, max_norm=None, scale_grad_by_freq=False, sparse=False)
        self.embedding_table_char = nn.Embedding(num_embeddings=self.char_size, embedding_dim=self.opt.embedding_dims_char,
                                                 padding_idx=0, max_norm=None, scale_grad_by_freq=False, sparse=False)
        self.lstm_word = nn.LSTM(input_size=self.opt.embedding_dims, hidden_size=self.opt.hidden_dims, num_layers=3,
                                 dropout=0.5, bidirectional=True, batch_first=True)
        self.lstm_char = nn.LSTM(input_size=self.opt.embedding_dims_char, hidden_size=self.opt.hidden_dims_char,
                                 num_layers=3, dropout=0.5, bidirectional=True, batch_first=True)
        # Self Attention Layers
        self.S1 = nn.Linear(self.opt.hidden_dims * 2, self.opt.hidden_dims * 2, bias=True)
        self.S2 = nn.Linear(self.opt.hidden_dims * 2, self.opt.hidden_dims * 2, bias=True)
        self.init_weights()
        # self.initialize_hidden_plus_cell(self.batch_size)

    def initialize_hidden_plus_cell(self, batch_size):

        if torch.cuda.is_available():
            zero_hidden = Variable(torch.zeros(6, batch_size, self.opt.hidden_dims).cuda(), requires_grad=True)
            zero_cell = Variable(torch.zeros(6, batch_size, self.opt.hidden_dims).cuda(), requires_grad=True)
        else:
            zero_hidden = Variable(torch.zeros(6, batch_size, self.opt.hidden_dims), requires_grad=True)
            zero_cell = Variable(torch.zeros(6, batch_size, self.opt.hidden_dims), requires_grad=True)

        return zero_hidden, zero_cell

    def init_weights(self):
        initrange = 0.1
        self.S1.weight.data.uniform_(-initrange, initrange)
        self.S2.weight.data.uniform_(-initrange, initrange)

    def attention(self, output_q, output_c, batch_size, seq_len, is_char=False):
        if torch.cuda.is_available():
            attn_Q = Variable(torch.zeros(batch_size, seq_len * self.opt.hidden_dims * 2).cuda())
            attn_C = Variable(torch.zeros(batch_size, seq_len * self.opt.hidden_dims * 2).cuda())
        else:
            attn_Q = Variable(torch.zeros(batch_size, seq_len * self.opt.hidden_dims * 2))
            attn_C = Variable(torch.zeros(batch_size, seq_len * self.opt.hidden_dims * 2))

        # Attention
        for i in range(batch_size):  # for i in batch_size
            Q = output_q[i, :seq_len, :]  # output[bs, sequence, nhid] for question, padding len * nhid
            C0 = output_c[i, :seq_len, :]  # padding len * nhid of candidate sent
            if is_char:
                C = functional.tanh(self.S2(C0))
            else:
                C = functional.tanh(self.S1(C0))
            H = torch.mm(Q, C.t())  # parallel attention
            AQ = functional.softmax(H)  # p, p
            AC = functional.softmax(H.t())

            MQ = torch.mm(AQ, Q)  # p, nhid
            MC = torch.mm(AC, MQ)
            attn_Q[i, :] = MQ.view(-1)
            attn_C[i, :] = MC.view(-1)

        return attn_Q, attn_C

    def forward(self, batch_size, input_Q, input_C, input_C2, input_Q_ch, input_C_ch, input_C2_ch, is_train=True):
        # get the sentence matrix
        hidden_Q, cell_Q = hidden_C, cell_C = hidden_C2, cell_C2 = self.initialize_hidden_plus_cell(batch_size)
        hidden_Q_ch, cell_Q_ch = hidden_C_ch, cell_C_ch = hidden_C2_ch, cell_C2_ch = \
            self.initialize_hidden_plus_cell(batch_size)

        input_Q = torch.transpose(input_Q, 0, 1)
        input_C = torch.transpose(input_C, 0, 1)
        input_Q_ch = torch.transpose(input_Q_ch, 0, 1)
        input_C_ch = torch.transpose(input_C_ch, 0, 1)
        output = self.embedding_table(input_Q)
        output_ch = self.embedding_table_char(input_Q_ch)
        c_output = self.embedding_table(input_C)  # .view(batch_size, 1, -1)
        c_output_ch = self.embedding_table_char(input_C_ch)

        for _ in range(self.opt.num_layers):
            output, (hidden_Q, cell_Q) = self.lstm_word(output, (hidden_Q, cell_Q))
            c_output, (hidden_C, cell_C) = self.lstm_word(c_output, (hidden_C, cell_C))
            output_ch, (hidden_Q_ch, cell_Q_ch) = self.lstm_char(output_ch, (hidden_Q_ch, cell_Q_ch))
            c_output_ch, (hidden_C_ch, cell_C_ch) = self.lstm_char(c_output_ch, (hidden_C_ch, cell_C_ch))

        attn_Q1, attn_C = self.attention(output, c_output, batch_size, input_Q.size(1), is_char=False)
        attn_Q1_ch, attn_C_ch = self.attention(output_ch, c_output_ch, batch_size, input_Q_ch.size(1), is_char=True)
        repre_Q1 = torch.cat((attn_Q1, attn_Q1_ch), 1)
        repre_C = torch.cat((attn_C, attn_C_ch), 1)

        if is_train:
            input_C2 = torch.transpose(input_C2, 0, 1)
            input_C2_ch = torch.transpose(input_C2_ch, 0, 1)
            c2_output = self.embedding_table(input_C2)
            c2_output_ch = self.embedding_table_char(input_C2_ch)
            for _ in range(self.opt.num_layers):
                c2_output, (hidden_C2, cell_C2) = self.lstm_word(c2_output, (hidden_C2, cell_C2))
                c2_output_ch, (hidden_C2_ch, cell_C2_ch) = self.lstm_char(c2_output_ch, (hidden_C2_ch, cell_C2_ch))
            attn_Q2, attn_C2 = self.attention(output, c2_output, batch_size, input_Q.size(1), is_char=False)
            attn_Q2_ch, attn_C2_ch = self.attention(output_ch, c2_output_ch, batch_size, input_Q_ch.size(1), is_char=True)
            repre_Q2 = torch.cat((attn_Q2, attn_Q2_ch), 1)
            repre_C2 = torch.cat((attn_C2, attn_C2_ch), 1)

            return repre_Q1, repre_C, repre_Q2, repre_C2
        else:
            return repre_Q1, repre_C


class AnswerSelection(nn.Module):

    def __init__(self, vocab_size, char_size, opt, pretrained_embeddings=None, pre_char_embed=None, is_train=True):
        super(AnswerSelection, self).__init__()
        self.opt = opt
        self.is_train = is_train

        self.encoder = LSTMEncoder(vocab_size, char_size, self.opt, self.is_train)
        if pretrained_embeddings is not None:
            self.encoder.embedding_table.weight.data.copy_(pretrained_embeddings)
        if pre_char_embed is not None:
            self.encoder.embedding_table_char.weight.data.copy_(pre_char_embed)
        self.initialize_parameters()

        self.loss_function = nn.MarginRankingLoss(margin=0.5)
        self.optimizer = optim.Adam(self.encoder.parameters(), lr=self.opt.learning_rate,
                                      betas=(self.opt.beta_1, 0.999), weight_decay=1e-5)
        self.distance = nn.PairwiseDistance(1)

    def forward(self):

        if self.is_train:
            self.repre_Q1, self.repre_C, self.repre_Q2, self.repre_C2 = \
                self.encoder(self.batch_size, self.batch_a, self.batch_b, self.batch_c, self.batch_a_char,
                             self.batch_b_char, self.batch_c_char,self.is_train)

            if self.batch_size == 1:
                self.prediction1 = self.distance(self.repre_Q1.view(1, -1), self.repre_C.view(1, -1)).view(-1, 1)
                self.prediction2 = self.distance(self.repre_Q2.view(1, -1), self.repre_C2.view(1, -1)).view(-1, 1)
            else:
                self.prediction1 = self.distance(self.repre_Q1, self.repre_C).view(-1, 1)
                self.prediction2 = self.distance(self.repre_Q2, self.repre_C2).view(-1, 1)

            return self.prediction1, self.prediction2
        else:
            self.repre_Q1, self.repre_C = self.encoder(self.batch_size, self.batch_a, self.batch_b, None,
                                                     self.batch_a_char, self.batch_b_char, None, self.is_train)

            if self.batch_size == 1:
                self.prediction = self.distance(self.repre_Q1.view(1, -1), self.repre_C.view(1, -1)).view(-1, 1)
            else:
                self.prediction = self.distance(self.repre_Q1, self.repre_C).view(-1, 1)
            return self.prediction

    def get_loss(self):
        self.loss = self.loss_function(self.prediction2, self.prediction1, self.labels)  # + self.penal

    def load_pretrained_parameters(self):

        pretrained_state_dict_path = os.path.join(self.opt.pretraining_dir, self.opt.pretrained_state_dict)
        self.encoder.load_state_dict(torch.load(pretrained_state_dict_path))
        print('Pretrained parameters have been successfully loaded into the encoder networks.')

    def initialize_parameters(self):

        state_dict = self.encoder.state_dict()
        for key in state_dict.keys():
            if '.weight' in key:
                state_dict[key] = xavier_normal(state_dict[key])
            if '.bias' in key:
                bias_length = state_dict[key].size()[0]
                start, end = bias_length // 4, bias_length // 2
                state_dict[key][start:end].fill_(2.5)
        self.encoder.load_state_dict(state_dict)

    def train_step(self, train_batch_a, train_batch_b, train_batch_c, char1, char2, char3,
                   train_labels):
        if torch.cuda.is_available():
            self.batch_a = train_batch_a.cuda()
            self.batch_b = train_batch_b.cuda()
            self.batch_c = train_batch_c.cuda()
            self.batch_a_char = char1.cuda()
            self.batch_b_char = char2.cuda()
            self.batch_c_char = char3.cuda()
            self.labels = train_labels.cuda()
        else:
            self.batch_a = train_batch_a
            self.batch_b = train_batch_b
            self.batch_c = train_batch_c
            self.batch_a_char = char1
            self.batch_b_char = char2
            self.batch_c_char = char3
            self.labels = train_labels

        self.batch_size = self.batch_a.size(1)

        self.forward()
        self.encoder.zero_grad()  # encoder_a == encoder_b
        self.get_loss()
        self.loss.backward()
        clip_grad_norm(self.encoder.parameters(), self.opt.clip_value)

        self.optimizer.step()
        return self.prediction1, self.prediction2

    def test_step(self, test_batch_a, test_batch_b, test_batch_c, char1, char2, char3, test_labels):
        if torch.cuda.is_available():
            self.batch_a = test_batch_a.cuda()
            self.batch_b = test_batch_b.cuda()
            self.batch_c = test_batch_c.cuda()
            self.batch_a_char = char1.cuda()
            self.batch_b_char = char2.cuda()
            self.batch_c_char = char3.cuda()
            self.labels = test_labels.cuda()
        else:
            self.batch_a = test_batch_a
            self.batch_b = test_batch_b
            self.batch_c = test_batch_c
            self.batch_a_char = char1
            self.batch_b_char = char2
            self.batch_c_char = char3
            self.labels = test_labels

        self.batch_size = self.batch_a.size(1)
        #
        self.forward()
        self.get_loss()
        return self.prediction1, self.prediction2

    def qa_step(self, test_batch_a, test_batch_b, char1, char2, test_labels):
        self.is_train = False

        if torch.cuda.is_available():
            self.batch_a = test_batch_a.cuda()
            self.batch_b = test_batch_b.cuda()
            self.batch_a_char = char1.cuda()
            self.batch_b_char = char2.cuda()
            self.labels = test_labels.cuda()
        else:
            self.batch_a = test_batch_a
            self.batch_b = test_batch_b
            self.batch_a_char = char1
            self.batch_b_char = char2
            self.labels = test_labels

        self.batch_size = self.batch_a.size(1)
        self.forward()
        return self.prediction
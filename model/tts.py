import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

from .rnn import DelayedRNN
from text import symbols


class Attention(nn.Module):
    def __init__(self, hp):
        super(Attention, self).__init__()
        self.M = hp.model.gmm
        self.rnn_cell = nn.LSTMCell(input_size=2*hp.model.hidden, hidden_size=hp.model.hidden)
        self.W_g = nn.Linear(hp.model.hidden, 3*self.M)
        with torch.no_grad():
            self.W_g.bias[self.M:2*self.M] += np.log(10)
        
    def attention(self, h_i, memory, ksi):
        phi_hat = self.W_g(h_i)

        ksi = ksi + torch.exp(phi_hat[:, :self.M])
        beta = torch.exp(phi_hat[:, self.M:2*self.M])
        alpha = F.softmax(phi_hat[:, 2*self.M:3*self.M], dim=-1)
        
        u = memory.new_tensor(np.arange(memory.size(1)), dtype=torch.float)
        u_R = u + 1.5
        u_L = u + 0.5
        
        term1 = torch.sum(
            alpha.unsqueeze(-1) * torch.reciprocal(
                1 + torch.exp((ksi.unsqueeze(-1) - u_R) / beta.unsqueeze(-1))
            ),
            keepdim=True,
            dim=1
        )
        
        term2 = torch.sum(
            alpha.unsqueeze(-1) * torch.reciprocal(
                1 + torch.exp((ksi.unsqueeze(-1) - u_L) / beta.unsqueeze(-1))
            ),
            keepdim=True,
            dim=1
        )
        
        weights = term1 - term2
        
        context = torch.bmm(weights, memory)
        
        termination = 1 - term1.squeeze(1)

        return context, weights, termination, ksi # (B, 1, D), (B, 1, T), (B, T)

    
    
    def forward(self, input_h_c, memory, input_lengths):
        B, T, D = input_h_c.size()
        
        context = input_h_c.new_zeros(B, D)
        h_i, c_i  = input_h_c.new_zeros(B, D), input_h_c.new_zeros(B, D)
        ksi = input_h_c.new_zeros(B, self.M)
        
        contexts, weights = [], []
        for i in range(T):
            x = torch.cat([input_h_c[:, i], context.squeeze(1)], dim=-1)
            h_i, c_i = self.rnn_cell(x, (h_i, c_i))
            context, weight, termination, ksi = self.attention(h_i, memory, ksi)
            
            contexts.append(context)
            weights.append(weight)
            
        contexts = torch.cat(contexts, dim=1)
        alignment = torch.cat(weights, dim=1)
        termination = torch.gather(termination, 1, (input_lengths-1).unsqueeze(-1)) # 4

        return context, alignment, termination



class TTS(nn.Module):
    def __init__(self, hp, freq, layers):
        super(TTS, self).__init__()
        self.hp = hp

        self.W_t_0 = nn.Linear(1, hp.model.hidden)
        self.W_f_0 = nn.Linear(1, hp.model.hidden)
        self.W_c_0 = nn.Linear(freq, hp.model.hidden)
        
        self.layers = nn.ModuleList([ DelayedRNN(hp) for _ in range(layers) ])

        # Gaussian Mixture Model: eq. (2)
        self.K = hp.model.gmm
        self.pi_softmax = nn.Softmax(dim=3)

        # map output to produce GMM parameter eq. (10)
        self.W_theta = nn.Linear(hp.model.hidden, 3*self.K)

        self.embedding_text = nn.Embedding(len(symbols), hp.model.hidden)
        self.text_lstm = nn.LSTM(input_size=hp.model.hidden,
                                hidden_size=hp.model.hidden//2, 
                                batch_first=True,
                                bidirectional=True)
        
        self.attention = Attention(hp)

    def text_encode(self, text, input_lengths):
        total_length = text.size(1)
        embed = self.embedding_text(text)
        packed = nn.utils.rnn.pack_padded_sequence(embed, input_lengths, batch_first=True, enforce_sorted=False)
        memory, _ = self.text_lstm(packed)
        unpacked, _ = nn.utils.rnn.pad_packed_sequence(memory, batch_first=True, total_length=total_length)
        return unpacked
        
    def forward(self, x, text, input_lengths):
        # Extract memory
        memory = self.text_encode(text, input_lengths)
        
        # x: [B, M, T] / B=batch, M=mel, T=time
        h_t = self.W_t_0(F.pad(x, [1, -1]).unsqueeze(-1))
        h_f = self.W_f_0(F.pad(x, [0, 0, 1, -1]).unsqueeze(-1))
        h_c = self.W_c_0(F.pad(x, [1, -1]).transpose(1, 2))
        
        # h_t, h_f: [B, M, T, D] / h_c: [B, T, D]
        for i, layer in enumerate(self.layers):
            if i!=(len(self.layers)//2):
                h_t, h_f, h_c = layer(h_t, h_f, h_c)
                
            else:
                h_c, alignment, termination = self.attention(h_c,
                                                             memory,
                                                             input_lengths)
                
                h_t, h_f, _ = layer(h_t, h_f, h_c, attention=True)

        theta_hat = self.W_theta(h_f)

        mu = theta_hat[:,:,:, :self.K] # eq. (3)
        std = theta_hat[:,:,:, self.K:2*self.K] # eq. (4)
        pi = theta_hat[:,:,:, 2*self.K:] # eq. (5)
            
        return mu, std, pi, alignment
    
    
    def sample(self, x, text, input_lengths):
        # Extract memory
        memory = self.text_encode(text, input_lengths)
        
        # x: [1, M, T] / B=1, M=mel, T=time
        x_t, x_f = x.clone(), x.clone()

        for i in range(x.size(-1)):
            h_t = self.W_t_0(x_t.unsqueeze(-1))
            h_f = self.W_f_0(x_f.unsqueeze(-1))
            h_c, alignment, termination = self.attention(self.W_c_0(F.pad(x, [1, -1]).transpose(1, 2)),
                                                         memory,
                                                         input_lengths)

            for layer in self.layers:
                h_t, h_f, h_c = layer(h_t, h_f, h_c)

            theta_hat = self.W_theta(h_f)

            mu = theta_hat[:, :, :, :self.K] # eq. (3)
            std = theta_hat[:, :, :, self.K:2*self.K] # eq. (4)
            pi = theta_hat[:, :, :, 2*self.K:] # eq. (5)

            x_t[:,:,i+1] = mu[:,:,i]
            x_f[:,i+1,:] = mu[:,i,:]

        return mu, std, pi, termination
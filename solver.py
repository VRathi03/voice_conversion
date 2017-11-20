import torch
from torch import optim
from torch.autograd import Variable
from torch import nn
import torch.nn.functional as F
import numpy as np
import pickle
from model import Encoder
from model import Decoder
from model import Discriminator
#from postprocess.utils import ispecgram
#from scipy.io import wavfile
import os 
from utils import Hps
from utils import DataLoader
from utils import Logger

def cal_mean_grad(net):
    grad = Variable(torch.FloatTensor([0])).cuda()
    for i, p in enumerate(net.parameters()):
        grad += torch.mean(p.grad)
    return grad.data[0] / (i + 1)

def calculate_gradients_penalty(netD, real_data, fake_data):
    alpha = torch.rand(real_data.size(0))
    alpha = alpha.view(real_data.size(0), 1, 1)
    alpha = alpha.cuda() if torch.cuda.is_available() else alpha
    alpha = Variable(alpha)
    interpolates = alpha * real_data + (1 - alpha) * fake_data
    disc_interpolates = netD(interpolates)

    gradients = torch.autograd.grad(
        outputs=torch.mean(disc_interpolates), 
        inputs=interpolates, 
        create_graph=True)[0]
    gradients_penalty = (1. - torch.sqrt(1e-8 + torch.sum(gradients.view(gradients.size(0), -1)**2, dim=1))) ** 2
    gradients_penalty = torch.mean(gradients_penalty)
    return gradients_penalty

class Solver(object):
    def __init__(self, hps, data_loader, log_dir='./log/'):
        self.hps = hps
        self.data_loader = data_loader
        self.model_kept = []
        self.max_keep = 10
        self.Encoder_s = None
        self.Encoder_c = None
        self.Decoder = None
        self.Discriminator = None
        self.G_opt = None
        self.D_opt = None
        self.build_model()
        self.logger = Logger(log_dir)

    def build_model(self):
        self.Encoder_s = Encoder()
        self.Encoder_c = Encoder()
        self.Decoder = Decoder()
        self.Discriminator = Discriminator()
        if torch.cuda.is_available():
            self.Encoder_s.cuda()
            self.Encoder_c.cuda()
            self.Decoder.cuda()
            self.Discriminator.cuda()
        params = list(self.Encoder_s.parameters()) \
            + list(self.Encoder_c.parameters())\
            + list(self.Decoder.parameters())
        self.G_opt = optim.Adagrad(params, lr=self.hps.lr)
        self.D_opt = optim.Adagrad(self.Discriminator.parameters(), lr=self.hps.lr)

    def to_var(self, x):
        x = Variable(torch.from_numpy(x), requires_grad=True)
        return x.cuda() if torch.cuda.is_available() else x

    def save_model(self, model_path, iteration, enc_only=True):
        if not enc_only:
            all_model = {
                'encoder_s': self.Encoder_s.state_dict(),
                'encoder_c': self.Encoder_c.state_dict(),
                'decoder': self.Decoder.state_dict(),
                'discriminator': self.Discriminator.state_dict(),
            }
        else:
            all_model = {
                'encoder_s': self.Encoder_s.state_dict(),
                'encoder_c': self.Encoder_c.state_dict(),
                'decoder': self.Decoder.state_dict(),
            }
        new_model_path = '{}-{}'.format(model_path, iteration)
        with open(new_model_path, 'wb') as f_out:
            torch.save(all_model, f_out)
        self.model_kept.append(new_model_path)

        if len(self.model_kept) >= self.max_keep:
            os.remove(self.model_kept[0])
            self.model_kept.pop(0)

    def reset_grad(self):
        self.Encoder_s.zero_grad()
        self.Encoder_c.zero_grad()
        self.Decoder.zero_grad()
        self.Discriminator.zero_grad()

    def load_model(self, model_path, enc_only=True):
        print('load model from {}'.format(model_path))
        with open(model_path, 'rb') as f_in:
            all_model = torch.load(f_in)
            self.Encoder_s.load_state_dict(all_model['encoder_s'])
            self.Encoder_c.load_state_dict(all_model['encoder_c'])
            self.Decoder.load_state_dict(all_model['decoder'])
            if not enc_only:
                self.Discriminator.load_state_dict(all_model['discriminator'])

    def grad_clip(self, net_list):
        max_grad_norm = self.hps.max_grad_norm
        for net in net_list:
            torch.nn.utils.clip_grad_norm(net.parameters(), max_grad_norm)

    def train(self, model_path):
        # load hyperparams
        batch_size = self.hps.batch_size
        iterations = self.hps.iterations
        max_grad_norm = self.hps.max_grad_norm
        alpha, beta, lambda_ = self.hps.alpha, self.hps.beta, self.hps.lambda_
        for iteration in range(iterations):
            for j in range(5):
                #===================== Train D =====================#
                X_i_t, X_i_tk, X_i_tk_prime, X_j = \
                    [self.to_var(x).permute(0, 2, 1) for x in next(self.data_loader)]
                # encode
                Ec_i_t = self.Encoder_c(X_i_t)
                Ec_i_tk = self.Encoder_c(X_i_tk)
                Ec_i_tk_prime = self.Encoder_c(X_i_tk_prime)
                Ec_j = self.Encoder_c(X_j)
                same_pair = torch.cat([Ec_i_t, Ec_i_tk], dim=1)
                diff_pair = torch.cat([Ec_i_tk_prime, Ec_j], dim=1)
                same_val = torch.mean(self.Discriminator(same_pair))
                diff_val = torch.mean(self.Discriminator(diff_pair))
                gradients_penalty = calculate_gradients_penalty(self.Discriminator, same_pair, diff_pair)
                w_distance = same_val - diff_val
                D_loss = -w_distance + lambda_ * gradients_penalty
                self.reset_grad()
                D_loss.backward()
                self.grad_clip([self.Discriminator])
                self.D_opt.step()
                # print info
                info = {
                    'D_loss': D_loss.data[0],
                    'w_distance': w_distance.data[0],
                    'gradients_penalty': gradients_penalty.data[0],
                }
                slot_value = (j, iteration+1, iterations) + tuple([value for value in info.values()])
                print(
                    'D-%d:[%06d/%06d], D_loss=%.3f, w_distance=%.3f, gp=%.3f'
                    % slot_value,
                )
                for tag, value in info.items():
                    self.logger.scalar_summary(tag, value, iteration + 1)
            #===================== Train G =====================#
            X_i_t, X_i_tk, X_i_tk_prime, X_j = [self.to_var(x).permute(0, 2, 1) for x in next(self.data_loader)]
            # encode
            Es_i_t = self.Encoder_s(X_i_t)
            Es_i_tk = self.Encoder_s(X_i_tk)
            Ec_i_t = self.Encoder_c(X_i_t)
            Ec_j = self.Encoder_c(X_j)
            Ec_i_tk = self.Encoder_c(X_i_tk)
            Ec_i_tk_prime = self.Encoder_c(X_i_tk_prime)
            # similarity loss
            loss_sim = torch.mean((Es_i_t - Es_i_tk) ** 2)
            # reconstruction
            E_tk = torch.cat([Es_i_t, Ec_i_tk], dim=1)
            X_tilde = self.Decoder(E_tk)
            loss_rec = torch.mean(torch.abs(X_tilde - X_i_tk))
            same_pair = torch.cat([Ec_i_t, Ec_i_tk], dim=1)
            diff_pair = torch.cat([Ec_i_tk_prime, Ec_j], dim=1)
            same_val = torch.mean(self.Discriminator(same_pair))
            diff_val = torch.mean(self.Discriminator(diff_pair))
            gradients_penalty = calculate_gradients_penalty(self.Discriminator, same_pair, diff_pair)
            w_distance = same_val - diff_val
            G_loss = loss_rec + alpha * loss_sim + beta * w_distance
            self.reset_grad()
            G_loss.backward()
            self.grad_clip([self.Encoder_c, self.Encoder_s, self.Decoder])
            self.G_opt.step()
            info = {
                'loss_rec': loss_rec.data[0],
                'loss_sim': loss_sim.data[0],
                'G_w_distance': w_distance.data[0],
            }
            slot_value = (iteration+1, iterations) + tuple([value for value in info.values()])
            print(
                'G:[%06d/%06d], loss_rec=%.3f, loss_sim=%.3f, w_distance=%.3f'
                % slot_value,
            )
            for tag, value in info.items():
                self.logger.scalar_summary(tag, value, iteration + 1)
        if iteration % 100 == 0 or iteration + 1 == iterations:
            self.save_model(model_path, iteration)

if __name__ == '__main__':
    hps = Hps()
    hps.load('./hps/v4.json')
    hps_tuple = hps.get_tuple()
    data_loader = DataLoader(
        '/nfs/Mazu/jjery2243542/voice_conversion/datasets/batches_16_128_100000.h5',
    )
    for _ in range(100000):
        try:
            a = next(data_loader)
        except:
            print(data_loader.index)
            print(len(data_loader.keys))

    #solver = Solver(hps_tuple, data_loader)

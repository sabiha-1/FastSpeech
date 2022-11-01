import copy
import math
from collections import OrderedDict

from config import hparams as hp
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import utils

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def clones(module, N):
    return nn.ModuleList([copy.deepcopy(module) for _ in range(N)])


def Embedding(num_embeddings, embedding_dim, padding_idx, std=0.01):
    m = nn.Embedding(num_embeddings, embedding_dim, padding_idx=padding_idx)
    m.weight.data.normal_(0, std)
    return m


class SpeakerIntegrator(nn.Module):
    """ Speaker Integrator """

    def __init__(self):
        super(SpeakerIntegrator, self).__init__()

    def forward(self, x, spembs):
        """
        x      shape : (batch, 39, 256)
        spembs shape : (batch, 256)
        """
        spembs = spembs.unsqueeze(1)
        spembs = spembs.repeat(1, x.shape[1], 1)
        x = x + spembs

        return x


class VarianceAdaptor(nn.Module):
    """ Variance Adaptor """

    def __init__(self):
        super(VarianceAdaptor, self).__init__()
        self.duration_predictor = VariancePredictor()
        self.length_regulator = LengthRegulator()
        self.pitch_predictor = VariancePredictor()
        self.energy_predictor = VariancePredictor()

        self.pitch_bins = nn.Parameter(
            torch.exp(
                torch.linspace(np.log(hp.f0_min), np.log(hp.f0_max), hp.n_bins - 1)
            )
        )
        self.energy_bins = nn.Parameter(
            torch.linspace(hp.energy_min, hp.energy_max, hp.n_bins - 1)
        )
        self.pitch_embedding = nn.Embedding(hp.n_bins, hp.encoder_hidden)
        self.energy_embedding = nn.Embedding(hp.n_bins, hp.encoder_hidden)

    # 1. predict duration
    # 2. predict f0     -> embedding
    # 3. predict energy -> embedding
    # 4. x + pitch_embedding + energy_embedding
    # 5. Length Regulator

    def forward(
        self,
        x,
        src_mask,
        mel_mask=None,
        duration_target=None,
        pitch_target=None,
        energy_target=None,
        max_len=None,
    ):

        ## Duration Predictor ##
        log_duration_prediction = self.duration_predictor(x, src_mask)

        if duration_target is not None:
            duration_values = duration_target
        else:
            duration_rounded = torch.clamp(
                torch.round(torch.exp(log_duration_prediction) - hp.log_offset), min=0
            )
            duration_values = duration_rounded

        ## Pitch Predictor ##
        pitch_prediction = self.pitch_predictor(x, src_mask)

        if pitch_target is not None:
            pitch_embedding = self.pitch_embedding(
                torch.bucketize(pitch_target.detach(), self.pitch_bins.detach())
            )
        else:
            pitch_embedding = self.pitch_embedding(
                torch.bucketize(pitch_prediction.detach(), self.pitch_bins.detach())
            )

        ## Energy Predictor ##
        energy_prediction = self.energy_predictor(x, src_mask)
        if energy_target is not None:
            energy_embedding = self.energy_embedding(
                torch.bucketize(energy_target.detach(), self.energy_bins.detach())
            )
        else:
            energy_embedding = self.energy_embedding(
                torch.bucketize(energy_prediction.detach(), self.energy_bins.detach())
            )

        x = x + pitch_embedding + energy_embedding

        ## Length Regulator ##

        x, mel_len = self.length_regulator(x, duration_values, max_len)
        if mel_mask is None:
            mel_mask = utils.get_mask_from_lengths(mel_len)

        return (
            x,
            log_duration_prediction,
            pitch_prediction,
            energy_prediction,
            mel_len,
            mel_mask,
        )


class LengthRegulator(nn.Module):
    """ Length Regulator """

    def __init__(self):
        super(LengthRegulator, self).__init__()

    def LR(self, x, duration, max_len):
        output = list()
        mel_len = list()
        for batch, expand_target in zip(x, duration):
            expanded = self.expand(batch, expand_target)
            output.append(expanded)
            mel_len.append(expanded.shape[0])

        if max_len is not None:
            output = utils.pad(output, max_len)
        else:
            output = utils.pad(output)

        return output, torch.LongTensor(mel_len).to(device)

    def expand(self, batch, predicted):
        out = list()

        for i, vec in enumerate(batch):
            expand_size = predicted[i].item()
            out.append(vec.expand(int(expand_size), -1))
        out = torch.cat(out, 0)

        return out

    def forward(self, x, duration, max_len):
        output, mel_len = self.LR(x, duration, max_len)
        return output, mel_len


class VariancePredictor(nn.Module):
    """ Duration, Pitch and Energy Predictor """

    def __init__(self):
        super(VariancePredictor, self).__init__()
        self.input_size = hp.encoder_hidden
        self.filter_size = hp.variance_predictor_filter_size
        self.kernel = hp.variance_predictor_kernel_size
        self.conv_output_size = hp.variance_predictor_filter_size
        self.dropout = hp.variance_predictor_dropout

        self.conv_layer = nn.Sequential(
            OrderedDict(
                [
                    (
                        "conv1d_1",
                        Conv(
                            self.input_size,
                            self.filter_size,
                            kernel_size=self.kernel,
                            padding=(self.kernel - 1) // 2,
                        ),
                    ),
                    ("relu_1", nn.ReLU()),
                    ("layer_norm_1", nn.LayerNorm(self.filter_size)),
                    ("dropout_1", nn.Dropout(self.dropout)),
                    (
                        "conv1d_2",
                        Conv(
                            self.filter_size,
                            self.filter_size,
                            kernel_size=self.kernel,
                            padding=1,
                        ),
                    ),
                    ("relu_2", nn.ReLU()),
                    ("layer_norm_2", nn.LayerNorm(self.filter_size)),
                    ("dropout_2", nn.Dropout(self.dropout)),
                ]
            )
        )

        self.linear_layer = nn.Linear(self.conv_output_size, 1)

    def forward(self, encoder_output, mask):
        out = self.conv_layer(encoder_output)
        out = self.linear_layer(out)
        out = out.squeeze(-1)

        if mask is not None:
            out = out.masked_fill(mask, 0.0)

        return out


class Conv(nn.Module):
    """
    Convolution Module
    """

    def __init__(
        self,
        in_channels,
        out_channels,
        kernel_size=1,
        stride=1,
        padding=0,
        dilation=1,
        bias=True,
        w_init="linear",
    ):
        """
        :param in_channels: dimension of input
        :param out_channels: dimension of output
        :param kernel_size: size of kernel
        :param stride: size of stride
        :param padding: size of padding
        :param dilation: dilation rate
        :param bias: boolean. if True, bias is included.
        :param w_init: str. weight inits with xavier initialization.
        """
        super(Conv, self).__init__()

        self.conv = nn.Conv1d(
            in_channels,
            out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            bias=bias,
        )

    def forward(self, x):
        x = x.contiguous().transpose(1, 2)
        x = self.conv(x)
        x = x.contiguous().transpose(1, 2)

        return x

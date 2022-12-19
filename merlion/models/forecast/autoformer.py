#
# Copyright (c) 2022 salesforce.com, inc.
# All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# For full license text, see the LICENSE file in the repo root or https://opensource.org/licenses/BSD-3-Clause
#
"""
    Implementation of Autoformer: 
    Decomposition Transformers with Auto-Correlation for Long-Term Series Forecasting: https://arxiv.org/abs/2106.13008 
    Code adapted from https://github.com/thuml/Autoformer. 
"""
import copy
import logging
import math

import numpy as np
import pandas as pd
from scipy.stats import norm

from typing import List, Optional, Tuple, Union
from abc import abstractmethod

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
except ImportError as e:
    err = (
        "Try installing Merlion with optional dependencies using `pip install salesforce-merlion[deep-learning]` or "
        "`pip install `salesforce-merlion[all]`"
    )
    raise ImportError(str(e) + ". " + err)


from merlion.models.base import NormalizingConfig
from merlion.models.deep_base import TorchModel
from merlion.models.forecast.deep_base import DeepForecasterConfig, DeepForecaster

from merlion.models.utils.nn_modules import (
    AutoCorrelation,
    AutoCorrelationLayer,
    SeriesDecomposeBlock,
    SeasonalLayernorm,
    DataEmbeddingWoPos,
)

from merlion.models.utils.nn_modules.enc_dec_autoformer import Encoder, Decoder, EncoderLayer, DecoderLayer


from merlion.utils.misc import initializer

logger = logging.getLogger(__name__)


class AutoformerConfig(DeepForecasterConfig, NormalizingConfig):
    """
    Config object for autoformer forecaster
    """

    @initializer
    def __init__(
        self,
        n_past,
        max_forecast_steps: int = None,
        moving_avg: int = 25,
        enc_in: int = None,
        dec_in: int = None,
        e_layers: int = 2,
        d_layers: int = 1,
        factor: int = 3,
        d_model: int = 512,
        embed: str = "timeF",
        dropout: float = 0.05,
        activation: str = "gelu",
        n_heads: int = 8,
        d_ff: int = 2048,
        **kwargs
    ):
        """
        :param n_past: # of past steps used for forecasting future.
        :param max_forecast_steps:  Max # of steps we would like to forecast for.
        :param moving_avg: Window size of moving average for Autoformer.
        :param enc_in: Input size of encoder. If `enc_in = None`, then the model will automatically use `config.dim`,
            which is the dimension of the input data.
        :param dec_in: Input size of decoder. If `dec_in = None`, then the model will automatically use `config.dim`,
            which is the dimension of the input data.
        :param e_layers: Number of encoder layers.
        :param d_layers: Number of decoder layers.
        :param factor: Attention factor.
        :param d_model: Dimension of the model.
        :param embed: Time feature encoding type, options include `timeF`, `fixed` and `learned`.
        :param dropout: dropout rate.
        :param activation: Activation function, can be `gelu`, `relu`, `sigmoid`, etc.
        :param n_heads: Number of heads of the model.
        :param d_ff: Hidden dimension of the MLP layer in the model.
        """

        super().__init__(n_past=n_past, max_forecast_steps=max_forecast_steps, **kwargs)


class AutoformerModel(TorchModel):
    """
    Implementaion of Autoformer Deep Torch Model
    """

    def __init__(self, config: AutoformerConfig):
        super().__init__(config)

        if config.dim is not None:
            config.enc_in = config.dim if config.enc_in is None else config.enc_in
            config.dec_in = config.enc_in if config.dec_in is None else config.dec_in

        if config.target_seq_index is None:
            config.c_out = config.enc_in
        else:
            copnfig.c_out = 1

        self.n_past = config.n_past
        self.start_token_len = config.start_token_len
        self.max_forecast_steps = config.max_forecast_steps

        kernel_size = config.moving_avg
        self.decomp = SeriesDecomposeBlock(kernel_size)

        # Embedding
        # The series-wise connection inherently contains the sequential information.
        # Thus, we can discard the position embedding of transformers.
        self.enc_embedding = DataEmbeddingWoPos(
            config.enc_in, config.d_model, config.embed, config.ts_encoding, config.dropout
        )

        self.dec_embedding = DataEmbeddingWoPos(
            config.dec_in, config.d_model, config.embed, config.ts_encoding, config.dropout
        )

        # Encoder
        self.encoder = Encoder(
            [
                EncoderLayer(
                    AutoCorrelationLayer(
                        AutoCorrelation(False, config.factor, attention_dropout=config.dropout, output_attention=False),
                        config.d_model,
                        config.n_heads,
                    ),
                    config.d_model,
                    config.d_ff,
                    moving_avg=config.moving_avg,
                    dropout=config.dropout,
                    activation=config.activation,
                )
                for l in range(config.e_layers)
            ],
            norm_layer=SeasonalLayernorm(config.d_model),
        )

        # Decoder
        self.decoder = Decoder(
            [
                DecoderLayer(
                    AutoCorrelationLayer(
                        AutoCorrelation(True, config.factor, attention_dropout=config.dropout, output_attention=False),
                        config.d_model,
                        config.n_heads,
                    ),
                    AutoCorrelationLayer(
                        AutoCorrelation(False, config.factor, attention_dropout=config.dropout, output_attention=False),
                        config.d_model,
                        config.n_heads,
                    ),
                    config.d_model,
                    config.c_out,
                    config.d_ff,
                    moving_avg=config.moving_avg,
                    dropout=config.dropout,
                    activation=config.activation,
                )
                for l in range(config.d_layers)
            ],
            norm_layer=SeasonalLayernorm(config.d_model),
            projection=nn.Linear(config.d_model, config.c_out, bias=True),
        )

    def forward(
        self,
        past,
        past_timestamp,
        future_timestamp,
        enc_self_mask=None,
        dec_self_mask=None,
        dec_enc_mask=None,
        **kwargs
    ):
        config = self.config

        future_timestamp = torch.cat(
            [past_timestamp[:, (past_timestamp.shape[1] - self.start_token_len) :], future_timestamp], dim=1
        )

        # decomp init
        mean = torch.mean(past, dim=1).unsqueeze(1).repeat(1, self.max_forecast_steps, 1)
        zeros = torch.zeros(
            [past.shape[0], self.max_forecast_steps, past.shape[2]], dtype=torch.float, device=self.device
        )
        seasonal_init, trend_init = self.decomp(past)
        # decoder input
        trend_init = torch.cat([trend_init[:, (trend_init.shape[1] - self.start_token_len) :, :], mean], dim=1)
        seasonal_init = torch.cat(
            [seasonal_init[:, (seasonal_init.shape[1] - self.start_token_len) :, :], zeros], dim=1
        )

        # enc
        enc_out = self.enc_embedding(past, past_timestamp)
        enc_out, attns = self.encoder(enc_out, attn_mask=enc_self_mask)
        # dec
        dec_out = self.dec_embedding(seasonal_init, future_timestamp)
        seasonal_part, trend_part = self.decoder(
            dec_out, enc_out, x_mask=dec_self_mask, cross_mask=dec_enc_mask, trend=trend_init
        )
        # final
        dec_out = trend_part + seasonal_part

        return dec_out[:, -self.max_forecast_steps :, :]  # [B, L, D]


class AutoformerForecaster(DeepForecaster):
    """
    Implementaion of Autoformer deep forecaster
    """

    config_class = AutoformerConfig
    deep_model_class = AutoformerModel

    def __init__(self, config: AutoformerConfig):
        super().__init__(config)

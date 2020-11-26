# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import collections
import copy
import inspect
import os
import re
import tempfile
import types
from abc import ABC
from argparse import Namespace
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from weakref import proxy

import torch
from torch import ScriptModule, Tensor
from torch.nn import Module
from torch.optim import SGD
from torch.optim.optimizer import Optimizer

from pytorch_lightning import _logger as log
from pytorch_lightning.callbacks import Callback
from pytorch_lightning.core.grads import GradInformation
from pytorch_lightning.core.hooks import CheckpointHooks, DataHooks, ModelHooks
from pytorch_lightning.core.memory import ModelSummary
from pytorch_lightning.core.saving import ALLOWED_CONFIG_TYPES, PRIMITIVE_TYPES, ModelIO
from pytorch_lightning.core.step_result import Result
from pytorch_lightning.utilities import AMPType, rank_zero_warn
from pytorch_lightning.utilities.device_dtype_mixin import DeviceDtypeModuleMixin
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from pytorch_lightning.utilities.parsing import AttributeDict, collect_init_args, get_init_args
from pytorch_lightning.utilities.xla_device_utils import XLADeviceUtils

TPU_AVAILABLE = XLADeviceUtils.tpu_device_exists()

if TPU_AVAILABLE:
    import torch_xla.core.xla_model as xm


def do_nothing_closure():
    return


class LightningOptimizer(Optimizer):
    """
    This class is used to wrap the user optimizers and handle properly
    the backward and optimizer_step logic across accelerators, AMP, accumulated_grad_batches
    """
    def __init__(self,
                 optimizer: Optimizer,
                 accumulate_grad_batches: Optional[int] = None):

        assert accumulate_grad_batches is None or isinstance(accumulate_grad_batches, int)
        if isinstance(accumulate_grad_batches, int) and accumulate_grad_batches < 1:
            raise MisconfigurationException(f"accumulate_grad_batches parameters "
                                            f"{accumulate_grad_batches} should be >= 1")

        optim_dict = {}
        for k, v in optimizer.__dict__.items():
            if k != 'step':
                optim_dict[k] = v
        self.__dict__ = optim_dict

        # For Horovod
        if hasattr(optimizer, "skip_synchronize"):
            self.skip_synchronize = optimizer.skip_synchronize
            self.synchronize = optimizer.synchronize

        self._trainer = None
        self._optimizer = optimizer
        self._accumulate_grad_batches = accumulate_grad_batches
        self._use_accumulate_grad_batches_from_trainer = accumulate_grad_batches is None

    def _on_trainer_init(self, trainer):
        self._trainer = proxy(trainer)

    def _accumulated_batches_reached(self):
        if self._use_accumulate_grad_batches_from_trainer:
            accumulate_grad_batches = self._trainer.accumulate_grad_batches
        else:
            accumulate_grad_batches = self._accumulate_grad_batches
        return (self._trainer.batch_idx + 1) % accumulate_grad_batches == 0

    @property
    def _should_accumulate(self):
        # checks if backward or backward + optimizer step (via closure)
        accumulation_done = self._accumulated_batches_reached()
        is_final_batch = self._trainer.train_loop._num_training_batches_reached()
        return not (accumulation_done or is_final_batch)

    def step(self, *args, closure: Callable = None, make_optimizer_step: Optional[bool] = None, **kwargs):
        """
        Call this directly from your training_step when doing optimizations manually.
        By using this we can ensure that all the proper scaling when using 16-bit etc has been done for you

        .. tip:: In manual mode we still automatically accumulate grad over batches if
           Trainer(accumulate_grad_batches=x) is set.

        Args:
            closure: Closure should contain forward and backward step
            make_optimizer_step: Whether to force an optimizer step. When nothing is provided,
                we will use `accumulate_grad_batches` for accumulation frequency by default.
                However, one coud provide True and False based on its own scheduling.

        .. tip:: In manual mode we still automatically accumulate grad over batches if
           Trainer(accumulate_grad_batches=x) is set.

        Args:
            optimizer: Optimizer used to perform `.step()` call

            make_optimizer_step: Whether to force an optimizer step. When nothing is provided,
                we will use `accumulate_grad_batches` for accumulation frequency by default.
                However, one coud provide True and False based on its own scheduling.
                c.f example 2 and 3

            optimizer_closure: One could provide its own optimizer_closure. Set to None by default.

            args: Any parameters provided to optimizer.step()

            kwargs: Any parameters provided to optimizer.step()

        Example::

            def training_step(...):
                (opt_a, opt_b) = self.optimizers()
                loss_a = ...
                # automatically applies scaling, etc...
                self.manual_backward(loss_a, opt_a)
                opt_a.step()

        Example::

            def training_step(self, batch, batch_idx):
                # using Boring Model
                opt = self.optimizers() # only 1 optimizer

                def compute_loss():
                    x = batch[0]
                    x = F.dropout(x, 0.1)
                    predictions = self(x)
                    predictions = F.dropout(predictions, 0.1)
                    loss = self.loss(None, predictions)
                    return loss

                def closure():
                    # emulate MC dropout training
                    num_backward = 1
                    losses = []
                    for backward_idx in range(num_backward + 1):
                        loss = compute_loss()
                        losses.append(loss)
                        retain_graph = num_backward!= backward_idx
                        self.manual_backward(loss, opt, retain_graph=retain_graph)
                    loss_mean = torch.stack(losses).mean()
                    loss_std = torch.stack(losses).std()
                    self.log("train_loss_mean", loss_mean, on_step=True, prog_bar=True, on_epoch=True)
                    self.log("train_loss_std", loss_std, on_step=True, prog_bar=True, on_epoch=True)

                opt.step(loss, closure=closure)

        Example::

            # Scenario for a gan.

            def training_step(self, batch, batch_idx, optimizer_idx):

                # emulate gans training
                opt_gen, opt_dis = self.optimizers()

                # Note: Be careful, don't log on the same key in self.log in both closure
                # as they will be aggregated together on epoch_end

                def gen_closure():
                    ... forward and compute loss for generator
                    loss_gen = ...
                    self.log("loss_gen", loss_gen, on_step=True, on_epoch=True)
                    self.manual_backward(loss_gen, opt_gen)

                def dis_closure():
                    ... forward and compute loss for discriminator
                    loss_dis = ...
                    self.log("loss_dis", loss_dis, on_step=True, on_epoch=True)
                    self.manual_backward(loss_dis, opt_dis)

                # this will accumulate gradients for 2 batches and then call opt_gen.step()
                opt_gen.step(closure=gen_closure, make_optimizer_step=batch_idx % 2 == 0)

                # update discriminator every 4 batches
                # therefore, no gradient accumulation for discriminator
                if batch_idx % 4 == 0 :
                    # Note: Set make_optimizer_step to True or it will use by default
                    # Trainer(accumulate_grad_batches=x)
                    opt_dis.step(closure=optimizer_closure, make_optimizer_step=True)
        """

        profiler_name = "optimizer_step_and_closure"
        if closure is None:
            closure = do_nothing_closure
            profile_name = "optimizer_step"
        else:
            if not isinstance(closure, types.FunctionType):
                raise MisconfigurationException("When closure is provided, it should be a function")

        if make_optimizer_step is None:
            make_optimizer_step = not self._should_accumulate

        trainer = self._trainer
        optimizer = self._optimizer

        if make_optimizer_step:
            if trainer.on_tpu:
                with trainer.profiler.profile(profiler_name):
                    xm.optimizer_step(optimizer, optimizer_args={'closure': closure, **kwargs})

            elif trainer.amp_backend is not None:
                trainer.precision_connector.backend.step(trainer, optimizer, closure)

            else:
                with trainer.profiler.profile(profiler_name):
                    optimizer.step(closure=closure, *args, **kwargs)

            # perform zero grad
            optimizer.zero_grad()
        else:
            # make sure to call optimizer_closure when accumulating
            with trainer.train_loop.block_ddp_sync_behaviour():
                with trainer.profiler.profile("closure"):
                    closure()

    def __repr__(self):
        groups = "["
        for i, group in enumerate(self.param_groups):
            groups += '('
            params = ''
            for key in sorted(group.keys()):
                if key != 'params':
                    params += f'{key}={round(group[key], 12)}, '
            groups += params[:-2]
            groups += '),'
        groups = groups[:-1] + ']'
        format_string = f"{self.__class__.__name__}(optim={self._optimizer.__class__.__name__}, groups={groups})"
        return format_string

# Copyright 2019 The Sonnet Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or  implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Batch normalization module."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from sonnet.src import base
from sonnet.src import initializers
from sonnet.src import moving_averages
from sonnet.src import once
from sonnet.src import utils
import tensorflow as tf


class BaseBatchNorm(base.Module):
  r"""Batch normalization module.

  This implements normalization across the batch and spatial dimensions.
  It maintains moving averages of the mean and variance which can be
  used to normalize at test time. The constructor is generic and
  requires the user to pass in objects to compute these.

  At training time we use the batch statistics for that batch and these are then
  used to update the moving averages.

  At test time we can either use the moving averages of the batch statistics
  (``test_local_stats=False``) or we can use the local statistics
  (``test_local_stats=True``).

  It transforms the input ``x`` into:

  .. math::

      \d{outputs} = \d{scale} \dfrac{x - \mu}{\sigma + \epsilon} + \d{offset}

  Where :math:`\mu` and :math:`\sigma` are respectively the mean and standard
  deviation of ``x``. Note that this module automatically uses the fused batch
  norm op if the data format is ``NHWC``.

  There are many different variations for how users want to manage scale and
  offset if they require them at all. These are:

    - No scale/offset in which case ``create_*`` should be set to ``False`` and
      ``scale``/``offset`` aren't passed when the module is called.
    - Trainable scale/offset in which case ``create_*`` should be set to
      ``True`` and again ``scale``/``offset`` aren't passed when the module is
      called. In this case this module creates and owns the ``scale``/``offset``
      variables.
    - Externally generated ``scale``/``offset``, such as for conditional
      normalization, in which case ``create_*`` should be set to ``False`` and
      then the values fed in at call time.

  Attributes:
    scale: If ``create_scale``, a trainable :tf:`Variable` holding the current
      scale after the module is connected for the first time.
    offset: If ``create_offset``, a trainable :tf:`Variable` holding the current
      offset after the module is connected for the first time.
  """

  def __init__(self,
               create_scale,
               create_offset,
               moving_mean,
               moving_variance,
               eps=1e-5,
               scale_init=None,
               offset_init=None,
               data_format="channels_last",
               name=None):
    """Constructs a ``BaseBatchNorm`` module.

    Args:
      create_scale: ``bool`` representing whether to create a trainable scale
        per channel applied after the normalization.
      create_offset: ``bool`` representing whether to create a trainable offset
        per channel applied after normalization and scaling.
      moving_mean: An object which keeps track of the moving average of the mean
        which can be used to normalize at test time. This object must have an
        update method which takes a value and updates the internal state and a
        value property which returns the current mean.
      moving_variance: An object which keeps track of the moving average of the
        variance which can be used to normalize at test time. This object must
        have an update method which takes a value and updates the internal state
        and a value property which returns the current variance.
      eps: Small epsilon to avoid division by zero variance. Defaults to
        ``1e-5``.
      scale_init: Optional initializer for the scale variable. Can only be set
        if ``create_scale=True``. By default scale is initialized to ``1``.
      offset_init: Optional initializer for the offset variable. Can only be set
        if ``create_offset=True``. By default offset is initialized to ``0``.
      data_format: The data format of the input. Can be either
        ``channels_first``, ``channels_last``, ``N...C`` or ``NC...``. By
        default it is ``channels_last``.
      name: Name of the module.
    """
    super(BaseBatchNorm, self).__init__(name=name)

    self._eps = eps

    self.moving_mean = moving_mean
    self.moving_variance = moving_variance

    self._data_format = data_format
    self._channel_index = utils.get_channel_index(data_format)

    self._create_scale = create_scale
    self._create_offset = create_offset

    if not self._create_scale and scale_init is not None:
      raise ValueError("Cannot set `scale_init` if `create_scale=False`")
    self._scale_init = scale_init or initializers.Ones()
    if not self._create_offset and offset_init is not None:
      raise ValueError("Cannot set `offset_init` if `create_offset=False`")
    self._offset_init = offset_init or initializers.Zeros()

  @utils.smart_autograph
  def __call__(self,
               inputs,
               is_training,
               test_local_stats=False,
               scale=None,
               offset=None):
    """Returns normalized inputs.

    Args:
      inputs: An n-D tensor of the data_format specified above on which the
        transformation is performed.
      is_training: ``bool`` to indicate if the module should be connected in
        training mode, meaning the moving averages are updated.
      test_local_stats: ``bool`` to indicate if local batch statistics should be
        used when ``is_training=False``. If not, moving averages are used. By
        default ``False``.
      scale: A tensor up to n-D. The shape of this tensor must be broadcastable
        to the shape of ``inputs``. This is the scale applied to the normalized
        inputs. This cannot be passed in if the module was constructed with
        ``create_scale=True``.
      offset: A tensor up to n-D. The shape of this tensor must be broadcastable
        to the shape of ``inputs``. This is the offset applied to the normalized
        inputs. This cannot be passed in if the module was constructed with
        ``create_offset=True``.

    Returns:
      An n-d tensor of the same shape as inputs that has been normalized.
    """
    use_batch_stats = is_training or test_local_stats
    if self._create_scale:
      if scale is not None:
        raise ValueError(
            "Cannot pass `scale` at call time if `create_scale=True`.")

    if self._create_offset:
      if offset is not None:
        raise ValueError(
            "Cannot pass `offset` at call time if `create_offset=True`.")

    self._initialize(inputs)
    if scale is None:
      scale = self.scale
    if offset is None:
      offset = self.offset

    mean, variance = self._moments(inputs, use_batch_stats)

    if self._fused:
      out, mean, variance, _, _ = tf.raw_ops.FusedBatchNormV2(
          x=inputs,
          mean=mean,
          variance=variance,
          scale=scale,
          offset=offset,
          is_training=use_batch_stats,
          epsilon=self._eps,
          data_format=self._fused_data_format)

    else:
      out = tf.nn.batch_normalization(
          inputs,
          mean=mean,
          variance=variance,
          scale=scale,
          offset=offset,
          variance_epsilon=self._eps)

    if is_training:
      self._update_statistics(mean, variance)

    return out

  @once.once
  def _initialize(self, inputs):
    input_shape = inputs.shape
    rank = len(input_shape)
    self._fused = (rank == 4 and self._channel_index == -1)
    self._fused_data_format = "NHWC" if self._channel_index == -1 else "NCHW"
    if self._channel_index < 0:
      channel_index = self._channel_index + rank
    else:
      channel_index = self._channel_index
    self._axis = tuple(i for i in range(rank) if i != channel_index)

    # Ensure all the variables are created on the first call
    mean, variance = tf.nn.moments(inputs, self._axis, keepdims=True)
    self.shape = mean.shape
    self.moving_mean.initialize(mean)
    self.moving_variance.initialize(variance)

    dtype = inputs.dtype

    if self._channel_index == -1:
      params_shape = [inputs.shape[-1]]
    else:  # self._channel_index == 1
      params_shape = [inputs.shape[1]] + [1] * (rank - 2)
    # Creates scale and offset parameters - required for fused_batch_norm
    # trainable set to with_scale and with_offset which gives no-op if false
    self.scale = tf.Variable(
        self._scale_init(params_shape, dtype),
        name="scale",
        trainable=self._create_scale)

    self.offset = tf.Variable(
        self._offset_init(params_shape, dtype),
        name="offset",
        trainable=self._create_offset)

    if self._fused:
      with tf.init_scope():
        self._fused_constant = tf.constant([])

  def _moments(self, inputs, use_batch_stats):
    if use_batch_stats:
      if self._fused:
        # The raw ops version of fused batch norm calculates the mean and
        # variance internally but requires tensors to be passed in.
        mean = self._fused_constant
        variance = self._fused_constant
      else:
        mean, variance = tf.nn.moments(inputs, self._axis, keepdims=True)
    else:  # use moving stats
      mean = self.moving_mean.value
      variance = self.moving_variance.value
      if self._fused:
        mean = tf.squeeze(mean)
        variance = tf.squeeze(variance)
    return mean, variance

  def _update_statistics(self, mean, variance):
    if self._fused:
      mean = tf.reshape(mean, self.shape)
      variance = tf.reshape(variance, self.shape)
    self.moving_mean.update(mean)
    self.moving_variance.update(variance)


class BatchNorm(BaseBatchNorm):
  """Batch normalization with exponential moving average for test statistics.

  See :class:`BaseBatchNorm` for details.

  Attributes:
    scale: If ``create_scale=True``, a trainable :tf:`Variable` holding the
      current scale after the module is connected for the first time.
    offset: If ``create_offset``, a trainable :tf:`Variable` holding the current
      offset after the module is connected for the first time.
  """

  def __init__(self,
               create_scale,
               create_offset,
               decay_rate=0.999,
               eps=1e-5,
               scale_init=None,
               offset_init=None,
               data_format="channels_last",
               name=None):
    """Constructs a ``BatchNorm`` module.

    Args:
      create_scale: ``bool`` representing whether to create a trainable scale
        per channel applied after the normalization.
      create_offset: ``bool`` representing whether to create a trainable offset
        per channel applied after normalization and scaling.
      decay_rate: Decay rate of the exponential moving averages of the mean and
        variance.
      eps: Small epsilon to avoid division by zero variance. Defaults to
        ``1e-5``.
      scale_init: Optional initializer for the scale variable. Can only be set
        if ``create_scale=True``. By default scale is initialized to ``1``.
      offset_init: Optional initializer for the offset variable. Can only be set
        if ``create_offset=True``. By default offset is initialized to ``0``.
      data_format: The data format of the input. Can be either
        ``channels_first``, ``channels_last``, ``N...C`` or ``NC...``. By
        default it is ``channels_last``.
      name: Name of the module.
    """
    with tf.name_scope(name or "batch_norm"):
      moving_mean = moving_averages.ExponentialMovingAverage(decay_rate)
      moving_variance = moving_averages.ExponentialMovingAverage(decay_rate)

    super(BatchNorm, self).__init__(
        create_scale=create_scale,
        create_offset=create_offset,
        moving_mean=moving_mean,
        moving_variance=moving_variance,
        eps=eps,
        scale_init=scale_init,
        offset_init=offset_init,
        data_format=data_format,
        name=name)


class CrossReplicaBatchNorm(BaseBatchNorm):
  """Cross-replica Batch Normalization.

  At every step the full batch is used to calculate the batch statistics even
  within a distributed setting (note only with `snt.(Tpu)Replicator`).

  See :class:`BaseBatchNorm` for details.

  Attributes:
    scale: If ``create_scale=True``, a trainable :tf:`Variable` holding the
      current scale after the module is connected for the first time.
    offset: If ``create_offset``, a trainable :tf:`Variable` holding the current
      offset after the module is connected for the first time.
  """

  def __init__(self,
               create_scale,
               create_offset,
               moving_mean,
               moving_variance,
               eps=1e-5,
               scale_init=None,
               offset_init=None,
               data_format="channels_last",
               name=None):
    """Constructs a ``BaseBatchNorm`` module.

    Args:
      create_scale: ``bool`` representing whether to create a trainable scale
        per channel applied after the normalization.
      create_offset: ``bool`` representing whether to create a trainable offset
        per channel applied after normalization and scaling.
      moving_mean: An object which keeps track of the moving average of the mean
        which can be used to normalize at test time. This object must have an
        update method which takes a value and updates the internal state and a
        value property which returns the current mean.
      moving_variance: An object which keeps track of the moving average of the
        variance which can be used to normalize at test time. This object must
        have an update method which takes a value and updates the internal state
        and a value property which returns the current variance.
      eps: Small epsilon to avoid division by zero variance. Defaults to
        ``1e-5``.
      scale_init: Optional initializer for the scale variable. Can only be set
        if ``create_scale=True``. By default scale is initialized to ``1``.
      offset_init: Optional initializer for the offset variable. Can only be set
        if ``create_offset=True``. By default offset is initialized to ``0``.
      data_format: The data format of the input. Can be either
        ``channels_first``, ``channels_last``, ``N...C`` or ``NC...``. By
        default it is ``channels_last``.
      name: Name of the module.
    """
    super(CrossReplicaBatchNorm, self).__init__(
        create_scale=create_scale,
        create_offset=create_offset,
        moving_mean=moving_mean,
        moving_variance=moving_variance,
        eps=eps,
        scale_init=scale_init,
        offset_init=offset_init,
        data_format=data_format,
        name=name)

  @once.once
  def _initialize(self, inputs):
    super(CrossReplicaBatchNorm, self)._initialize(inputs)

    # Always use the unfused op here as mean/var are calculated before the op is
    # called so no speed-up is gained from the fused op
    self._fused = False

  def _moments(self, inputs, use_batch_stats):
    replica_context = tf.distribute.get_replica_context()
    if replica_context is None:
      raise TypeError(
          "Cross replica batch norm cannot be called in cross-replica context.")

    if use_batch_stats:
      # Note: This uses var=E(x^2) - E(x)^2 instead of the more numerically
      # stable var=E((x-E(x))^2) as this means that with XLA the all_reduces can
      # be combined and a fusion removed giving significant speed-up.
      # If you see NaNs in your model please try the alternative formula and
      # file a bug with your use-case.
      mean = tf.reduce_mean(inputs, self._axis, keepdims=True)
      mean = replica_context.all_reduce("MEAN", mean)
      mean_of_squares = tf.reduce_mean(
          tf.square(inputs), self._axis, keepdims=True)
      mean_of_squares = replica_context.all_reduce("MEAN", mean_of_squares)
      mean_squared = tf.square(mean)
      var = mean_of_squares - mean_squared
      return mean, var

    else:  # use moving statistics
      mean = self.moving_mean.value
      variance = self.moving_variance.value
      return mean, variance

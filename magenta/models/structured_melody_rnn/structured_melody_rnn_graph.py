# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Provides function to build an structured melody RNN model's graph."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# internal imports
import six
import tensorflow as tf
import magenta

from tensorflow.python.util import nest as tf_nest


def make_rnn_cell(rnn_layer_sizes,
                  dropout_keep_prob=1.0,
                  attn_length=0,
                  base_cell=tf.contrib.rnn.BasicLSTMCell):
  """Makes a RNN cell from the given hyperparameters.

  Args:
    rnn_layer_sizes: A list of integer sizes (in units) for each layer of the
        RNN.
    dropout_keep_prob: The float probability to keep the output of any given
        sub-cell.
    attn_length: The size of the attention vector.
    base_cell: The base tf.contrib.rnn.RNNCell to use for sub-cells.

  Returns:
      A tf.contrib.rnn.MultiRNNCell based on the given hyperparameters.
  """
  cells = []
  for num_units in rnn_layer_sizes:
    cell = base_cell(num_units)
    if attn_length and not cells:
      # Add attention wrapper to first layer.
      cell = tf.contrib.rnn.AttentionCellWrapper(
          cell, attn_length, state_is_tuple=True)
    cell = tf.contrib.rnn.DropoutWrapper(
        cell, output_keep_prob=dropout_keep_prob)
    cells.append(cell)

  cell = tf.contrib.rnn.MultiRNNCell(cells)

  return cell


def extract_input_windows(inputs, batch_size, input_size, window_size):
  """Extracts sliding windows from a batch of input sequences.

  Args:
    inputs: A tensor of input sequences with shape
        `[batch_size, num_steps, input_size]`.
    batch_size: The number of sequences per batch.
    input_size: The size of the input representation per step.
    window_size: The number of steps to use per window.

  Returns:
    A tensor with shape
    `[batch_size, num_steps - window_size + 1, input_size, window_size]`
    containing the input window at each step.
  """
  input_windows = tf.extract_image_patches(
      tf.expand_dims(inputs, -1), ksizes=[1, window_size, input_size, 1],
      strides=[1, 1, 1, 1], rates=[1, 1, 1, 1], padding='VALID')

  # TODO(iansimon): do we need to transpose the last two dimensions?
  return tf.reshape(input_windows, [batch_size, -1, input_size, window_size])


def encode_input_windows(input_windows, batch_size, input_size, window_size,
                         encoding_size):
  """Encodes a sequence of input windows using shared weights.

  Args:
    input_windows: A tensor of input windows with shape
        `[batch_size, num_steps, input_size, window_size]`.
    batch_size: The number of sequences per batch.
    input_size: The size of the input representation per step.
    window_size: The number of steps in an input window.
    encoding_size: The size of the final encoding, used to compute self-
        similarities.

  Returns:
    A tensor with shape `[batch_size, num_steps, encoding_size]` containing the
    encoded input window at each step.
  """
  input_windows_flat = tf.reshape(
      input_windows, [batch_size, -1, input_size * window_size, 1])

  # This isn't really a 2D convolution, but a fully-connected layer operating on
  # flattened windows.
  encodings = tf.contrib.layers.conv2d(
      input_windows_flat, encoding_size, [1, input_size * window_size],
      padding='VALID', activation_fn=tf.nn.relu)

  return tf.squeeze(encodings, axis=2)


def similarity_weighted_attention(labels, self_similarity, num_classes):
  """Computes similarity-weighted softmax attention over past labels.

  For each step, computes an attention-weighted sum of the one-hot-encoded label
  at prior steps, where attention is determined by self-similarity.

  The final label step is assumed to immediately precede the final input step,
  i.e. the final input step can attend to all labels.

  Args:
    labels: A tensor of label sequences with shape
        `[batch_size, num_label_steps]`.
    self_similarity: A tensor of input self-similarities based on encoded
        windows, with shape `[batch_size, num_input_steps, num_label_steps]`.
    num_classes: The number of classes to use in the one-hot encoding.

  Returns:
    A tensor with shape `[batch_size, num_input_steps, num_classes]` containing
    the similarity-weighted attention over labels for each step.
  """
  num_input_steps = tf.shape(self_similarity)[1]
  num_label_steps = tf.shape(self_similarity)[2]

  steps = tf.range(num_label_steps - num_input_steps + 1, num_label_steps + 1)
  permuted_self_similarity = tf.transpose(self_similarity, [1, 0, 2])

  def similarity_to_attention(enumerated_similarity):
    step, sim = enumerated_similarity
    return tf.concat(
        [tf.nn.softmax(sim[:, :step]), tf.zeros_like(sim[:, step:])], axis=-1)

  permuted_attention = tf.map_fn(
      similarity_to_attention, (steps, permuted_self_similarity),
      dtype=tf.float32)
  attention = tf.transpose(permuted_attention, [1, 0, 2])

  return tf.matmul(attention, tf.one_hot(labels, num_classes))


def build_graph(mode, config, sequence_example_file_paths=None):
  """Builds the TensorFlow graph.

  Args:
    mode: 'train', 'eval', or 'generate'. Only mode related ops are added to
        the graph.
    config: An EventSequenceRnnConfig containing the encoder/decoder and HParams
        to use.
    sequence_example_file_paths: A list of paths to TFRecord files containing
        tf.train.SequenceExample protos. Only needed for training and
        evaluation.

  Returns:
    A tf.Graph instance which contains the TF ops.

  Raises:
    ValueError: If mode is not 'train', 'eval', or 'generate'.
  """
  if mode not in ('train', 'eval', 'generate'):
    raise ValueError("The mode parameter must be 'train', 'eval', "
                     "or 'generate'. The mode parameter was: %s" % mode)

  hparams = config.hparams
  encoder_decoder = config.encoder_decoder

  tf.logging.info('hparams = %s', hparams.values())

  input_size = encoder_decoder.input_size
  num_classes = encoder_decoder.num_classes
  no_event_label = encoder_decoder.default_event_label

  with tf.Graph().as_default() as graph:
    inputs, labels, lengths = None, None, None

    if mode == 'train' or mode == 'eval':
      inputs, labels, lengths = magenta.common.get_padded_batch(
          sequence_example_file_paths, hparams.batch_size, input_size,
          shuffle=mode == 'train')
      # When training we get full inputs, so to form windows we pad with zeros.
      input_buffer = tf.zeros(
          [hparams.batch_size, hparams.window_size - 1, input_size])
      # And there are no past encodings.
      past_encodings = tf.zeros([hparams.batch_size, 0, hparams.encoding_size])
      # And at no point can we attend to the final label.
      target_labels = labels[:, :-1]

    elif mode == 'generate':
      inputs = tf.placeholder(tf.float32, [hparams.batch_size, None,
                                           input_size])
      # At generation time we form windows using past inputs.
      input_buffer = tf.placeholder(tf.float32, [hparams.batch_size,
                                                 hparams.window_size - 1,
                                                 input_size])
      # When generating, we need to attend over all past labels.
      labels = tf.placeholder(tf.int64, [hparams.batch_size, None])
      past_encodings = tf.placeholder(tf.float32, [hparams.batch_size, None,
                                                   hparams.encoding_size])
      target_labels = labels

    # Extract sliding windows from the input sequences.
    padded_inputs = tf.concat([input_buffer, inputs], axis=1)
    input_windows = extract_input_windows(
        padded_inputs, hparams.batch_size, input_size, hparams.window_size)

    # Encode input windows.
    encodings = encode_input_windows(
        input_windows, hparams.batch_size, input_size, hparams.window_size,
        hparams.encoding_size)

    # Compute similarity between current encodings and all past and current
    # encodings except the most recent.
    target_encodings = tf.concat([past_encodings, encodings[:, :-1, :]], axis=1)
    self_similarity = tf.matmul(encodings, target_encodings, transpose_b=True)

    # Compute and append similarity-weighted attention on past labels.
    attention_inputs = similarity_weighted_attention(
        target_labels, self_similarity, num_classes)
    combined_inputs = tf.concat([inputs, attention_inputs], axis=2)

    cell = make_rnn_cell(
        hparams.rnn_layer_sizes,
        dropout_keep_prob=(
            1.0 if mode == 'generate' else hparams.dropout_keep_prob),
        attn_length=(
            hparams.attn_length if hasattr(hparams, 'attn_length') else 0))

    initial_state = cell.zero_state(hparams.batch_size, tf.float32)

    outputs, final_state = tf.nn.dynamic_rnn(
        cell, combined_inputs, sequence_length=lengths,
        initial_state=initial_state, swap_memory=True)

    outputs_flat = magenta.common.flatten_maybe_padded_sequences(
        outputs, lengths)
    logits_flat = tf.contrib.layers.linear(outputs_flat, num_classes)

    if mode == 'train' or mode == 'eval':
      labels_flat = magenta.common.flatten_maybe_padded_sequences(
          labels, lengths)

      softmax_cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(
          labels=labels_flat, logits=logits_flat)

      predictions_flat = tf.argmax(logits_flat, axis=1)
      correct_predictions = tf.to_float(
          tf.equal(labels_flat, predictions_flat))
      event_positions = tf.to_float(tf.not_equal(labels_flat, no_event_label))
      no_event_positions = tf.to_float(tf.equal(labels_flat, no_event_label))

      if mode == 'train':
        loss = tf.reduce_mean(softmax_cross_entropy)
        perplexity = tf.exp(loss)
        accuracy = tf.reduce_mean(correct_predictions)
        event_accuracy = (
            tf.reduce_sum(correct_predictions * event_positions) /
            tf.reduce_sum(event_positions))
        no_event_accuracy = (
            tf.reduce_sum(correct_predictions * no_event_positions) /
            tf.reduce_sum(no_event_positions))

        optimizer = tf.train.AdamOptimizer(learning_rate=hparams.learning_rate)

        train_op = tf.contrib.slim.learning.create_train_op(
            loss, optimizer, clip_gradient_norm=hparams.clip_norm)
        tf.add_to_collection('train_op', train_op)

        vars_to_summarize = {
            'loss': loss,
            'metrics/perplexity': perplexity,
            'metrics/accuracy': accuracy,
            'metrics/event_accuracy': event_accuracy,
            'metrics/no_event_accuracy': no_event_accuracy,
        }

        tf.summary.image('self-similarity', tf.expand_dims(self_similarity, -1))

      elif mode == 'eval':
        vars_to_summarize, update_ops = tf.contrib.metrics.aggregate_metric_map(
            {
                'loss': tf.metrics.mean(softmax_cross_entropy),
                'metrics/accuracy': tf.metrics.accuracy(
                    labels_flat, predictions_flat),
                'metrics/per_class_accuracy':
                    tf.metrics.mean_per_class_accuracy(
                        labels_flat, predictions_flat, num_classes),
                'metrics/event_accuracy': tf.metrics.recall(
                    event_positions, correct_predictions),
                'metrics/no_event_accuracy': tf.metrics.recall(
                    no_event_positions, correct_predictions),
            })

        for updates_op in update_ops.values():
          tf.add_to_collection('eval_ops', updates_op)

        # Perplexity is just exp(loss) and doesn't need its own update op.
        vars_to_summarize['metrics/perplexity'] = tf.exp(
            vars_to_summarize['loss'])

      for var_name, var_value in six.iteritems(vars_to_summarize):
        tf.summary.scalar(var_name, var_value)
        tf.add_to_collection(var_name, var_value)

    elif mode == 'generate':
      temperature = tf.placeholder(tf.float32, [])
      softmax_flat = tf.nn.softmax(
          tf.div(logits_flat, tf.fill([num_classes], temperature)))
      softmax = tf.reshape(softmax_flat, [hparams.batch_size, -1, num_classes])

      tf.add_to_collection('inputs', inputs)
      tf.add_to_collection('input_buffer', input_buffer)
      tf.add_to_collection('labels', labels)
      tf.add_to_collection('past_encodings', past_encodings)
      tf.add_to_collection('encodings', encodings)
      tf.add_to_collection('temperature', temperature)
      tf.add_to_collection('softmax', softmax)

      # Flatten state tuples for metagraph compatibility.
      for state in tf_nest.flatten(initial_state):
        tf.add_to_collection('initial_state', state)
      for state in tf_nest.flatten(final_state):
        tf.add_to_collection('final_state', state)

  return graph

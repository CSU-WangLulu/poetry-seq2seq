#! /usr/bin/env python
#-*- coding:utf-8 -*-

# standard
import os
from IPython import embed


# framework
import tensorflow as tf
from tensorflow.contrib import rnn, seq2seq

from tensorflow.python.ops.rnn_cell import GRUCell
from tensorflow.python.ops.rnn_cell import LSTMCell
from tensorflow.python.ops.rnn_cell import MultiRNNCell
from tensorflow.python.ops.rnn_cell import DropoutWrapper, ResidualWrapper

from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.layers.core import Dense
from tensorflow.python.util import nest

from tensorflow.contrib.seq2seq.python.ops import attention_wrapper
from tensorflow.contrib.seq2seq.python.ops import beam_search_decoder

# custom
from utils import save_dir

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

_model_path = os.path.join(save_dir, 'model')

_VOCAB_SIZE = 6000
_NUM_UNITS = 128
_NUM_LAYERS = 4
_BATCH_SIZE = 64


class Generator:
    """
    Seq2Seq model based on tensorflow.contrib.seq2seq
    """
    def build_model(self):
        print 'Building model...'

        # Build encoder and decoder networks
        self.init_placeholders()
        self.build_encoder()
        self.build_decoder()

        # Merge all the training summaries
        self.summary_op = tf.summary.merge_all()

    def init_placeholders(self):
        # embedding_placeholder: [vocab_size, hidden_units]
        self.embedding_placeholder = tf.placeholder(
            name='embedding_placeholder',
            shape=[self.vocab_size, self.hidden_units],
            dtype=self.dtype
        )

        self.embedding = tf.get_variable(
            name='embedding',
            shape=[self.vocab_size, self.hidden_units],
            trainable=False,
        )

        self.init_embedding = self.embedding.assign(self.embedding_placeholder)

        # encode_inputs: [batch_size, time_steps]
        self.encoder_inputs = tf.placeholder(
            name='encoder_inputs',
            shape=(None, None),
            dtype=tf.int32
        )

        # encoder_inputs_length: [batch_size]
        self.encoder_inputs_length = tf.placeholder(
            name='encoder_inputs_length',
            shape=(None,),
            dtype=tf.int32
        )

        if self.mode == 'train':
            # decoder_inputs: [batch_size, max_time_steps]
            self.decoder_inputs = tf.placeholder(
                dtype=tf.int32,
                shape=(None, None),
                name='decoder_inputs'
            )

            # decoder_inputs_length: [batch_size]
            self.decoder_inputs_length = tf.placeholder(
                dtype=tf.int32,
                shape=(None,),
                name='decoder_inputs_length'
            )

            # TODO(sdsuo): Make corresponding modification in data_utils
            decoder_start_token = tf.ones(
                shape=[self.batch_size, 1],
                dtype=tf.int32
            ) * self.start_token
            decoder_end_token = tf.ones(
                shape=[self.batch_size, 1],
                dtype=tf.int32
            ) * self.end_token

            # decoder_inputs_train: [batch_size , max_time_steps + 1]
            # insert _GO symbol in front of each decoder input
            self.decoder_inputs_train = tf.concat([decoder_start_token,
                                                  self.decoder_inputs], axis=1)

            # decoder_inputs_length_train: [batch_size]
            self.decoder_inputs_length_train = self.decoder_inputs_length + 1

            # decoder_targets_train: [batch_size, max_time_steps + 1]
            # insert EOS symbol at the end of each decoder input
            self.decoder_targets_train = tf.concat([self.decoder_inputs,
                                                   decoder_end_token], axis=1)

    def build_single_cell(self):
        if self.cell_type == 'gru':
            cell_type = GRUCell
        elif self.cell_type == 'lstm':
            cell_type = LSTMCell
        else:
            raise RuntimeError('Unknown cell type!')
        cell = cell_type(self.hidden_units)

        return cell

    def build_encoder_cell(self):
        multi_cell = MultiRNNCell([self.build_single_cell() for _ in range(self.depth)])

        return multi_cell

    def build_encoder(self):
        print 'Building encoder...'
        with tf.variable_scope('encoder'):
            # Build encoder cell
            self.encoder_cell = self.build_encoder_cell()


            # embedded inputs: [batch_size, time_step, embedding_size]
            self.encoder_inputs_embedded = tf.nn.embedding_lookup(
                params=self.embedding,
                ids=self.encoder_inputs
            )

            # TODO(sdsuo): Decide if we need a Dense input layer here

            # Encode input sequences into context vectors
            # encoder_outputs: [batch_size, time_step, cell_output_size]
            # encoder_last_state: [batch_size, cell_output_size]
            self.encoder_outputs, self.encoder_last_state = tf.nn.dynamic_rnn(
                cell=self.encoder_cell,
                inputs=self.encoder_inputs_embedded,
                sequence_length=self.encoder_inputs_length,
                dtype=self.dtype,
                time_major=False
            )

    def build_decoder_cell(self):
        # TODO(sdsuo): Read up and decide whether to use beam search
        self.attention_mechanism = seq2seq.BahdanauAttention(
            num_units=self.hidden_units,
            memory=self.encoder_outputs,
            memory_sequence_length=self.encoder_inputs_length
        )

        self.decoder_cell_list = [
            self.build_single_cell() for _ in range(self.depth)
        ]

        # NOTE(sdsuo): Not sure what this does yet
        def attn_decoder_input_fn(inputs, attention):
            pass

        # NOTE(sdsuo): Attention mechanism is implemented only on the top decoder layer
        self.decoder_cell_list[-1] = seq2seq.AttentionWrapper(
            cell=self.decoder_cell_list[-1],
            attention_mechanism=self.attention_mechanism,
            attention_layer_size=self.hidden_units,
            cell_input_fn=attn_decoder_input_fn,
            initial_cell_state=self.encoder_last_state[-1],
            alignment_history=False,
            name='attention_wrapper'
        )

        # NOTE(sdsuo): Not sure why this is necessary
        # To be compatible with AttentionWrapper, the encoder last state
        # of the top layer should be converted into the AttentionWrapperState form
        # We can easily do this by calling AttentionWrapper.zero_state

        # Also if beamsearch decoding is used, the batch_size argument in .zero_state
        # should be ${decoder_beam_width} times to the origianl batch_size
        if self.use_beamsearch_decode:
            batch_size = self.batch_size * self.beam_width
        else:
            batch_size = self.batch_size

        initial_state = [state for state in self.encoder_last_state]
        initial_state[-1] = self.decoder_cell_list[-1].zero_state(
            batch_size=batch_size,
            dtype=self.dtype
        )
        decoder_initial_state = tuple(initial_state)


        return MultiRNNCell(self.decoder_cell_list), decoder_initial_state


    def build_decoder(self):
        print 'Building decoder...'
        with tf.variable_scope('decoder'):
            # Building decoder_cell and decoder_initial_state
            self.decoder_cell, self.decoder_initial_state = self.build_decoder_cell()

            # Output projection layer to convert cell_outputs to logits
            output_layer = Dense(self.vocab_size, name='output_projection')

            if self.mode == 'train':
                self.decoder_inputs_embedded = tf.nn.embedding_lookup(
                    params=self.embedding,
                    ids=self.decoder_inputs_train
                )

                training_helper = seq2seq.TrainingHelper(
                    inputs=self.decoder_inputs_embedded,
                    sequence_length=self.decoder_inputs_length_train,
                    time_major=False,
                    name='training_helper'
                )
                training_decoder = seq2seq.BasicDecoder(
                    cell=self.decoder_cell,
                    helper=training_helper,
                    initial_state=self.decoder_initial_state,
                    output_layer=output_layer
                )
                max_decoder_length = tf.reduce_max(self.decoder_inputs_length_train)

                embed()

                self.decoder_outputs_train, self.decoder_last_state_train, self.decoder_outputs_length_train = seq2seq.dynamic_decode(
                    decoder=training_decoder,
                    output_time_major=False,
                    impute_finished=True,
                    maximum_iterations=max_decoder_length
                )

                # NOTE(sdsuo): Not sure why this is necessary
                self.decoder_logits_train = tf.identity(self.decoder_outputs_train.rnn_output)

                 # Use argmax to extract decoder symbols to emit
                self.decoder_pred_train = tf.argmax(
                    self.decoder_logits_train,
                    axis=-1,
                    name='decoder_pred_train'
                )

                # masks: masking for valid and padded time steps, [batch_size, max_time_step + 1]
                masks = tf.sequence_mask(
                    lengths=self.decoder_inputs_length_train,
                    maxlen=max_decoder_length,
                    dtype=self.dtype,
                    name='masks'
                )

                # Computes per word average cross-entropy over a batch
                # Internally calls 'nn_ops.sparse_softmax_cross_entropy_with_logits' by default
                self.loss = seq2seq.sequence_loss(
                    logits=self.decoder_logits_train,
                    targets=self.decoder_targets_train,
                    weights=masks,
                    average_across_timesteps=True,
                    average_across_batch=True
                )

                # Training summary for the current batch_loss
                tf.summary.scalar('loss', self.loss)

                # Contruct graphs for minimizing loss
                self.init_optimizer()


            elif self.mode == 'decode':
                pass
            else:
                raise RuntimeError

    def init_optimizer(self):
        print("Setting optimizer..")
        # Gradients and SGD update operation for training the model
        trainable_params = tf.trainable_variables()
        if self.optimizer.lower() == 'adadelta':
            self.opt = tf.train.AdadeltaOptimizer(learning_rate=self.learning_rate)
        elif self.optimizer.lower() == 'adam':
            self.opt = tf.train.AdamOptimizer(learning_rate=self.learning_rate)
        elif self.optimizer.lower() == 'rmsprop':
            self.opt = tf.train.RMSPropOptimizer(learning_rate=self.learning_rate)
        else:
            self.opt = tf.train.GradientDescentOptimizer(learning_rate=self.learning_rate)

        # Compute gradients of loss w.r.t. all trainable variables
        gradients = tf.gradients(self.loss, trainable_params)

        # Clip gradients by a given maximum_gradient_norm
        clip_gradients, _ = tf.clip_by_global_norm(gradients, self.max_gradient_norm)

        # Update the model
        self.updates = self.opt.apply_gradients(
            zip(clip_gradients, trainable_params), global_step=self.global_step)

    def __init__(self):
        self.vocab_size = 6000
        self.hidden_units = _NUM_UNITS
        self.depth = _NUM_LAYERS
        self.cell_type = 'lstm'
        self.dtype = tf.float32
        self.batch_size = 64
        self.optimizer = 'adam'
        self.max_gradient_norm = 10
        self.global_step = 100
        self.mode = 'train'
        self.start_token = 0
        self.end_token = -1
        self.use_beamsearch_decode = False
        self.beam_width = 5
        self.learning_rate = 0.1

        self.build_model()

    def _init_vars(self, sess):
        pass

    def _train_a_batch(self, sess, kw_mats, kw_lens, s_mats, s_lens):
        pass

    def train(self, n_epochs = 6, learn_rate = 0.002, decay_rate = 0.97):
        pass

    def generate(self, keywords):
        pass

if __name__ == '__main__':
    generator = Generator()


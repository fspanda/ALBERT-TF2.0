# coding=utf-8
# Copyright 2019 The Google Research Authors.
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

# Lint as: python2, python3
"""BERT finetuning runner with sentence piece tokenization."""

from __future__ import absolute_import, division, print_function

import collections
import csv
import functools
import json
import os

import numpy as np
import six
import tensorflow as tf
from absl import app, flags, logging
from six.moves import zip

import tokenization
from albert import AlbertConfig, AlbertModel
from input_pipeline import create_classifier_dataset
from optimization import LAMB, WarmUp

FLAGS = flags.FLAGS

## Required parameters
flags.DEFINE_string(
    "train_data_path", None,
    "train_data path for tfrecords for the task.")

flags.DEFINE_string(
    "eval_data_path", None,
    "eval_data path for tfrecords for the task.")

flags.DEFINE_string(
    "albert_config_file", None,
    "The config json file corresponding to the pre-trained ALBERT model. "
    "This specifies the model architecture.")

flags.DEFINE_string("task_name", None, "The name of the task to train.")

flags.DEFINE_string(
    "vocab_file", None,
    "The vocabulary file that the ALBERT model was trained on.")

flags.DEFINE_string("spm_model_file", None,
                    "The model file for sentence piece tokenization.")

flags.DEFINE_string(
    "output_dir", None,
    "The output directory where the model checkpoints will be written.")

flags.DEFINE_enum(
    "strategy_type", "one", ["one", "mirror"],
    "Training strategy for single or multi gpu training")

## Other parameters

flags.DEFINE_string(
    "init_checkpoint", None,
    "Initial checkpoint (usually from a pre-trained ALBERT model).")

flags.DEFINE_string("input_meta_data_path",None,"input_meta_data_path")


flags.DEFINE_bool(
    "do_lower_case", True,
    "Whether to lower case the input text. Should be True for uncased "
    "models and False for cased models.")

flags.DEFINE_integer(
    "max_seq_length", 512,
    "The maximum total input sequence length after WordPiece tokenization. "
    "Sequences longer than this will be truncated, and sequences shorter "
    "than this will be padded.")

flags.DEFINE_float("classifier_dropout",0.1,"classification layer dropout")

flags.DEFINE_bool("do_train", False, "Whether to run training.")

flags.DEFINE_bool("do_eval", False, "Whether to run eval on the dev set.")

flags.DEFINE_integer("train_batch_size", 32, "Total batch size for training.")

flags.DEFINE_integer("eval_batch_size", 8, "Total batch size for eval.")

flags.DEFINE_float("learning_rate", 5e-5, "The initial learning rate for Adam.")

flags.DEFINE_float("weight_decay", 0.01, "weight_decay")

flags.DEFINE_float("adam_epsilon", 1e-6, "adam_epsilon")

flags.DEFINE_integer("num_train_epochs", 3,
                   "Total number of training epochs to perform.")

flags.DEFINE_bool("enable_xla",False, "enables XLA")

flags.DEFINE_float(
    "warmup_proportion", 0.1,
    "Proportion of training to perform linear learning rate warmup for. "
    "E.g., 0.1 = 10% of training.")

flags.DEFINE_integer("seed", 42, "random_seed")

def set_config_v2(enable_xla=False):
  """Config eager context according to flag values using TF 2.0 API."""
  if enable_xla:
    tf.config.optimizer.set_jit(True)
    # Disable PinToHostOptimizer in grappler when enabling XLA because it
    # causes OOM and performance regression.
    tf.config.optimizer.set_experimental_options(
        {'pin_to_host_optimization': False}
    )


def get_model(albert_config, max_seq_length, num_labels, init_checkpoint, learning_rate,
                     num_train_steps, num_warmup_steps):
    """Returns keras fuctional model"""
    float_type = tf.float32
    hidden_dropout_prob = FLAGS.classifier_dropout # as per original code relased
    input_word_ids = tf.keras.layers.Input(
        shape=(max_seq_length,), dtype=tf.int32, name='input_word_ids')
    input_mask = tf.keras.layers.Input(
        shape=(max_seq_length,), dtype=tf.int32, name='input_mask')
    input_type_ids = tf.keras.layers.Input(
        shape=(max_seq_length,), dtype=tf.int32, name='input_type_ids')
    
    albert_layer = AlbertModel(config=albert_config, float_type=float_type)

    pooled_output, _ = albert_layer(input_word_ids, input_mask, input_type_ids)

    albert_model = tf.keras.Model(inputs=[input_word_ids,input_mask,input_type_ids],
                                  outputs=[pooled_output])

    albert_model.load_weights(init_checkpoint)
                                                
    initializer = tf.keras.initializers.TruncatedNormal(stddev=albert_config.initializer_range)

    output = tf.keras.layers.Dropout(rate=hidden_dropout_prob)(pooled_output)
    output = tf.keras.layers.Dense(
        num_labels,
        activation="softmax",
        kernel_initializer=initializer,
        name='output',
        dtype=float_type)(
            output)
    model = tf.keras.Model(
        inputs={
            'input_word_ids': input_word_ids,
            'input_mask': input_mask,
            'input_type_ids': input_type_ids
        },
        outputs=output)
    
    learning_rate_fn = tf.keras.optimizers.schedules.PolynomialDecay(initial_learning_rate=learning_rate,
                                                decay_steps=num_train_steps,end_learning_rate=0.0)
    if num_warmup_steps:
        learning_rate_fn = WarmUp(initial_learning_rate=learning_rate,
                                decay_schedule_fn=learning_rate_fn,
                                warmup_steps=num_warmup_steps)
    lamp_optimizer = LAMB(
        learning_rate=learning_rate_fn,
        weight_decay_rate=FLAGS.weight_decay,
        beta_1=0.9,
        beta_2=0.999,
        epsilon=FLAGS.adam_epsilon,
        exclude_from_weight_decay=['layer_norm', 'bias'])
    
    loss_fct = tf.keras.losses.SparseCategoricalCrossentropy()

    model.compile(optimizer=lamp_optimizer,loss=loss_fct,metrics=['accuracy'])
    
    return model



def main(_):
  logging.set_verbosity(logging.INFO)

  if FLAGS.enable_xla:
	  set_config_v2(FLAGS.enable_xla)

  strategy = None
  if FLAGS.strategy_type == "one":
	  strategy = tf.distribute.OneDeviceStrategy()
  if FLAGS.strategy_type == "mirror":
	  strategy = tf.distribute.MirroredStrategy()
  else:
	  raise ValueError('The distribution strategy type is not supported: %s' %
                     FLAGS.strategy_type)

  with tf.io.gfile.GFile(FLAGS.input_meta_data_path, 'rb') as reader:
    input_meta_data = json.loads(reader.read().decode('utf-8'))
  
  num_labels = input_meta_data["num_labels"]
  FLAGS.max_seq_length = input_meta_data["max_seq_length"]
  processor_type = input_meta_data['processor_type']
  
  if not FLAGS.do_train and not FLAGS.do_eval and not FLAGS.do_predict:
    raise ValueError(
        "At least one of `do_train`, `do_eval` or `do_predict' must be True.")

  albert_config = AlbertConfig.from_json_file(FLAGS.albert_config_file)

  if FLAGS.max_seq_length > albert_config.max_position_embeddings:
    raise ValueError(
        "Cannot use sequence length %d because the ALBERT model "
        "was only trained up to sequence length %d" %
        (FLAGS.max_seq_length, albert_config.max_position_embeddings))

  tf.io.gfile.makedirs(FLAGS.output_dir)

  num_train_steps = None
  num_warmup_steps = None
  if FLAGS.do_train:
    len_train_examples = input_meta_data['train_data_size']
    num_train_steps = int(
        len_train_examples / FLAGS.train_batch_size * FLAGS.num_train_epochs)
    num_warmup_steps = int(num_train_steps * FLAGS.warmup_proportion)

  with strategy.scope():
	  model = get_model(
		  albert_config=albert_config,
		  max_seq_length=FLAGS.max_seq_length,
		  num_labels=num_labels,
		  init_checkpoint=FLAGS.init_checkpoint,
		  learning_rate=FLAGS.learning_rate,
		  num_train_steps=num_train_steps,
		  num_warmup_steps=num_warmup_steps)
  model.summary()

  if FLAGS.do_train:
    logging.info("***** Running training *****")
    logging.info("  Num examples = %d", len_train_examples)
    logging.info("  Batch size = %d", FLAGS.train_batch_size)
    logging.info("  Num steps = %d", num_train_steps)

    train_input_fn = functools.partial(
      create_classifier_dataset,
      FLAGS.train_data_path,
      seq_length=FLAGS.max_seq_length,
      batch_size=FLAGS.train_batch_size,
      drop_remainder=False)
    
    eval_input_fn = functools.partial(
      create_classifier_dataset,
      FLAGS.eval_data_path,
      seq_length=FLAGS.max_seq_length,
      batch_size=FLAGS.eval_batch_size,
      is_training=False,
      drop_remainder=False)

    with strategy.scope():
        training_dataset = train_input_fn()
        evaluation_dataset = eval_input_fn()

        summary_dir = os.path.join(FLAGS.output_dir, 'summaries')
        summary_callback = tf.keras.callbacks.TensorBoard(summary_dir)
        checkpoint_path = os.path.join(FLAGS.output_dir, 'checkpoint')
        checkpoint_callback = tf.keras.callbacks.ModelCheckpoint(checkpoint_path, save_weights_only=True)
        custom_callbacks = [summary_callback, checkpoint_callback]
        
        model.fit(x=training_dataset,validation_data=evaluation_dataset,epochs=FLAGS.num_train_epochs,callbacks=custom_callbacks)
    

  if FLAGS.do_eval:
    len_eval_examples = input_meta_data['eval_data_size']

    logging.info("***** Running evaluation *****")
    logging.info("  Num examples = %d", len_eval_examples)
    logging.info("  Batch size = %d", FLAGS.eval_batch_size)
    
    with strategy.scope():
        loss,accuracy = model.evaluate(evaluation_dataset)

    print(f"loss : {loss} , Accuracy : {accuracy}")


if __name__ == "__main__":
  flags.mark_flag_as_required("train_data_path")
  flags.mark_flag_as_required("eval_data_path")
  flags.mark_flag_as_required("input_meta_data_path")
  flags.mark_flag_as_required("init_checkpoint")
  flags.mark_flag_as_required("spm_model_file")
  flags.mark_flag_as_required("albert_config_file")
  flags.mark_flag_as_required("output_dir")
  app.run(main)

# -*- coding:utf-8 -*-
# Author: hankcs
# Date: 2019-12-29 13:55
import logging
import math
import os

import tensorflow as tf

from hanlp.components.taggers.tagger import TaggerComponent
from hanlp.components.taggers.transformers.metrics import MaskedSparseCategoricalAccuracy
from hanlp.components.taggers.transformers.transformer_transform import TransformerTransform
from hanlp.layers.transformers import AutoTokenizer, TFAutoModel, TFPreTrainedModel, PreTrainedTokenizer, TFAlbertModel, \
    BertTokenizer
from hanlp.losses.sparse_categorical_crossentropy import MaskedSparseCategoricalCrossentropyOverBatchFirstDim
from hanlp.optimizers.adamw import create_optimizer
from hanlp.utils.util import merge_locals_kwargs


class TransformerTaggingModel(tf.keras.Model):
    def __init__(self, transformer: tf.keras.Model, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.transformer = transformer

    def call(self, inputs, training=None, mask=None):
        return super().call(inputs, training, mask)


class TransformerTagger(TaggerComponent):
    def __init__(self, transform: TransformerTransform = None) -> None:
        if transform is None:
            transform = TransformerTransform()
        super().__init__(transform)
        self.transform: TransformerTransform = transform

    def build_model(self, transformer, max_seq_length, **kwargs) -> tf.keras.Model:
        tokenizer: PreTrainedTokenizer = AutoTokenizer.from_pretrained(transformer)
        self.transform.tokenizer = tokenizer
        transformer: TFPreTrainedModel = TFAutoModel.from_pretrained(transformer, name=os.path.basename(transformer))
        self.transform.transformer_config = transformer.config

        input_ids = tf.keras.layers.Input(shape=(max_seq_length,), dtype=tf.int32, name='input_ids')
        input_mask = tf.keras.layers.Input(shape=(max_seq_length,), dtype=tf.int32, name='input_mask')
        segment_ids = tf.keras.layers.Input(shape=(max_seq_length,), dtype=tf.int32, name='segment_ids')
        sequence_output = transformer([input_ids, input_mask, segment_ids])[0]
        sequence_output = tf.keras.layers.Dropout(transformer.config.to_dict().get('hidden_dropout_prob', 0.1),
                                                  name='hidden_dropout')(sequence_output)
        logits = tf.keras.layers.Dense(len(self.transform.tag_vocab),
                                       kernel_initializer=tf.keras.initializers.TruncatedNormal(
                                           transformer.config.to_dict().get('initializer_range', 0.02)))(
            sequence_output)
        return tf.keras.Model(inputs=[input_ids, input_mask, segment_ids], outputs=logits)

    def fit(self, trn_data, dev_data, save_dir,
            transformer,
            optimizer='adamw',
            learning_rate=5e-5,
            weight_decay_rate=0,
            epsilon=1e-8,
            clipnorm=1.0,
            warmup_steps_ratio=0,
            use_amp=False,
            max_seq_length=128,
            batch_size=32,
            epochs=3,
            metrics='accuracy',
            run_eagerly=False,
            logger=None,
            verbose=True,
            **kwargs):
        return super().fit(**merge_locals_kwargs(locals(), kwargs))

    # noinspection PyMethodOverriding
    def build_optimizer(self, optimizer, learning_rate, epsilon, weight_decay_rate, clipnorm, use_amp, train_steps,
                        warmup_steps, **kwargs):
        if optimizer == 'adamw':
            opt = create_optimizer(init_lr=learning_rate,
                                   epsilon=epsilon,
                                   weight_decay_rate=weight_decay_rate,
                                   clipnorm=clipnorm,
                                   num_train_steps=train_steps, num_warmup_steps=warmup_steps)
            # opt = tfa.optimizers.AdamW(learning_rate=3e-5, epsilon=1e-08, weight_decay=0.01)
            # opt = tf.keras.optimizers.Adam(learning_rate=3e-5, epsilon=1e-08)
            self.config.optimizer = tf.keras.utils.serialize_keras_object(opt)
            lr_config = self.config.optimizer['config']['learning_rate']['config']
            if 'decay_schedule_fn' in lr_config:
                lr_config['decay_schedule_fn'] = dict(
                    (k, v) for k, v in lr_config['decay_schedule_fn'].items() if not k.startswith('_'))
        else:
            opt = super().build_optimizer(optimizer)
        if use_amp:
            # loss scaling is currently required when using mixed precision
            opt = tf.keras.mixed_precision.experimental.LossScaleOptimizer(opt, 'dynamic')
        return opt

    def build_vocab(self, trn_data, logger):
        train_examples = super().build_vocab(trn_data, logger)
        warmup_steps_per_epoch = math.ceil(train_examples * self.config.warmup_steps_ratio / self.config.batch_size)
        self.config.warmup_steps = warmup_steps_per_epoch * self.config.epochs
        return train_examples

    def train_loop(self, trn_data, dev_data, epochs, num_examples, train_steps_per_epoch, dev_steps, model, optimizer,
                   loss, metrics, callbacks, logger, **kwargs):
        history = self.model.fit(trn_data, epochs=epochs, steps_per_epoch=train_steps_per_epoch,
                                 validation_data=dev_data,
                                 callbacks=callbacks,
                                 validation_steps=dev_steps,
                                 # mask out padding labels
                                 # class_weight=dict(
                                 #     (i, 0 if i == 0 else 1) for i in range(len(self.transform.tag_vocab)))
                                 )  # type:tf.keras.callbacks.History
        return history

    def build_loss(self, loss, **kwargs):
        return MaskedSparseCategoricalCrossentropyOverBatchFirstDim()

    def build_metrics(self, metrics, logger: logging.Logger, **kwargs):
        if metrics == 'accuracy':
            return MaskedSparseCategoricalAccuracy('accuracy')
        return super().build_metrics(metrics, logger, **kwargs)

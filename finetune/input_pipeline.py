import itertools
import logging
import sys
import math
import os
from collections.abc import Iterable
from collections import Counter

from abc import ABCMeta, abstractmethod

import tqdm
import numpy as np
import pandas as pd
import tensorflow as tf
from tensorflow.python.data import Dataset
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelBinarizer
from sklearn.utils import shuffle as dataset_shuffle
import finetune
from finetune.errors import FinetuneError
from finetune.encoding.input_encoder import EncodedOutput, tokenize_context
from finetune.util.imbalance import compute_class_weights
from finetune.util.timing import ProgressBar

LOGGER = logging.getLogger("finetune")

class Chunker:
    def __init__(self, max_length, total_context_width, justify="c"):
        if total_context_width is None:
            total_context_width = 2 * max_length // 3
        assert total_context_width < max_length
        assert justify.lower() in {"center", "left", "right"}
        
        self.max_length = max_length
        self.total_context_width = total_context_width
        self.chunk_size = self.max_length - 2
        self.useful_chunk_width = self.chunk_size - total_context_width 
        self.justify = justify.lower()
        
        if self.justify == "left":
            self.normal_start = 0
        elif self.justify == "right":
            self.normal_start = total_context_width
        elif self.justify == "center":
            self.normal_start = total_context_width // 2

        self.normal_end = self.normal_start + self.useful_chunk_width

    def generate_chunks(self, length):
        for start in range(0, length, self.useful_chunk_width):
            end = start + self.chunk_size
            yield start, end
            if end >= length:
                break

    def useful_chunk_section(self, start_of_doc, end_of_doc):
        start = self.normal_start
        end = self.normal_end
        if start_of_doc:
            start = 0    
        if end_of_doc:
            end = self.max_length
        return start, end


class BasePipeline(metaclass=ABCMeta):
    def __init__(self, config):
        self.config = config
        self.text_encoder = self.config.base_model.get_encoder(self.config)
        self.label_encoder = None
        self.target_dim = None
        self.pad_idx_ = None
        self.rebuild = False
        self.epoch = 0
        self._chunker = None

    @property
    def dataset_size(self):
        return self.config.dataset_size

    @abstractmethod
    def _target_encoder(self):
        # Overridden by subclass to produce the right target encoding for a given target model.
        raise NotImplementedError
    
    @property
    def chunker(self):
        if getattr(self, "_chunker", None) is None:
            self._chunker = Chunker(
                max_length=self.config.max_length,
                total_context_width=self.config.chunk_context,
                justify=self.config.chunk_alignment
            )
        return self._chunker


    def _add_context_info_if_present(self, types, shapes):
        if self.config.use_auxiliary_info:
            TS = tf.TensorShape
            types["context"] = tf.float32
            shapes["context"] = TS([None, self.config.context_dim])
        return types, shapes

    def feed_shape_type_def(self):
        TS = tf.TensorShape
        types = {
            "tokens": tf.int32
        }
        shapes = {
            "tokens": TS([None]),
        }
        types, shapes = self._add_context_info_if_present(types, shapes)
        return (
            (types, tf.float32,),
            (shapes, TS([self.target_dim]),),
        )

    def text_to_tokens_mask(self, X, Y=None, context=None):
        out_gen = self._text_to_ids(X, pad_token=self.config.pad_token)
        for i, out in enumerate(out_gen):
            if context is None:
                feats = {"tokens": out.token_ids}
            else:
                tokenized_context = tokenize_context(context, out, self.config)
                feats = {"tokens": out.token_ids, "context": tokenized_context}
            if Y is None:
                yield feats
            else:
                yield feats, self.label_encoder.transform([Y])[0]

    def _post_data_initialization(self, Y=None):
        if Y is not None:
            if self.label_encoder is None:
                self.label_encoder = self._target_encoder()
                if not callable(Y):
                    self.label_encoder.fit(Y)
                else:
                    Y_fit = list(itertools.islice(Y(), 10000))
                    self.label_encoder.fit(Y_fit)

            self.config.pad_idx = self.pad_idx

            target_dim = self.label_encoder.target_dim
            self.lm_loss_coef = (
                self.config.lm_loss_coef if target_dim is not None else 1.0
            )
            self.target_dim = target_dim

    def _compute_class_counts(self, encoded_dataset):
        target_arrs = np.asarray([target_arr for doc, target_arr in encoded_dataset])
        targets = []
        for target in self.label_encoder.inverse_transform(target_arrs):
            if isinstance(target, Iterable):
                # Iterable
                targets.extend(target)
            else:
                targets.append(target)

        return Counter(targets)

    def _compute_class_weights(self, class_weights, class_counts):
        return compute_class_weights(class_weights=class_weights, class_counts=class_counts)

    def _dataset_with_targets(self, Xs, Y, train, context=None, update_hook=None):
        if context is not None:
            if not callable(Xs) and not callable(Y) and not callable(context):
                dataset = lambda: zip(Xs, Y, context)
            elif callable(Xs) and callable(Y) and callable(context):
                dataset = lambda: zip(Xs(), Y(), context)
            else:
                raise ValueError( "Either none or all of Xs and Y and context should be callable, not a mixture")

            dataset_encoded = lambda: itertools.chain.from_iterable(
                map(lambda xyc: self.text_to_tokens_mask(*xyc), dataset())
            )
        else:
            if not callable(Xs) and not callable(Y):
                dataset = lambda: zip(Xs, Y)
            elif callable(Xs) and callable(Y):
                dataset = lambda: zip(Xs(), Y())
            else:
                raise ValueError( "Either neither or both of Xs and Y should be callable, not a mixture")
            dataset_encoded = lambda: itertools.chain.from_iterable(
                map(lambda xy: self.text_to_tokens_mask(*xy), dataset())
            )

        if not callable(Y) and train:
            dataset_encoded_list = list(dataset_encoded())
            self.config.dataset_size = len(dataset_encoded_list)
            if self.config.class_weights is not None:
                class_counts = self._compute_class_counts(dataset_encoded_list)
                self.config.class_weights = self._compute_class_weights(
                    class_weights=self.config.class_weights,
                    class_counts=class_counts
                )
        shape_def = self.feed_shape_type_def()
        return Dataset.from_generator(
            lambda: self.wrap_tqdm(dataset_encoded(), train, update_hook=update_hook), *shape_def
        )

    def _dataset_without_targets(self, Xs, train, context=None, update_hook=None):
        if context is not None:
            # we assume that X must have known length if we also provide context so this is safe
            if callable(Xs):
                Xs_ = Xs()
            else:
                Xs_ = Xs
            Xs_gen = lambda: zip(Xs_, [None] * len(Xs_), context)
            Xs_fn = lambda: self.wrap_tqdm(Xs_gen(), train, update_hook=update_hook)
            dataset_encoded = lambda: itertools.chain.from_iterable(
                map(lambda xyc: self.text_to_tokens_mask(*xyc), Xs_fn())
            )
        else:
            if not callable(Xs):
                Xs_fn = lambda: self.wrap_tqdm(Xs, train, update_hook=update_hook)
            else:
                Xs_fn = lambda: self.wrap_tqdm(Xs(), train, update_hook=update_hook)
            dataset_encoded = lambda: itertools.chain.from_iterable(
                map(self.text_to_tokens_mask, Xs_fn())
            )

        if not callable(Xs) and self.config.chunk_long_sequences:
            # Adjust dataset size to account for long documents being chunked
            dataset_encoded_list = list(dataset_encoded())
            self.config.dataset_size = len(dataset_encoded_list)
        types, shapes = self.feed_shape_type_def()
        return Dataset.from_generator(
            dataset_encoded, types[0], shapes[0]
        )  # 0s cut out the targets

    def _integer_val_size(self, val_size, dataset_size):
        if isinstance(val_size, float):
            return int(val_size * dataset_size)
        return val_size

    def validation_settings(self, n_examples, batch_size):
        """
        Auto-select reasonable validation settings
        """
        if self.config.val_size is not None and self.config.val_interval is not None:
            return (
                self._integer_val_size(self.config.val_size, n_examples),
                self.config.val_interval,
            )

        # Auto-select reasonable validation size
        if self.config.val_size == 'auto':
            if n_examples < 50 and not self.config.keep_best_model:
                val_size = 0
            else:
                val_size = max(5, int(0.05 * n_examples))
                val_size = min(100, val_size)
        else:
            val_size = self._integer_val_size(self.config.val_size, n_examples)

        # Auto-select reasonable validation interval
        if self.config.val_interval is None:
            # sys.maxsize corresponds to never running validation
            # and is used when val_size is set to 0
            val_interval = 4 * int(math.ceil(val_size / batch_size)) or None
        else:
            val_interval = int(self.config.val_interval)

        return int(val_size), val_interval

    def resampling(self, Xs, Y, context=None):
        return Xs, Y, context

    def _make_dataset(self, Xs, Y, train=False, context=None, update_hook=None):
        if Y is not None:
            dataset = lambda: self._dataset_with_targets(Xs, Y, train=train, context=context, update_hook=update_hook)
        else:
            dataset = lambda: self._dataset_without_targets(Xs, train=train, context=context, update_hook=update_hook)
        return dataset

    def wrap_tqdm(self, gen, train, update_hook=None):
        if train is None:
            return gen

        try:
            total = len(gen)
        except:
            if train:
                total = self.config.dataset_size
            else:
                total = self.config.val_size

        def internal_gen():
            current_epoch = (self.epoch - 1) % self.config.n_epochs + 1
            it = iter(gen)

            if train:
                desc = "Epoch {}/{}".format(current_epoch, self.config.n_epochs)
            else:
                desc = "Validation"
            for _, i in zip(range(self._skip_tqdm), it):
                yield i

            for i in ProgressBar(
                it,
                desc=desc,
                total=total,
                miniters=1,
                leave=current_epoch == self.config.n_epochs and train,
                update_hook=update_hook,
                silent=self.config.debugging_logs,
                current_epoch=current_epoch,
                total_epochs=self.config.n_epochs
            ):
                yield i

            if train:
                self.epoch += 1

        return internal_gen()

    def get_train_input_fns(self, Xs, Y=None, batch_size=None, val_size=None, context=None, update_hook=None):
        self.epoch = 1
        batch_size = batch_size or self.config.batch_size

        shuffle_buffer_size = self.config.shuffle_buffer_size
        val_size = val_size or 0
        prefetch_buffer = 2  # breaks the pipeline to allow concurrency

        if callable(Xs):
            try:
                self.config.dataset_size = len(Xs())
            except TypeError:
                if self.config.dataset_size is None:
                    raise FinetuneError(
                        "Generator input function does not have a length and no `config.dataset_size` is specified. "
                        "You must set `config.dataset_size` explicitly."
                    )
        else:
            self.config.dataset_size = len(Xs)

        self.config.val_size, self.config.val_interval = self.validation_settings(
            n_examples=len(Xs) if not callable(Xs) else self.config.dataset_size,
            batch_size=batch_size or self.config.batch_size,
        )
        self.config.dataset_size -= val_size

        if Y is not None:
            self._post_data_initialization(Y=Y)
        else:
            self._post_data_initialization(Y=None)

        if callable(Xs) or Y is None:
            self._skip_tqdm = val_size
            dataset = self._make_dataset(Xs, Y, train=True, context=context, update_hook=update_hook)
            val_dataset_unbatched = (
                lambda: dataset()
                .shuffle(
                    shuffle_buffer_size,
                    seed=self.config.seed,
                    reshuffle_each_iteration=False,
                )
                .take(self.config.val_size)
            )
            train_dataset_unbatched = (
                lambda: dataset()
                .shuffle(
                    shuffle_buffer_size,
                    seed=self.config.seed,
                    reshuffle_each_iteration=False,
                )
                .skip(self.config.val_size)
            )
        else:
            self._skip_tqdm = 0
            if context is not None:
                to_shuffle = (Xs, Y, context)

                if self.config.val_size > 0 and self.config.val_set is None:
                    Xs_tr, Xs_va, Y_tr, Y_va, c_tr, c_va = train_test_split(*to_shuffle, test_size=self.config.val_size, random_state=self.config.seed)
                else:
                    Xs_tr, Y_tr, c_tr = dataset_shuffle(*to_shuffle, random_state=self.config.seed)
                    Xs_va, Y_va, c_va = self.config.val_set or ([], [], [])

                Xs_tr, Y_tr, c_tr = self.resampling(Xs_tr, Y_tr, c_tr)
                self.config.dataset_size = len(Xs_tr)
                val_dataset_unbatched = self._make_dataset(Xs_va, Y_va, train=False, context=c_va)
                train_dataset_unbatched = self._make_dataset(Xs_tr, Y_tr, train=True, context=c_tr, update_hook=update_hook)
            else:
                to_shuffle = (Xs, Y)

                if self.config.val_size > 0 and self.config.val_set is None:
                    Xs_tr, Xs_va, Y_tr, Y_va = train_test_split(*to_shuffle, test_size=self.config.val_size, random_state=self.config.seed)
                else:
                    Xs_tr, Y_tr = dataset_shuffle(*to_shuffle, random_state=self.config.seed)
                    Xs_va, Y_va = self.config.val_set or ([], [])

                Xs_tr, Y_tr, _ = self.resampling(Xs_tr, Y_tr)
                self.config.dataset_size = len(Xs_tr)
                val_dataset_unbatched = self._make_dataset(Xs_va, Y_va, train=False)
                train_dataset_unbatched = self._make_dataset(Xs_tr, Y_tr, train=True, update_hook=update_hook)

        if self.config.chunk_long_sequences or self.config.class_weights:
            # Certain settings require that the entire dataset be encoded before compiling the graph
            with tf.Graph().as_default():
                train_dataset_unbatched()

        _, shapes = self.feed_shape_type_def()
        if Y is None:
            shapes = shapes[0]

        val_dataset = (
            lambda: val_dataset_unbatched()
            .padded_batch(batch_size, padded_shapes=shapes, drop_remainder=False)
            .cache()
            .prefetch(prefetch_buffer)
        )
        train_dataset = (
            lambda: train_dataset_unbatched()
            .padded_batch(batch_size, padded_shapes=shapes, drop_remainder=False)
            .repeat(self.config.n_epochs)
            .prefetch(prefetch_buffer)
        )

        return (
            val_dataset,
            train_dataset,
            self.config.val_size,
            self.config.val_interval,
        )

    def get_predict_input_fn(self, Xs, batch_size=None, context=None):
        batch_size = batch_size or self.config.predict_batch_size
        _, shapes = self.feed_shape_type_def()
        tf_dataset = lambda: self._dataset_without_targets(Xs, train=None, context=context).padded_batch(batch_size, padded_shapes=shapes[0], drop_remainder=False)
        return tf_dataset

    @property
    def pad_idx(self):
        if self.pad_idx_ is None:
            if hasattr(self.label_encoder, "classes_"):
                classes = list(self.label_encoder.classes_)
                if self.config.pad_token in classes:
                    self.pad_idx_ = classes.index(self.config.pad_token)
                else:
                    self.pad_idx_ = None
        return self.pad_idx_

    def _format_for_encoding(self, X):
        """
        Most subclasses take in inputs as:
            List (batch) of list (docs)

        Encode_multi_input expect the following format:
            List (batch) of list (docs) of list (subseqs) of text

        This method is responsible for standardizing inputs to the above format
        """
        return [X]

    def _format_for_inference(self, X):
        return list(X)

    def _text_to_ids(self, Xs, pad_token=None):
        Xs = self._format_for_encoding(Xs)
        if self.config.chunk_long_sequences and len(Xs) == 1:
            # can only chunk single sequence inputs
            encoded = self.text_encoder.encode_multi_input(
                Xs,
                max_length=sys.maxsize,
            )
            length = len(encoded.token_ids)
            field_starts_and_ends = dict()
            for field in EncodedOutput._fields:
                field_value = getattr(encoded, field)
                if field_value is not None:
                    field_starts_and_ends[field] = (field_value[0], field_value[-1])
            for start, end in self.chunker.generate_chunks(length):
                d = dict()
                for field in EncodedOutput._fields:
                    field_value = getattr(encoded, field)
                    if field_value is not None:
                        fv = field_value[start:end]
                        if self.config.add_eos_bos_to_chunk:
                            start_token, end_token = field_starts_and_ends[field]
                            if fv[0] != start_token:
                                fv = np.concatenate(([start_token], fv))
                            if fv[-1] != end_token:
                                fv = np.concatenate((fv, [end_token]))
                        d[field] = fv
                yield EncodedOutput(**d)
        else:
            encoder_out = self.text_encoder.encode_multi_input(
                Xs,
                max_length=self.config.max_length,
            )

            d = dict()
            for field in EncodedOutput._fields:
                field_value = getattr(encoder_out, field)
                if field_value is not None:
                    d[field] = field_value

            yield EncodedOutput(**d)

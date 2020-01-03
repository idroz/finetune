import math

import tensorflow as tf	
from finetune.util.shapes import shape_list
from finetune.util.positional_embeddings import add_timing_signal_from_position
from finetune.base_models.gpt.featurizer import conv1d


def embed_context(context, featurizer_state, config, train):
    with tf.variable_scope("context_embedding"):
        context_dim = shape_list(context)[-1]
        context_weight = tf.get_variable(
            name="ce",	
            shape=[context_dim, config.n_context_embed],
            initializer=tf.random_normal_initializer(stddev=config.context_embed_stddev),	
        )
        context_bias = tf.get_variable(
            name="ca",	
            shape=[config.n_context_embed],	
            initializer=tf.zeros_initializer(),	
        )
        c_embed = tf.add(tf.tensordot(context, context_weight, axes=[[-1], [0]]), context_bias)
    featurizer_state['context'] = c_embed
    return featurizer_state


def add_context_embed(featurizer_state):
    if "context" in featurizer_state:
        context_embed = featurizer_state["context"]

        shape = shape_list(context_embed)
        if len(shape) == 4:
            # comparison / multiple choice 
            flat_embed = tf.reshape(
                context_embed, 
                [shape[0] * shape[1], shape[2], shape[3]],
            )
        else:
            flat_embed = context_embed

        seq_mask = tf.sequence_mask(featurizer_state['lengths'])
        for key in ['features', 'explain_out']:
            if key in featurizer_state:
                float_mask = tf.cast(seq_mask, tf.float32)
                binary_mask = tf.constant(1.) - float_mask
                flat_embed = flat_embed * tf.expand_dims(binary_mask, -1)
                sum_context = tf.reduce_sum(flat_embed, 1)
                mean_context = sum_context / tf.reduce_sum(float_mask)

                if len(shape) == 4:
                    mean_context = tf.reshape(
                        mean_context, 
                        [shape[0], shape[1], shape[3]]
                    )
    
                featurizer_state[key] = tf.concat(
                    (featurizer_state[key], mean_context), -1
                )

        featurizer_state['sequence_features'] = tf.concat(
            (featurizer_state['sequence_features'], context_embed), -1
        )


def embed_position(context, featurizer_state, config, train):
    with tf.variable_scope("context_embedding"):
        context_dim = shape_list(context)[-1]
        batch, seq, _ = shape_list(featurizer_state['sequence_features'])
        context_channels = config.n_context_embed_per_channel * context_dim
        x = tf.zeros(shape=(batch, seq, context_channels))
        pos_embed = add_timing_signal_from_position(
            x,
            context,
            timescales = [
                [
                    (math.pi / 2) * (1/2500),
                    (25 * math.pi) * (1/2500)
                ]
            ] * context_dim
        ) / (float(context_channels) / config.context_embed_scale)
        featurizer_state['context'] = pos_embed
    return featurizer_state


def add_context_embed(featurizer_state):
    if "context" in featurizer_state:
        context_embed = featurizer_state["context"]

        shape = shape_list(context_embed)
        if len(shape) == 4:
            # comparison / multiple choice 
            flat_embed = tf.reshape(
                context_embed, 
                [shape[0] * shape[1], shape[2], shape[3]],
            )
        else:
            flat_embed = context_embed

        seq_mask = tf.sequence_mask(featurizer_state['lengths'])
        for key in ['features', 'explain_out']:
            if key in featurizer_state:
                float_mask = tf.cast(seq_mask, tf.float32)
                binary_mask = tf.constant(1.) - float_mask
                flat_embed = flat_embed * tf.expand_dims(binary_mask, -1)
                sum_context = tf.reduce_mean(flat_embed, 1)
                mean_context = sum_context / tf.reduce_mean(float_mask)

                if len(shape) == 4:
                    mean_context = tf.reshape(
                        mean_context, 
                        [shape[0], shape[1], shape[3]]
                    )
    
                featurizer_state[key] = tf.concat(
                    (featurizer_state[key], mean_context), -1
                )

        featurizer_state['sequence_features'] = tf.concat(
            (featurizer_state['sequence_features'], context_embed), -1
        )



import tensorflow as tf


def get_default_hparms():
    return tf.contrib.training.HParams(
        # TRAINING SETTINGS
        BATCH_SIZE=4,
        VISIBLE_GPUS=None,
        N_EPOCHS=3,
        SEED=42,

        # MODEL DEFINITION + INITIALIZATION
        WEIGHT_STDDEV=0.02,
        MAX_LENGTH=512,
        N_HEADS=12,
        N_LAYER=12,
        ACT_FN="gelu",
        N_EMBED=768,

        # REGULARIZATION
        EMBED_P_DROP=0.1,
        ATTN_P_DROP=0.1,
        RESID_P_DROP=0.1,
        CLF_P_DROP=0.1,
        L2_REG=0.01,
        VECTOR_L2=True,

        # LOSS + OPTIMIZATION
        B1=0.9,
        B2=0.999,
        EPSILON=1e-8,
        LR_SCHEDULE='warmup_linear',
        LR=6.25e-5,
        LR_WARMUP=0.002,
        MAX_GRAD_NORM=1,
        LM_LOSS_COEF=0.5,
        ROLLING_AVG_DECAY=0.99,

        # Logging
        SUMMARIZE_GRADS=False
    )


def cpu_hparams():
    hparam = get_default_hparms()
    hparam.VISIBLE_GPUS = []
    return hparam

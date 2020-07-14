import functools
import tensorflow as tf
from tensorflow_addons.text.crf import crf_log_likelihood

from finetune.base_models.gpt.featurizer import attn, dropout, norm
from finetune.util.shapes import shape_list, merge_leading_dims
from finetune.optimizers.recompute_grads import recompute_grad
from finetune.errors import FinetuneError
from finetune.nn.activations import act_fns
from finetune.nn.nn_utils import norm, build_ema_getter, tsa_filter
from finetune.nn.crf import sequence_decode, k_best_sequence_decode
from finetune.optimizers.learning_rate_schedules import warmup_constant


def perceptron(x, ny, config, w_init=None, b_init=None):
    """
    A very standard linear Perceptron model.
    :param x: Input tensor.
    :param ny: Number of outputs.
    :param config: A config object.
    :param w_init: Weight initializer.
    :param b_init: Bias initializer.
    :return: The output of the perceptron model.
    """
    w_init = w_init or tf.compat.v1.random_normal_initializer(stddev=config.weight_stddev)
    b_init = b_init or tf.compat.v1.constant_initializer(0)

    with tf.compat.v1.variable_scope('perceptron'):
        nx = config.n_embed
        w = tf.compat.v1.get_variable("w", [nx, ny], initializer=w_init)
        b = tf.compat.v1.get_variable("b", [ny], initializer=b_init)
        return tf.matmul(x, w) + b


def masked_language_model(*, X, mlm_weights, mlm_ids, mlm_positions, embed_weights, hidden, config, reuse=None, train=False):
    
    with tf.compat.v1.variable_scope('model/masked-language-model'):
        batch, seq, feats = shape_list(hidden)
        flat_offsets = tf.reshape(
            tf.range(0, batch, dtype=tf.int32) * seq, [-1, 1]
        )

        not_padding = tf.reshape(mlm_weights, [-1]) > 1e-9
        flat_positions = tf.boolean_mask(tensor=tf.reshape(mlm_positions + flat_offsets, [-1]), mask=not_padding) # take off the padding entirely
        gathered_hidden = tf.gather(tf.reshape(hidden, [batch * seq, feats]), flat_positions)
        mlm_ids = tf.boolean_mask(tensor=tf.reshape(mlm_ids, [-1]), mask=not_padding)
        
        final_proj_w = tf.compat.v1.get_variable(
            'dense/kernel',
            [config.n_embed, config.n_embed],
            initializer=tf.compat.v1.random_normal_initializer(stddev=config.weight_stddev)
        )
        final_proj_b = tf.compat.v1.get_variable(
            'dense/bias',
            [config.n_embed],
            initializer=tf.compat.v1.zeros_initializer
        )
        final_proj = act_fns[config.act_fn](
            tf.matmul(gathered_hidden, final_proj_w, transpose_b=True) + final_proj_b
        )

        normed_proj = norm(final_proj, 'LayerNorm')
        n_vocab = shape_list(embed_weights)[0]
        output_bias = tf.compat.v1.get_variable(
            "output_bias",
            shape=[n_vocab],
            initializer=tf.compat.v1.zeros_initializer()
        )
        
        logits = tf.matmul(normed_proj, embed_weights, transpose_b=True)
        logits = tf.nn.bias_add(logits, output_bias)
        
        mlm_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(            
            logits=logits,
            labels=mlm_ids,
        ) # No weights needed as there is no padding.

        logits = tf.scatter_nd(
            indices=flat_positions,
            updates=logits,
            shape=[batch * seq, n_vocab]
        )
                
        return {
            "logits": logits,
            "losses": mlm_loss,
        }


def language_model(*, X, sequence_lengths, embed_weights, hidden, config, reuse=None, train=False):
    """
    A language model output and loss for the language modelling objective described in the original finetune paper.
    This language model uses weights that are tied to the input embedding.
    :param X: The raw token ids fed to the featurizer.
    :param M: A loss mask, with 1's where losses should be counted and 0's elsewhere.
    :param embed_weights: The word embedding matrix, normally the one returned by the featurizer.
    :param hidden: Output of the featurizer.
    :param config: A config object.
    :param reuse: A Flag passed through to the tf.variable_scope context manager.
    :return: A dict containing:
        logits: The un-normalised log-probabilities over each word in the vocabulary.
        loss: The masked language modelling loss.

    """
    X = merge_leading_dims(X, 2)
    M = tf.sequence_mask(sequence_lengths, dtype=tf.float32)
    hidden = merge_leading_dims(hidden, 3)

    batch, seq = shape_list(X)
    vocab_size, hidden_dim = shape_list(embed_weights)

    with tf.compat.v1.variable_scope('model/language-model', reuse=reuse):
        # language model ignores last hidden state because we don't have a target
        lm_h = tf.reshape(hidden, [-1, config.n_embed])  # [batch, seq_len, embed] --> [batch * seq_len, embed]
        lm_logits = tf.matmul(lm_h, embed_weights, transpose_b=True)  # tied weights
        lm_logits = tf.cast(lm_logits, tf.float32)
        hidden_shape = tf.shape(input=hidden)
        logits = tf.reshape(lm_logits, shape=tf.concat([hidden_shape[:-1], [vocab_size]], axis=0))
        lm_logits_offset = tf.reshape(logits[:, :-1], [-1, vocab_size])
        
        lm_losses = tf.compat.v1.losses.sparse_softmax_cross_entropy(
            logits=lm_logits_offset,
            labels=tf.reshape(X[:, 1:], [-1]),
            weights=tf.reshape(M[:, 1:], [-1])
        )

        perplexity = tf.reduce_sum(input_tensor=tf.exp(lm_losses) * M[:, 1:], axis=1) / tf.reduce_sum(input_tensor=M[:, 1:], axis=1)

        return {
            "logits": logits,
            "losses": lm_losses,
            "perplexity": perplexity,
        }


def _apply_class_weight(losses, targets, class_weights=None):
    if class_weights is not None:
        # loss multiplier applied based on true class
        weights = (
            tf.reduce_sum(input_tensor=class_weights * tf.cast(targets, dtype=tf.float32), axis=1)
        )
        weights *= tf.cast(tf.reduce_prod(input_tensor=tf.shape(input=weights)), dtype=tf.float32) / tf.reduce_sum(
            input_tensor=weights
        )
        losses *= tf.expand_dims(weights, 1)
    return losses


def _apply_multilabel_class_weight(losses, targets, class_weights=None):
    if class_weights is not None:
        # loss multiplier applied based on true class
        weights = (
            # contribution of positive class
            class_weights * tf.cast(targets, dtype=tf.float32) + 
            # contribution of negative class
            tf.ones_like(class_weights) * (1 - tf.cast(targets, dtype=tf.float32))
        )
        weights *= tf.cast(tf.reduce_prod(input_tensor=tf.shape(input=weights)), dtype=tf.float32) / tf.reduce_sum(
            input_tensor=weights
        )
        losses *= weights
    return losses


def classifier(hidden, targets, n_targets, config, train=False, reuse=None, **kwargs):
    """
    A simple linear classifier.

    :param hidden: The output of the featurizer. [batch_size, embed_dim]
    :param targets: One hot encoded target ids. [batch_size, n_classes]
    :param n_targets: A python int containing the number of classes that the model should be learning to predict over.
    :param dropout_placeholder:
    :param config: A config object, containing all parameters for the featurizer.
    :param train: If this flag is true, dropout and losses are added to the graph.
    :param reuse: Should reuse be set within this scope.
    :param kwargs: Spare arguments.
    :return: dict containing:
        logits: The unnormalised log probabilities of each class.
        losses: The loss for the classifier.
    """
    with tf.compat.v1.variable_scope("classifier", reuse=reuse):
        hidden = dropout(hidden, config.clf_p_drop, train)
        clf_logits = perceptron(hidden, n_targets, config)
        if targets is None:
            clf_losses = None
        else:
            clf_losses = tf.nn.softmax_cross_entropy_with_logits(
                logits=clf_logits, labels=tf.stop_gradient(targets)
            )

            clf_losses = _apply_class_weight(
                clf_losses, targets, kwargs.get("class_weights")
            )

        return {"logits": clf_logits, "losses": clf_losses}


def multi_choice_question(
    hidden, 
    targets, 
    n_targets, 
    config, 
    train=False, 
    reuse=None, 
    **kwargs
):
    with tf.compat.v1.variable_scope("model", reuse=reuse):
        if targets is not None:
            targets = tf.cast(targets, tf.int32)
        hidden = dropout(hidden, config.clf_p_drop, train)
        hidden = tf.unstack(hidden, num=n_targets, axis=1)
        hidden = tf.concat(hidden, axis=0)

        clf_out = perceptron(hidden, 1, config)
        clf_out = tf.split(clf_out, n_targets, axis=0)
        clf_out = tf.concat(clf_out, 1)

        if targets is None:
            clf_losses = None
        else:
            clf_losses = tf.nn.sparse_softmax_cross_entropy_with_logits(
                logits=clf_out, labels=tf.stop_gradient(targets)
            )

            clf_losses = _apply_class_weight(
                clf_losses, targets, kwargs.get("class_weights")
            )

        return {"logits": clf_out, "losses": clf_losses}


def multi_classifier(
    hidden, targets, n_targets, config, train=False, reuse=None, **kwargs
):
    """
    A simple linear classifier.

    :param hidden: The output of the featurizer. [batch_size, embed_dim]
    :param targets: The placeholder representing the sparse targets [batch_size, n_targets]
    :param n_targets: A python int containing the number of classes that the model should be learning to predict over.
    :param config: A config object, containing all parameters for the featurizer.
    :param train: If this flag is true, dropout and losses are added to the graph.
    :param reuse: Should reuse be set within this scope.
    :param kwargs: Spare arguments.
    :return: dict containing:
        logits: The unnormalised log probabilities of each class.
        losses: The loss for the classifier.
    """
    with tf.compat.v1.variable_scope("model", reuse=reuse):
        hidden = dropout(hidden, config.clf_p_drop, train)
        clf_logits = perceptron(hidden, n_targets, config)
        if targets is None:
            clf_losses = None
        else:
            clf_losses = tf.nn.sigmoid_cross_entropy_with_logits(
                logits=clf_logits, labels=tf.stop_gradient(targets)
            )
            clf_losses = _apply_multilabel_class_weight(
                clf_losses, targets, kwargs.get("class_weights")
            )
        return {"logits": clf_logits, "losses": clf_losses}


def regressor(hidden, targets, n_targets, config, train=False, reuse=None, **kwargs):
    """
    A simple linear regressor.

    :param hidden: The output of the featurizer. [batch_size, embed_dim]
    :param targets: The placeholder representing the regression targets. [batch_size]
    :param n_targets: A python int containing the number of outputs that the model should be learning to predict over.
    :param dropout_placeholder:
    :param config: A config object, containing all parameters for the featurizer.
    :param train: If this flag is true, dropout and losses are added to the graph.
    :param reuse: Should reuse be set within this scope.
    :param kwargs: Spare arguments.
    :return: dict containing:
        logits: The regression outputs.
        losses: L2 Loss for the regression targets.
    """
    with tf.compat.v1.variable_scope("regressor", reuse=reuse):
        hidden = dropout(hidden, config.clf_p_drop, train)
        outputs = perceptron(hidden, n_targets, config)
        if targets is None:
            loss = None
        else:
            if config.regression_loss.upper() == "L2":
                loss = tf.nn.l2_loss(outputs - targets)
            elif config.regression_loss.upper() == "L1":
                loss = tf.abs(outputs - targets)
            else:
                raise FinetuneError(
                    "regression_loss needs to be either L1 or L2, instead it is {}".format(
                        config.regression_loss
                    )
                )
        return {"logits": outputs, "losses": loss}


def ordinal_regressor(
    hidden,
    targets,
    n_targets,
    config,
    shared_threshold_weights=True,
    train=False,
    reuse=None,
    **kwargs
):
    """
    Ordinal Regressor using all-threshold loss.

    :param hidden: The output of the featurizer. [batch_size, embed_dim]
    :param targets: The placeholder representing the regression targets (binary threshold values). [batch_size]
    :param n_targets: A python int containing the number of thresholds that the model should be learning to predict over.
    :param dropout_placeholder:
    :param config: A config object, containing all parameters for the featurizer.
    :param train: If this flag is true, dropout and losses are added to the graph.
    :param reuse: Should reuse be set within this scope.
    :param kwargs: Spare arguments.
    :return: dict containing:
        logits: The regression outputs.
        losses: All-threshold Loss for the regression targets.
    """
    with tf.compat.v1.variable_scope("ordinalregressor", reuse=reuse):
        hidden = dropout(hidden, config.clf_p_drop, train)
        if shared_threshold_weights:
            w_init = tf.compat.v1.random_normal_initializer(stddev=config.weight_stddev)
            b_init = tf.compat.v1.random_normal_initializer(0)
            nx = config.n_embed
            w = tf.compat.v1.get_variable("w", [nx, 1], initializer=w_init)
            b = tf.compat.v1.get_variable("b", [n_targets], initializer=b_init)
            logits = tf.matmul(hidden, w) + b
        else:
            logits = perceptron(hidden, n_targets, config)

        if targets is None:
            outputs = tf.sigmoid(logits)
            loss = None
        else:
            outputs = logits
            loss = tf.nn.sigmoid_cross_entropy_with_logits(
                logits=logits, labels=tf.stop_gradient(targets)
            )
        return {"logits": outputs, "losses": loss}


def class_reweighting(class_weights):
    @tf.custom_gradient
    def custom_grad(logits):
        def grad(g):
            new_g = g * class_weights
            ratio = tf.norm(tensor=g) / tf.norm(tensor=new_g)
            return new_g * ratio

        return tf.identity(logits), grad

    return custom_grad


def sequence_labeler(
    hidden,
    targets,
    n_targets,
    config,
    pad_id,
    multilabel=False,
    train=False,
    reuse=None,
    lengths=None,
    use_crf=True,
    **kwargs
):
    """
    An Attention based sequence labeler model.

    In the case of unidirectional base models such as GPT this model takes the output of the pre-trained model,
    applies an additional randomly initialised multihead attention block, with residuals on top.
    The extra attention is not future masked to allow the model to label sequences based on context in both directions.
    The representations fed into this model are necessarily future masked because a language modelling loss is the
    original objective of the featurizer.

    For bidirectional base models we apply the crf model directly to the output of the base model.

    :param hidden: The output of the featurizer. [batch_size, sequence_length, embed_dim]
    :param targets: The placeholder representing the sequence labeling targets. [batch_size, sequence_length]
    :param n_targets: A python int containing the number of classes that the model should be learning to predict over.
    :param config: A config object, containing all parameters for the featurizer.
    :param train: If this flag is true, dropout and losses are added to the graph.
    :param reuse: Should reuse be set within this scope.
    :param lengths: The number of non-padding tokens in the input.
    :param kwargs: Spare arguments.
    :return: dict containing:
        "logits": The un-normalised log probabilities of each class being in each location. For usable predictions,
            sampling from this distribution is not sufficient and a viterbi decoding method should be used.
        "losses": The negative log likelihood for the sequence targets.
        "predict_params": A dictionary of params to be fed to the viterbi decode function.
    """
    with tf.compat.v1.variable_scope("sequence-labeler", reuse=reuse):

        if targets is not None:
            targets = tf.cast(targets, dtype=tf.int32)

        nx = config.n_embed

        def seq_lab_internal(hidden):
            if config.base_model.is_bidirectional:
                n = hidden
            else:
                attn_fn = functools.partial(
                    attn,
                    scope="seq_label_attn",
                    n_state=nx,
                    n_head=config.seq_num_heads,
                    resid_pdrop=config.resid_p_drop,
                    attn_pdrop=config.attn_p_drop,
                    train=train,
                    scale=False,
                    mask=False,
                )
                n = norm(attn_fn(hidden) + hidden, "seq_label_residual")
            
            flat_logits = tf.compat.v1.layers.dense(n, n_targets)
            logits = tf.reshape(
                flat_logits, tf.concat([tf.shape(input=hidden)[:2], [n_targets]], 0)
            )
            return logits

        with tf.compat.v1.variable_scope("seq_lab_attn"):
            if config.low_memory_mode and train:
                seq_lab_internal = recompute_grad(
                    seq_lab_internal, use_entire_scope=True
                )
            logits = seq_lab_internal(hidden)
            logits = tf.cast(logits, tf.float32) # always run the crf in float32

        loss = 0.0

        default_lengths = tf.shape(input=hidden)[1] * tf.ones(
            tf.shape(input=hidden)[0], dtype=tf.int32
        )
        if lengths is None:
            lengths = default_lengths
            
        class_weights = kwargs.get("class_weights")
        
        with tf.device("CPU:0" if train else logits.device):
            if multilabel:
                transition_params = []
                logits_individual = tf.unstack(logits, n_targets, axis=-1)
                if targets is not None:
                    targets_individual = tf.unstack(targets, n_targets, axis=-1)
                logits = []
                for i in range(n_targets):
                    transition_params.append(
                        tf.cast(tf.compat.v1.get_variable("Transition_matrix_{}".format(i), shape=[2, 2]), tf.float32)
                    )
                    logits.append(
                        tf.stack(
                            (logits_individual[pad_id], logits_individual[i]), axis=-1
                        )
                    )
                    if targets is not None and i != pad_id:
                        if class_weights is not None:
                            is_pos_cls = tf.cast(targets_individual[i], dtype=tf.float32)
                            class_weight = tf.expand_dims(class_weights[i] * is_pos_cls + class_weights[pad_id] * (1.0 - is_pos_cls), -1)
                            logits_i = class_reweighting(class_weight)(logits[-1])
                        else:
                            logits_i = logits[i]
                        if use_crf:
                            loss -= crf_log_likelihood(
                                logits_i,
                                targets_individual[i],
                                lengths,
                                transition_params=transition_params[-1],
                            )[0]
                        else:
                            weights = tf.sequence_mask(
                                lengths, maxlen=tf.shape(input=targets_individual[i])[1], dtype=tf.float32
                            ) / tf.expand_dims(tf.cast(lengths, tf.float32), -1)
                            loss += tf.compat.v1.losses.sparse_softmax_cross_entropy(
                                targets_individual[i],
                                logits_i,
                                weights=weights
                            )
                logits = tf.stack(logits, axis=-1)
            else:
                if class_weights is not None and train:
                    class_weights = tf.reshape(class_weights, [1, 1, -1])
                    one_hot_class_weights = class_weights * tf.one_hot(targets, depth=n_targets)
                    per_token_weights = tf.reduce_sum(
                        input_tensor=one_hot_class_weights, axis=-1, keepdims=True
                    )
                    logits = class_reweighting(per_token_weights)(logits)
                                                                                                          
                transition_params = tf.cast(
                    tf.compat.v1.get_variable(
                        "Transition_matrix", shape=[n_targets, n_targets]
                    ),
                    tf.float32
                )
                if targets is not None:
                    if use_crf:
                        log_likelihood, _ = crf_log_likelihood(
                            logits, targets, lengths, transition_params=transition_params
                        )
                        loss = -log_likelihood
                    else:
                        weights = tf.sequence_mask(
                            lengths, maxlen=tf.shape(input=targets)[1], dtype=tf.float32
                        ) / tf.expand_dims(tf.cast(lengths, tf.float32), -1)
                        loss = tf.compat.v1.losses.sparse_softmax_cross_entropy(
                            targets,
                            logits,
                            weights=weights
                        )

        return {
            "logits": logits,
            "losses": loss,
            "predict_params": {"transition_matrix": transition_params, "sequence_length": lengths},
        }

def vat(
    hidden,
    targets,
    n_targets,
    config,
    pad_id,
    multilabel=False,
    train=False,
    reuse=None,
    lengths=None,
    use_crf=True,
    embedding_out=None,
    **kwargs
):
    """
    Virtual Adversarial Training SSL model.

    First, iteratively creates an adversarial perturbation vector. Starts with
    a random vector, then, given the divergence between the perturbed output
    and the normal output, calculates the gradient with respect to the vector.
    The gradient is used to update the vector, and the process is repeated K
    times. Using the adversarial vector, consistency loss is enforced between
    the preturbed output and the normal output.

    :param hidden: The output of the featurizer. [batch_size, sequence_length, embed_dim]
    :param targets: The placeholder representing the sequence labeling targets. [batch_size, sequence_length]
    :param n_targets: A python int containing the number of classes that the model should be learning to predict over.
    :param config: A config object, containing all parameters for the featurizer.
    :param train: If this flag is true, dropout and losses are added to the graph.
    :param reuse: Should reuse be set within this scope.
    :param lengths: The number of non-padding tokens in the input.
    :param embedding_out: Embedded input fed into the featurizer.
    :param kwargs: Spare arguments.
    :return: dict containing:
        "logits": The un-normalised log probabilities of each class being in each location. For usable predictions,
            sampling from this distribution is not sufficient and a viterbi decoding method should be used.
        "losses": The negative log likelihood for the sequence targets.
        "predict_params": A dictionary of params to be fed to the viterbi decode function.
    """
    with tf.compat.v1.variable_scope("vat-sequence-labeler", reuse=reuse):
        tf.random.set_seed(config.seed)
        if targets is not None:
            targets = tf.cast(targets, dtype=tf.int32)

        layer = tf.keras.layers.Dense(n_targets)
        logits = tf.cast(layer(hidden), tf.float32)

        loss = 0.0

        default_lengths = tf.shape(input=hidden)[1] * tf.ones(
            tf.shape(input=hidden)[0], dtype=tf.int32
        )
        if lengths is None:
            lengths = default_lengths
            
        if targets is not None:
            # Seperate labled and unlabeled data
            target_shape = tf.shape(targets)
            batch_size = tf.shape(logits)[0]
            all_logits = logits
            all_lengths = lengths
            logits = all_logits[:target_shape[0]]
            lengths = all_lengths[:target_shape[0]]

        class_weights = kwargs.get("class_weights")
        if class_weights is not None and train:
            class_weights = tf.reshape(class_weights, [1, 1, -1])
            one_hot_class_weights = class_weights * tf.one_hot(targets, depth=n_targets)
            per_token_weights = tf.reduce_sum(
                input_tensor=one_hot_class_weights, axis=-1, keepdims=True
            )
            logits = class_reweighting(per_token_weights)(logits)
                                                                                                  
        transition_params = tf.cast(
            tf.compat.v1.get_variable(
                "Transition_matrix", shape=[n_targets, n_targets]
            ),
            tf.float32
        )

        if targets is not None:
            # Perturbation -> logits functions
            def after_embeddings(perturbation):
                featurizer_fn = kwargs.get("featurizer_fn")
                scope = kwargs.get("scope")
                # Revert scope to reuse featurizer weights
                with tf.compat.v1.variable_scope(scope):
                    preturbed_embedding = perturbation + embedding_out
                    featurizer_state = featurizer_fn(preturbed_embedding,
                                                     reuse=True)
                hidden = featurizer_state["sequence_features"]
                logits = layer(hidden)
                return tf.cast(logits, tf.float32)
            def after_transformer(perturbation):
                logits = layer(hidden + perturbation)
                return tf.cast(logits, tf.float32)
            # Apply perturbation to embeddings or transformer features
            if config.vat_preturb_embed:
                out_fn = after_embeddings
                pert_shape = tf.shape(embedding_out)
            else:
                out_fn = after_transformer
                pert_shape = tf.shape(hidden)

            # Logits -> probability distribution functions
            top_k_targets = None
            if config.vat_top_k:
                # Get targets outside of functions because adversarial and normal
                # should calculate distributions using the same targets
                # Target has shape of (batch_size, top_k, sequence length)
                top_k_targets, _ = k_best_sequence_decode(all_logits,
                                                        transition_params,
                                                        config.vat_top_k)
            def crf_probs(logits):
                all_probs = []
                # Get probability of each best sequence
                for cur_k in range(config.vat_top_k):
                    k_seqs = top_k_targets[:, cur_k, :]
                    k_log_likelihood, _ = crf_log_likelihood(logits,
                                                             k_seqs,
                                                             all_lengths,
                                                             transition_params=transition_params)
                    k_probs = tf.exp(k_log_likelihood)
                    all_probs.append(k_probs)

                # Get probability of not best sequence
                _all_probs = tf.stack(all_probs, axis=1)
                leftover_probs = tf.ones((batch_size,)) - \
                    tf.reduce_sum(_all_probs, axis=1)
                all_probs.append(leftover_probs)
                all_probs = tf.stack(all_probs, axis=1)
                # Should now be shape of (batch_size, top_k + 1)
                return all_probs
            def softmax_probs(logits):
                return tf.nn.softmax(logits)
            # K-best viterbi trick or softmax over logits
            prob_fn = crf_probs if config.vat_top_k else softmax_probs

            adv_vector = tf.random.uniform(shape=pert_shape, dtype=tf.float32)
            adv_vector = config.vat_e * tf.nn.l2_normalize(adv_vector, axis=-1)
            kl_div = tf.losses.KLDivergence(reduction=tf.keras.losses.Reduction.NONE)
            mask = tf.sequence_mask(all_lengths,
                                    maxlen=tf.math.reduce_max(all_lengths),
                                    dtype=tf.float32)
            # Gradient should only propegate thorugh the adversarial prediction
            probs = tf.stop_gradient(prob_fn(all_logits))
            for _ in range(config.vat_k):
                adv_logits = out_fn(adv_vector)
                adv_probs = prob_fn(adv_logits)
                adv_loss = kl_div(probs, adv_probs)
                adv_loss = tf.reduce_mean(adv_loss)
                gradient = tf.gradients(adv_loss, adv_vector)
                adv_vector = config.vat_e * tf.nn.l2_normalize(gradient, axis=-1)
                adv_vector = tf.reshape(adv_vector, tf.shape(adv_vector)[1:])
                adv_vector = tf.stop_gradient(adv_vector)
            adv_logits = out_fn(adv_vector)
            adv_probs = prob_fn(adv_logits)
            adv_loss = kl_div(probs, adv_probs)
            adv_loss = tf.reduce_mean(adv_loss)

            # Get TSA threshhold and discard confident labeled examples
            if config.tsa_method:
                logits, targets, lengths = tsa_filter(config.tsa_method,
                                                      logits, targets, lengths,
                                                      use_crf,
                                                      transition_params,
                                                      kwargs.get("total_num_steps"))
            if use_crf:
                with tf.device("CPU:0" if train else device):
                    log_likelihood, _ = crf_log_likelihood(
                        logits, targets, lengths, transition_params=transition_params
                    )
                    loss = -log_likelihood
            else:
                weights = tf.sequence_mask(
                    lengths, maxlen=tf.shape(input=targets)[1], dtype=tf.float32
                ) / tf.expand_dims(tf.cast(lengths, tf.float32), -1)
                loss = tf.compat.v1.losses.sparse_softmax_cross_entropy(
                    targets,
                    logits,
                    weights=weights
                )

            if config.tsa_method:
                # Make loss 0 if there are no targets that are over the thresh
                loss = tf.cond(tf.equal(tf.shape(targets)[0], 0),
                               lambda: 0.0,
                               lambda: loss)

            loss = tf.reduce_mean(loss)
            loss += config.ssl_loss_coef * adv_loss

        return {
            "logits": logits,
            "losses": loss,
            "predict_params": {"transition_matrix": transition_params, "sequence_length": lengths},
        }

def pseudo_label(
    hidden,
    targets,
    n_targets,
    config,
    pad_id,
    multilabel=False,
    train=False,
    reuse=None,
    lengths=None,
    use_crf=True,
    **kwargs
):
    """
    Pseudo Label SSL model.

    Calculates likelihood of unlabeled examples, and adds them to the batch of
    labeled examples if the likelihood is above a threshhold.

    :param hidden: The output of the featurizer. [batch_size, sequence_length, embed_dim]
    :param targets: The placeholder representing the sequence labeling targets. [batch_size, sequence_length]
    :param n_targets: A python int containing the number of classes that the model should be learning to predict over.
    :param config: A config object, containing all parameters for the featurizer.
    :param train: If this flag is true, dropout and losses are added to the graph.
    :param reuse: Should reuse be set within this scope.
    :param lengths: The number of non-padding tokens in the input.
    :param kwargs: Spare arguments.
    :return: dict containing:
        "logits": The un-normalised log probabilities of each class being in each location. For usable predictions,
            sampling from this distribution is not sufficient and a viterbi decoding method should be used.
        "losses": The negative log likelihood for the sequence targets.
        "predict_params": A dictionary of params to be fed to the viterbi decode function.
    """
    with tf.compat.v1.variable_scope("psuedo-label", reuse=reuse):
        tf.random.set_seed(config.seed)
        if targets is not None:
            targets = tf.cast(targets, dtype=tf.int32)

        layer = tf.keras.layers.Dense(n_targets)
        logits = tf.cast(layer(hidden), tf.float32)

        loss = 0.0

        default_lengths = tf.shape(input=hidden)[1] * tf.ones(
            tf.shape(input=hidden)[0], dtype=tf.int32
        )
        if lengths is None:
            lengths = default_lengths
            
        
        transition_params = tf.cast(
            tf.compat.v1.get_variable(
                "Transition_matrix", shape=[n_targets, n_targets]
            ),
            tf.float32
        )

        if targets is not None:
            # Seperate labled and unlabeled data
            target_shape = tf.shape(targets)
            u_logits = logits[target_shape[0]:]
            u_lengths = lengths[target_shape[0]:]
            logits = logits[:target_shape[0]]
            lengths = lengths[:target_shape[0]]

            u_targets, u_probs = sequence_decode(u_logits,
                                                 transition_params,
                                                 u_lengths,
                                                 use_gpu_op=False,
                                                 use_crf=use_crf)
            # Get probability of most likely sequence
            if use_crf:
                u_log_likelihood, _ = crf_log_likelihood(
                    u_logits, u_targets, u_lengths, transition_params=transition_params
                )
                u_seq_probs = tf.exp(u_log_likelihood)
            else:
                u_probs = tf.reduce_max(u_probs, axis=-1)
                u_seq_probs = tf.reduce_mean(u_probs, axis=-1)
            
            # Keep only sequences with prob above threshhold
            threshes = tf.ones_like(u_seq_probs) * config.pseudo_thresh
            mask = tf.greater(u_seq_probs, threshes)
            u_logits = tf.boolean_mask(u_logits, mask)
            u_lengths = tf.boolean_mask(u_lengths, mask)
            u_targets = tf.boolean_mask(u_targets, mask)
            u_targets = tf.cast(u_targets, tf.int32)
            logits = tf.concat((logits, u_logits), axis=0)
            lengths = tf.concat((lengths, u_lengths), axis=0)
            targets = tf.concat((targets, u_targets), axis=0)

        class_weights = kwargs.get("class_weights")
        if class_weights is not None and train:
            class_weights = tf.reshape(class_weights, [1, 1, -1])
            one_hot_class_weights = class_weights * tf.one_hot(targets, depth=n_targets)
            per_token_weights = tf.reduce_sum(
                input_tensor=one_hot_class_weights, axis=-1, keepdims=True
            )
            logits = class_reweighting(per_token_weights)(logits)

        if targets is not None:
            if use_crf:
                with tf.device("CPU:0" if train else logits.device):
                    log_likelihood, _ = crf_log_likelihood(
                        logits, targets, lengths, transition_params=transition_params
                    )
                    loss = -log_likelihood
            else:
                weights = tf.sequence_mask(
                    lengths, maxlen=tf.shape(input=targets)[1], dtype=tf.float32
                ) / tf.expand_dims(tf.cast(lengths, tf.float32), -1)
                loss = tf.compat.v1.losses.sparse_softmax_cross_entropy(
                    targets,
                    logits,
                    weights=weights
                )

        return {
            "logits": logits,
            "losses": loss,
            "predict_params": {"transition_matrix": transition_params, "sequence_length": lengths},
        }

def ict(
    hidden,
    targets,
    n_targets,
    config,
    pad_id,
    multilabel=False,
    train=False,
    reuse=None,
    lengths=None,
    use_crf=True,
    embedding_out=None,
    **kwargs
):
    """
    Interopolated Consistency Training SSL model.

    Enforces consistency loss through interpolation of the data. Given two
    points, one prediction is the output of the model on the interpolation of
    the inputs, and the other prediction is the interpolation of the mean
    teacher prediction on each of the inputs. Loss is calculated as MSE between
    the two predictions.
        
    :param hidden: The output of the featurizer. [batch_size, sequence_length, embed_dim]
    :param targets: The placeholder representing the sequence labeling targets. [batch_size, sequence_length]
    :param n_targets: A python int containing the number of classes that the model should be learning to predict over.
    :param config: A config object, containing all parameters for the featurizer.
    :param train: If this flag is true, dropout and losses are added to the graph.
    :param reuse: Should reuse be set within this scope.
    :param lengths: The number of non-padding tokens in the input.
    :param embedding_out: Embedded input fed into the featurizer.
    :param kwargs: Spare arguments.
    :return: dict containing:
        "logits": The un-normalised log probabilities of each class being in each location. For usable predictions,
            sampling from this distribution is not sufficient and a viterbi decoding method should be used.
        "losses": The negative log likelihood for the sequence targets.
        "predict_params": A dictionary of params to be fed to the viterbi decode function.
    """
    with tf.compat.v1.variable_scope("ict", reuse=reuse):
        tf.random.set_seed(config.seed)
        if targets is not None:
            targets = tf.cast(targets, dtype=tf.int32)

        layer = tf.keras.layers.Dense(n_targets)
        logits = tf.cast(layer(hidden), tf.float32)

        loss = 0.0

        default_lengths = tf.shape(input=hidden)[1] * tf.ones(
            tf.shape(input=hidden)[0], dtype=tf.int32
        )
        if lengths is None:
            lengths = default_lengths
        
        device = logits.device
        if targets is not None:
            # Seperate labled and unlabeled data
            target_shape = tf.shape(targets)
            u_logits = logits
            u_lengths = lengths
            u_embed = embedding_out
            u_batch_size = tf.shape(u_embed)[0]
            logits = logits[:target_shape[0]]
            lengths = lengths[:target_shape[0]]

        class_weights = kwargs.get("class_weights")
        if class_weights is not None and train:
            class_weights = tf.reshape(class_weights, [1, 1, -1])
            one_hot_class_weights = class_weights * tf.one_hot(targets, depth=n_targets)
            per_token_weights = tf.reduce_sum(
                input_tensor=one_hot_class_weights, axis=-1, keepdims=True
            )
            logits = class_reweighting(per_token_weights)(logits)
                                                                                                  
        transition_params = tf.cast(
            tf.compat.v1.get_variable(
                "Transition_matrix", shape=[n_targets, n_targets]
            ),
            tf.float32
        )

        if targets is not None:
            scope = kwargs.get("scope")
            featurizer_fn = kwargs.get("featurizer_fn")
            # Get indicies for second batch to interpolate with
            shuffle_indicies = tf.random.shuffle(tf.range(u_batch_size))
            # Create beta distribution to sample from
            beta_dist = tf.compat.v1.distributions.Beta(config.ict_alpha,
                                                        config.ict_alpha)
            
            # Create prediction on mixed inputs
            lam = beta_dist.sample((u_batch_size, 1, 1))
            shuffle_input = tf.gather(u_embed, shuffle_indicies, axis=0)
            mix_input = lam * u_embed + (1 - lam) * shuffle_input
            # Revert scope to reuse featurizer weights
            with tf.compat.v1.variable_scope(scope):
                featurizer_state = featurizer_fn(mix_input, reuse=True)
            hidden = featurizer_state["sequence_features"]
            mix_logits = layer(hidden)

            # Create mixed prediction on normal inputs
            with tf.compat.v1.variable_scope(scope):
                custom_getter = build_ema_getter("ema", decay=config.ema_decay)
            update_ops = tf.compat.v1.get_collection(tf.compat.v1.GraphKeys.UPDATE_OPS)
            with tf.control_dependencies(update_ops):
                with tf.compat.v1.variable_scope(scope, custom_getter=custom_getter):
                    featurizer_state = featurizer_fn(u_embed, reuse=None)
                hidden = featurizer_state["sequence_features"]
                ema_logits = layer(hidden)
            lam = beta_dist.sample((u_batch_size, 1, 1))
            ema_shuffle_logits = tf.gather(ema_logits, shuffle_indicies, axis=0)
            ema_mix_logits = lam * ema_logits + (1 - lam) * ema_shuffle_logits
            ema_mix_logits = tf.stop_gradient(ema_mix_logits)

            u_loss = tf.keras.losses.MSE(mix_logits, ema_mix_logits)
            u_loss = tf.compat.v1.Print(u_loss, [u_loss, tf.shape(u_loss),
                                                 ema_mix_logits,
                                                 tf.shape(ema_mix_logits),
                                                 mix_logits,
                                                 tf.shape(mix_logits),
                                                 mix_input,
                                                 tf.shape(mix_input),
                                                 u_embed,
                                                 tf.shape(u_embed),
                                                 lam,
                                                 tf.shape(lam)])
            u_loss = tf.reduce_mean(u_loss)

            # Get TSA threshhold and discard confident labeled examples
            if config.tsa_method:
                logits, targets, lengths = tsa_filter(config.tsa_method,
                                                      logits, targets, lengths,
                                                      use_crf,
                                                      transition_params,
                                                      kwargs.get("total_num_steps"))
            if use_crf:
                with tf.device("CPU:0" if train else device):
                    log_likelihood, _ = crf_log_likelihood(
                        logits, targets, lengths, transition_params=transition_params
                    )
                    loss = -log_likelihood
            else:
                weights = tf.sequence_mask(
                    lengths, maxlen=tf.shape(input=targets)[1], dtype=tf.float32
                ) / tf.expand_dims(tf.cast(lengths, tf.float32), -1)
                loss = tf.compat.v1.losses.sparse_softmax_cross_entropy(
                    targets,
                    logits,
                    weights=weights
                )

            if config.tsa_method:
                # Make loss 0 if there are no targets that are over the thresh
                loss = tf.cond(tf.equal(tf.shape(targets)[0], 0),
                               lambda: 0.0,
                               lambda: loss)


            # Get current SSL loss coeficient
            total_steps = kwargs.get("total_num_steps")
            global_step = tf.compat.v1.train.get_or_create_global_step()
            training_fraction = tf.cast(global_step, dtype=tf.float32) / total_steps
            coef_fraction = warmup_constant(training_fraction, warmup=0.25)
            loss_coef = tf.maximum(0.0, config.ssl_loss_coef * coef_fraction)
            tf.compat.v1.summary.scalar("SSL Loss Coef",  loss_coef)

            loss = tf.reduce_mean(loss) + loss_coef * u_loss
            loss = tf.compat.v1.Print(loss, [loss, tf.shape(loss)])

        return {
            "logits": logits,
            "losses": loss,
            "predict_params": {"transition_matrix": transition_params, "sequence_length": lengths},
        }


def mean_teacher(
    hidden,
    targets,
    n_targets,
    config,
    pad_id,
    multilabel=False,
    train=False,
    reuse=None,
    lengths=None,
    use_crf=True,
    embedding_out=None,
    **kwargs
):
    """
    Mean Teacher SSL model.

    Tracks the exponential moving average of the model weights, and enforces
    consistency loss between normal predictions and predictions produced by the
    EMA model.

    :param hidden: The output of the featurizer. [batch_size, sequence_length, embed_dim]
    :param targets: The placeholder representing the sequence labeling targets. [batch_size, sequence_length]
    :param n_targets: A python int containing the number of classes that the model should be learning to predict over.
    :param config: A config object, containing all parameters for the featurizer.
    :param train: If this flag is true, dropout and losses are added to the graph.
    :param reuse: Should reuse be set within this scope.
    :param lengths: The number of non-padding tokens in the input.
    :param embedding_out: Embedded input fed into the featurizer.
    :param kwargs: Spare arguments.
    :return: dict containing:
        "logits": The un-normalised log probabilities of each class being in each location. For usable predictions,
            sampling from this distribution is not sufficient and a viterbi decoding method should be used.
        "losses": The negative log likelihood for the sequence targets.
        "predict_params": A dictionary of params to be fed to the viterbi decode function.
    """
    with tf.compat.v1.variable_scope("mean_teacher", reuse=reuse):
        tf.random.set_seed(config.seed)
        if targets is not None:
            targets = tf.cast(targets, dtype=tf.int32)

        layer = tf.keras.layers.Dense(n_targets)
        logits = tf.cast(layer(hidden), tf.float32)

        loss = 0.0

        default_lengths = tf.shape(input=hidden)[1] * tf.ones(
            tf.shape(input=hidden)[0], dtype=tf.int32
        )
        if lengths is None:
            lengths = default_lengths
        
        if targets is not None:
            # Seperate labled and unlabeled data
            target_shape = tf.shape(targets)
            all_logits = logits
            all_lengths = lengths
            logits = logits[:target_shape[0]]
            lengths = lengths[:target_shape[0]]

        class_weights = kwargs.get("class_weights")
        if class_weights is not None and train:
            class_weights = tf.reshape(class_weights, [1, 1, -1])
            one_hot_class_weights = class_weights * tf.one_hot(targets, depth=n_targets)
            per_token_weights = tf.reduce_sum(
                input_tensor=one_hot_class_weights, axis=-1, keepdims=True
            )
            logits = class_reweighting(per_token_weights)(logits)
                                                                                                  
        transition_params = tf.cast(
            tf.compat.v1.get_variable(
                "Transition_matrix", shape=[n_targets, n_targets]
            ),
            tf.float32
        )

        if targets is not None:
            # Make EMA prediction and get consistency loss
            scope = kwargs.get("scope")
            featurizer_fn = kwargs.get("featurizer_fn")
            with tf.compat.v1.variable_scope(scope):
                custom_getter = build_ema_getter("ema", decay=config.ema_decay)
            update_ops = tf.compat.v1.get_collection(tf.compat.v1.GraphKeys.UPDATE_OPS)
            with tf.control_dependencies(update_ops):
                with tf.compat.v1.variable_scope(scope, custom_getter=custom_getter):
                    featurizer_state = featurizer_fn(embedding_out, reuse=None)
                hidden = featurizer_state["sequence_features"]
                ema_logits = tf.stop_gradient(layer(hidden))

            u_loss = tf.keras.losses.MSE(all_logits, ema_logits)
            u_loss = tf.reduce_mean(u_loss)

            if config.tsa_method:
                print("USING TSA")
                logits, targets, lengths = tsa_filter(config.tsa_method,
                                                      logits, targets, lengths,
                                                      use_crf,
                                                      transition_params,
                                                      kwargs.get("total_num_steps"))
            if use_crf:
                with tf.device("CPU:0" if train else device):
                    log_likelihood, _ = crf_log_likelihood(
                        logits, targets, lengths, transition_params=transition_params
                    )
                    loss = -log_likelihood
            else:
                weights = tf.sequence_mask(
                    lengths, maxlen=tf.shape(input=targets)[1], dtype=tf.float32
                ) / tf.expand_dims(tf.cast(lengths, tf.float32), -1)
                loss = tf.compat.v1.losses.sparse_softmax_cross_entropy(
                    targets,
                    logits,
                    weights=weights
                )

            if config.tsa_method:
                # Make loss 0 if there are no targets that are over the thresh
                loss = tf.cond(tf.equal(tf.shape(targets)[0], 0),
                               lambda: 0.0,
                               lambda: loss)

            # Get current SSL loss coeficient
            total_steps = kwargs.get("total_num_steps")
            global_step = tf.compat.v1.train.get_or_create_global_step()
            training_fraction = tf.cast(global_step, dtype=tf.float32) / total_steps
            coef_fraction = warmup_constant(training_fraction, warmup=0.25)
            loss_coef = tf.maximum(0.0, config.ssl_loss_coef * coef_fraction)
            tf.compat.v1.summary.scalar("SSL Loss Coef",  loss_coef)

            loss = tf.reduce_mean(loss) + config.ssl_loss_coef * u_loss

        return {
            "logits": logits,
            "losses": loss,
            "predict_params": {"transition_matrix": transition_params, "sequence_length": lengths},
        }

def association(
    hidden, lengths, targets, n_targets, config, train=False, reuse=None, **kwargs
):
    """
    An Attention based sequence labeler model with association.

    :param hidden: The output of the featurizer. [batch_size, sequence_length, embed_dim]
    :param lengths: The number of non-padding tokens in the input.
    :param targets: A dict containing:
     'labels': The sequence labeling targets. [batch_size, sequence_length],
     'associations': A matrix of class ids for the associations [batch_size, sequence_length, seqence_length]
    :param n_targets: A python int containing the number of classes that the model should be learning to predict over.
    :param config: A config object, containing all parameters for the featurizer.
    :param train: If this flag is true, dropout and losses are added to the graph.
    :param reuse: Should reuse be set within this scope.
    :param kwargs: Spare arguments.
    :return: dict containing:
        "logits": The un-normalised log probabilities of each class being in each location. For usable predictions,
            sampling from this distrobution is not sufficiant and a viterbi decoding method should be used.
        "losses": The negative log likelihood for the sequence targets.
        "predict_params": A dictionary of params to be fed to the viterbi decode function.
    """
    with tf.compat.v1.variable_scope("sequence-labeler", reuse=reuse):
        nx = config.n_embed
        length = config.max_length
        num_associations = len(config.association_types) + 1

        def seq_lab_internal(hidden):
            attn_fn = functools.partial(
                attn,
                scope="seq_label_attn",
                n_state=nx,
                n_head=config.seq_num_heads,
                resid_pdrop=config.resid_p_drop,
                attn_pdrop=config.attn_p_drop,
                train=train,
                scale=False,
                mask=False,
                lengths=lengths,
            )
            n = norm(attn_fn(hidden) + hidden, "seq_label_residual")
            flat_logits = tf.compat.v1.layers.dense(n, n_targets)
            logits = tf.reshape(
                flat_logits, tf.concat([tf.shape(input=hidden)[:2], [n_targets]], 0)
            )

            association_head = tf.compat.v1.layers.dense(n, nx)
            association_head = tf.reshape(
                association_head, tf.concat([tf.shape(input=hidden)[:2], [nx]], 0)
            )

            a = tf.expand_dims(association_head, 1)
            b = tf.expand_dims(association_head, 2)

            features = tf.concat(
                [
                    a - b,
                    a * b,
                    tf.tile(a, [1, length, 1, 1]),
                    tf.tile(b, [1, 1, length, 1]),
                    # TODO: Think about using prediction as a feature for associations.
                ],
                axis=-1,
            )
            associations_flat = tf.compat.v1.layers.dense(
                tf.reshape(features, shape=[-1, nx * 4]), num_associations
            )
            associations = tf.reshape(
                associations_flat, [-1, length, length, num_associations]
            )

            return logits, associations_flat, associations

        with tf.compat.v1.variable_scope("seq_lab_attn"):
            if config.low_memory_mode and train:
                seq_lab_internal = recompute_grad(
                    seq_lab_internal, use_entire_scope=True
                )

            logits, associations_flat, associations = seq_lab_internal(hidden)

        log_likelihood = 0.0
        association_loss = 0.0
        class_weights = kwargs.get("class_weights")
        if class_weights is not None:
            logits = class_reweighting(class_weights)(logits)

        transition_params = tf.compat.v1.get_variable(
            "Transition_matrix", shape=[n_targets, n_targets]
        )
        if targets is not None:
            log_likelihood, _ = crf_log_likelihood(
                logits,
                targets["labels"],
                kwargs.get("max_length") * tf.ones(tf.shape(input=targets["labels"])[0]),
                transition_params=transition_params,
            )
            sequence_mask = tf.sequence_mask(
                lengths, maxlen=length, dtype=tf.float32
            )
            mask = tf.expand_dims(sequence_mask, 1) * tf.expand_dims(sequence_mask, 2)

            association_loss = tf.compat.v1.losses.sparse_softmax_cross_entropy(
                logits=associations_flat,
                labels=tf.reshape(targets["associations"], shape=[-1]),
                weights=tf.reshape(mask, shape=[-1]),
            )

        return {
            "logits": {"sequence": logits, "association": associations},
            "losses": -log_likelihood
            + config.assocation_loss_weight
            * association_loss,  # TODO: think about weighting.
            "predict_params": {"transition_matrix": transition_params},
        }

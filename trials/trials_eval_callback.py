from datasets import load_from_disk

from transformers import AutoTokenizer
from transformers import AutoModelForMaskedLM
from transformers.models.roberta.configuration_roberta import *
from transformers.models.roberta.modeling_roberta import *
from transformers import TrainingArguments
from transformers import DataCollatorForLanguageModeling
from transformers import Trainer
from transformers import EarlyStoppingCallback, TrainerCallback

import torch
import pandas as pd
import os
import time
import subprocess as sp
import copy
import logging
import json
import numpy as np


def sample_decode(ds, tokenizer, split='train', n=5):
    for i in range(n):
        split_ds = ds[split]
        print(tokenizer.decode(split_ds[i]['input_ids']))
        print("\n\n\n")


def describe(df):
    base = df.describe()
    base.loc["1%"] = df.quantile(0.01)
    base.loc["99%"] = df.quantile(0.99)
    return base.reindex(["count", "mean", "std", "min", "1%", "25%", "50%", "75%", "99%", "max"])


def describe_lens(ds, split='train', user=False):
    df = ds[split].to_pandas()
    if user:
        print("Not implemented yet")
    else:
        print(describe(df['input_ids'].str.len()))

logger = logging.getLogger(__name__)

class evalLogsCallback(TrainerCallback):

    def __init__(self):
        super().__init__()
        self.metrics = {}

    def on_evaluate(self, args, state, control, **kwargs):
        metrics = kwargs['metrics']
        if control.should_save:
            metrics["perplexity"] = math.exp(metrics["eval_loss"])
            metrics['trial_params'] = json.dumps(state.trial_params)
        self.metrics = metrics.copy()
        logger.info(json.dumps(metrics))

    def on_save(self, args, state, control, **kwargs):
        output_dir = state.best_model_checkpoint.split('/checkpoint')[0]
        print(self.metrics, output_dir)
        self.save_metrics('eval_{}'.format(self.metrics['epoch']), self.metrics, output_dir)

    def on_train_end(self, args, state, control, **kwargs):
        output_dir = state.best_model_checkpoint.split('/checkpoint')[0]
        metrics = state.trial_params.copy()
        metrics["number_of_gpus"] = args.n_gpu
        metrics["best_loss"] = state.best_metric
        metrics["best_perplexity"] = math.exp(state.best_metric)
        metrics["best_model_checkpoint"] = state.best_model_checkpoint
        self.metrics = metrics
        self.save_metrics('final', self.metrics, output_dir)

    def save_metrics(self, split, metrics, output_dir, combined=True):
        path = os.path.join(output_dir, f"{split}_results.json")
        with open(path, "w") as f:
            json.dump(metrics, f, indent=4, sort_keys=True)


def run_trials(data_path, model_path, run_name, pos_embd_strat="load_512"):

    # load saved dataset
    print("loading dataset...")
    ds = load_from_disk(data_path)
    
    # tokenizer
    tokenizer = AutoTokenizer.from_pretrained("roberta-base")
    # load config and model
    config_dict = {
            'vocab_size' : 50265, # number of total tokens allowed
            'num_hidden_layers' : 6, # number of hidden RobertaLayers in a RobertaEncoder
            'num_attention_heads' : 12, # multi-headed attention heads
            'hidden_size' : 768, # dimension of hidden layers
            'intermediate_size' : 3072, # dimension of feedfoward layer in encoder
            'max_position_embeddings' : 514, # max seq. length the model could ever have
            'new_max_position_embeddings' : 4098, # max seq. length the model could ever have
            'hidden_act' : "gelu", # nonlinearity in the encoder and pooler
            'hidden_dropout_prob' : 0.1, # dropout probability for fully conn. layers
            'attention_probs_dropout_prob' : 0.1, # dropout in attn layer
            'type_vocab_size' : 1, # for 'token_type_ids' column
            'initializer_range' : 0.02, # stdev for initializing weight matrices
            'layer_norm_eps' : 1e-05, # epsilon in layer norm
            'position_embedding_type' : 'absolute', # there's special pos embds
            'bos_token_id' : 0,
            'pad_token_id' : 1,
            'eos_token_id' : 2,
            'model_type' : 'roberta',
            'is_decoder' : False, # is decoder-only
            'use_cache' : True, # return the last attn key/values
            } 
    roberta_config = RobertaConfig(**config_dict)
    
    def model_init():
        distil_model = AutoModelForMaskedLM.from_pretrained("distilroberta-base",
            config=roberta_config, ignore_mismatched_sizes=True)
        # expand positional embds with custom method in modeling_roberta.py
        use_trained = False if pos_embd_strat == "no_load" else True
        repeat = True if pos_embd_strat == "load_repeat" else False
        distil_model.expand_embds(roberta_config.new_max_position_embeddings,
                use_trained=use_trained, repeat=repeat)
        print("pos embds stdev: ",
                torch.std(distil_model.roberta.embeddings.position_embeddings.weight))
        return distil_model

    def optuna_hp_space(trial):
        return {
                "learning_rate": trial.suggest_float("learning_rate", low=5e-5, high=5e-2, log=True),
                "weight_decay": trial.suggest_float("weight_decay", low=0.001, high=0.01, log=True),
                "attention_probs_dropout_prob": trial.suggest_float("attention_probs_dropout_prob", low=0.05, high=0.5, log=True),
                }

    def compute_objective(metrics) -> float:
        metrics = copy.deepcopy(metrics)
        loss = metrics.pop("eval_loss", None)
        return loss

    # training arguments
    training_args = TrainingArguments(
        output_dir = model_path,
        overwrite_output_dir=True,
        logging_strategy="steps",
        logging_steps=500,
        save_strategy="epoch",
        evaluation_strategy="epoch",
        gradient_accumulation_steps=1,
        # learning_rate=5e-5,
        # weight_decay=0,
        # warmup_ratio=0.02,
        num_train_epochs=1,
        per_device_train_batch_size=1,
        save_steps=5000,
        save_total_limit=2,
        prediction_loss_only=False,
        metric_for_best_model='eval_loss',
        load_best_model_at_end=True,
        greater_is_better=False,
        run_name=run_name,
    )

    # data collator - performs batching and masking (i think)
    data_collator = DataCollatorForLanguageModeling(tokenizer=tokenizer,
            mlm=True, mlm_probability=0.15)

    # create callback for early stopping
    if False:
        early_stop = EarlyStoppingCallback(early_stopping_patience=3)


    # instantiate trainer
    trainer = Trainer(
        model_init=model_init,
        args=training_args,
        train_dataset=ds['train'].select(np.arange(4)),
        eval_dataset=ds['validation'].select(np.arange(4)),
        data_collator = data_collator,
        callbacks=[evalLogsCallback] #, early_stop],
    )
    backend = 'optuna'
    best_trial = trainer.hyperparameter_search(
            backend=backend,
            hp_space=optuna_hp_space,
            n_trials=10,
            compute_objective=compute_objective,
            )

    metrics = {}
    metrics["best_trial_details"] = json.dumps(best_trial) # run_id, loss, hyperparams
    metrics["best_trial_perplexity"] = math.exp(best_trial[1])
    trainer.log_metrics("trial", metrics)
    trainer.save_metrics("trial", metrics, combined=False)
    trainer.save_model()


# method to help pick a free GPU
def pick_gpu():
    command = "nvidia-smi --query-gpu=memory.free --format=csv"
    memory_free_info = sp.check_output(command.split()).decode('ascii').split('\n')[:-1][1:]
    memory_free_values = [int(x.split()[0]) for i, x in enumerate(memory_free_info)]

    for j in range(len(memory_free_values)):
        if memory_free_values[j] == 48676:
            print(f"using GPU {j}")
            os.environ["CUDA_VISIBLE_DEVICES"] = str(j)
            break


# ------------------- Main method ---------------------------

base_data_path = "/cronus_data/ssmith/data/blogsUD/"
base_model_path = "/cronus_data/ssmith/models/blogsUD/"
sample_chunked_path = base_data_path + "sample_chunked_4096"
sample_chunked_dsep_path = base_data_path + "sample_chunked_dsep_4096"
normal_model_path = base_model_path + "sample_normal_trials"
dsep_model_path = base_model_path + "sample_dsep_trials"
wandb_test_path = base_model_path + "new_optuna_param_test_2"


if True:
    data_paths = [sample_chunked_path]
    model_paths = [wandb_test_path]
    pos_embd_strats = ["load_repeat"]
    run_names = ["new_optuna_param_test"]
if False:
    data_paths = [sample_chunked_dsep_path, sample_chunked_path]
    model_paths = [dsep_model_path, normal_model_path]
    pos_embd_strats = ["load_repeat", "load_repeat"] # "load_repeat", "load_512", or "no_load"
    run_names = ["dsep_experiment_1", "dsep_experiment_2"]

pick_gpu() # pick an open GPU to use to train

# train
for i in range(len(data_paths)):
    run_trials(data_path=data_paths[i],
            model_path=model_paths[i],
            pos_embd_strat = pos_embd_strats[i],
            run_name = run_names[i],
    )
    print("\n\n------\n\n------\n\n")
    time.sleep(60)

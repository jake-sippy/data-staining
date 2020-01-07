import utils
import biases
from explainers import(
        LimeExplainer,
        ShapExplainer,
        ShapZerosExplainer,
        ShapMedianExplainer,
        GreedyExplainer,
        LogisticExplainer,
        TreeExplainer,
        RandomExplainer,
)

import os
import sys
import time
import json
import pprint
import sklearn
import argparse
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from tqdm import tqdm
from multiprocessing import Pool
from sklearn.pipeline import Pipeline
from lime.lime_text import LimeTextExplainer
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.metrics import (
        classification_report,
        accuracy_score,
        recall_score,
        f1_score
)
from sklearn.utils import resample
from sklearn.linear_model import LogisticRegression
from sklearn.neural_network import MLPClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.tree import DecisionTreeClassifier

global args                     # Arguments from cmd line
TEST_NAME = 'budget_test'       # Name of this test
LOG_PATH = 'logs'               # Top level directory for log files
LOGGING_ENABLED = True          # Save logs for this run if true
POOL_SIZE = 10                  # How many workers to spawn (one per seed)
TRAIN_SIZE = 0.9                # Train split ratio (including dev)
MIN_OCCURANCE = 0.10            # Min occurance for n-grams to be included
MAX_OCCURANCE = 0.50            # Max occurance for n-grams to be included
MAX_BUDGET = 5                  # Upper bound of budget to test explainers
N_SAMPLES = 50                  # Number of samples to evaluate each exapliner

# Mapping of model names to model objects
MODELS = {
    # 'mlp': MLPClassifier(),   # cannot use sample weights
    'dt': DecisionTreeClassifier(),
    'logistic': LogisticRegression(solver='lbfgs'),
    'rf': RandomForestClassifier(n_estimators=50)
}

# The set of explainers to test
EXPLAINERS = {
        'Random': RandomExplainer,
        'Greedy': GreedyExplainer,
        'LIME': LimeExplainer,
        'SHAP(kmeans)': ShapExplainer,
}


def run_seed(seed):
    """
    Runs a single seed of the test.

    Run a single seed of the test including biasing data, training models, and
    evaluating performance across regions of the dataset.
    """
    if args.quiet: sys.stdout = open(os.devnull, 'w')

    # Set metadata in runlog
    runlog = {}
    runlog['test_name'] = TEST_NAME
    runlog['seed'] = seed

    print('\nRunning SEED = {} ------------------------------'.format(seed))
    np.random.seed(seed)

    reviews_train, \
    reviews_test,  \
    labels_train,  \
    labels_test = utils.load_dataset(args.dataset, TRAIN_SIZE, runlog)

    # Create bias #############################################################
    bias_obj = biases.ComplexBias(
            reviews_train,
            labels_train,
            args.bias_length,
            MIN_OCCURANCE,
            MAX_OCCURANCE,
            runlog
    )
    labels_train_bias, biased_train = bias_obj.bias(reviews_train, labels_train)
    labels_test_bias, biased_test = bias_obj.bias(reviews_test, labels_test)

    # Preprocessing reviews TODO Generalize for pytorch models
    X_train,  \
    X_test,   \
    pipeline, \
    feature_names = utils.vectorize_dataset(
            reviews_train,
            reviews_test,
            MIN_OCCURANCE,
            MAX_OCCURANCE,
            runlog
    )

    # Convert to pandas df
    train_df = pd.DataFrame(data=X_train, columns=feature_names)
    train_df['label_orig'] = labels_train
    train_df['label_bias'] = labels_train_bias
    train_df['biased'] = biased_train

    test_df = pd.DataFrame(data=X_test, columns=feature_names)
    test_df['label_orig'] = labels_test
    test_df['label_bias'] = labels_test_bias
    test_df['biased'] = biased_test

    # Resampling dataset #######################################################
    train_df = utils.resample(train_df, feature_names)

    # Training biased model ####################################################
    model_orig, model_bias = utils.train_models(
            args.model,
            MODELS,
            train_df,
            runlog
    )

    utils.evaluate_models(model_orig, model_bias, test_df, runlog)

    # Get data points to test explainer on #####################################

    R = train_df[ train_df['biased'] ]
    # DEBUG testing intersections of R and D
    # Dpos = train_df['label_orig'] == bias_obj.bias_label
    # R_diff_Dpos = train_df[ (train_df['biased']) & (~Dpos) ]
    # R_union_Dpos = train_df[ (train_df['biased']) & (Dpos) ]
    explain = R
    print('\t\tNUM_EXPLAIN = {}'.format(len(explain)))

    drop_cols = ['label_orig', 'label_bias', 'biased']
    X_explain = explain.drop(drop_cols, axis=1).values
    n_samples = min(N_SAMPLES, len(X_explain))
    runlog['n_samples'] = n_samples
    print('\t\tNUM_SAMPLES = {}'.format(n_samples))

    # Handle interpretable models by adding their respective explainer
    if args.model == 'logistic':
        EXPLAINERS['Ground Truth'] = LogisticExplainer
    elif args.model == 'dt' or args.model == 'rf':
        EXPLAINERS['Ground Truth'] = TreeExplainer

    for name in EXPLAINERS:
        runlog['explainer'] = name

        print('\tEXPLAINER = {}'.format(name))

        explainer = EXPLAINERS[name](
                model_bias,
                X_train,
                feature_names,
                seed
        )

        for budget in range(1, MAX_BUDGET + 1):
            runlog['budget'] = budget

            tp_error = 0
            fn_error = 0
            recall_sum = 0

            for i in range(n_samples):
                instance = X_explain[i]
                top_feats = explainer.explain(instance, budget)

                recall = 0
                for word in bias_obj.bias_words:
                    if word in top_feats:
                        recall += 1
                recall /= args.bias_length
                # DEBUG testing ground truth explainer, print when fails
                # if budget >= runlog['bias_len'] and recall < 1.0:
                #     explainer.explain(instance, budget, p=True)
                recall_sum += recall

            # Compute faithfulness w/ recall
            recall = recall_sum / n_samples
            # DEBUG: test where budget 2 is failing
            runlog['recall'] = recall
            print('\t\tAVG_RECALL   = {:.4f}'.format(recall))

            if LOGGING_ENABLED:
                filename = '{:s}_{:d}_{:03d}_{:02d}.json'.format(
                        name, args.bias_length, seed, budget)
                utils.save_log(LOG_PATH, filename, runlog)



def setup_argparse():
    desc = 'This script is meant to compare multiple explainers\' ability to' \
           'recover bias that we have systematically introduced to models.'
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument(
            'dataset',
            type=str,
            metavar='DATASET',
            help='CSV dataset to bias')
    parser.add_argument(
            'model',
            type=str,
            metavar='MODEL',
            help=' | '.join(list(MODELS.keys())))
    parser.add_argument(
            'seed_low',
            type=int,
            metavar='SEED_LOW',
            help='Lower bound of seeds to loop over (inclusive)')
    parser.add_argument(
            'seed_high',
            type=int,
            metavar='SEED_HIGH',
            help='Higher bound of seeds to loop over (exclusive)')
    parser.add_argument(
            'bias_length',
            type=int,
            metavar='BIAS_LENGTH',
            help='Number of features to include in bias')
    parser.add_argument(
            '--log-dir',
            type=str,
            metavar='LOG_DIR',
            default=LOG_PATH,
            help='Log file directory (default = {})'.format(LOG_PATH))
    parser.add_argument(
            '--quiet',
            action='store_true',
            help='Do not print out information while running')

    # Check args
    args = parser.parse_args()
    assert (args.seed_low < args.seed_high), \
            'No seeds in range [{}, {})'.format(args.seed_low, args.seed_high)
    assert args.model in MODELS, \
            'Model name not recognized ({}), must be one of {}'.format(
                    args.model, list(MODELS.keys()))
    return args


if __name__ == '__main__':
    args = setup_argparse()
    seeds = range(args.seed_low, args.seed_high)
    if POOL_SIZE > 1:
        pool = Pool(POOL_SIZE)
        pool.map(run_seed, seeds)
        pool.close()
        pool.join()
    else:
        for seed in seeds:
            run_seed(seed)

# -*- coding: utf-8 -*-

# Copyright © 2023-2024 Apple Inc.

import argparse
import logging
import os
import sys
import yaml

from .weighting import (WeightByTokens, WeightByCubeRootTokens,
                        WeightByLogTokens, WeightByTokensClipped,
                        WeightBySqrtTokens)

import numpy as np

from pfl.aggregate.weighting import WeightByUser
from pfl.algorithm import (NNAlgorithmParams, FederatedAveraging, FedProx,
                           FedProxParams)
from pfl.privacy import (
    CentrallyApplicablePrivacyMechanism, CentrallyAppliedPrivacyMechanism,
    NoPrivacy, GaussianMechanism, LaplaceMechanism,
    MomentsAccountantGaussianMechanism, NormClippingOnly, PrivUnitMechanism)
from pfl.privacy.ftrl_mechanism import BandedMatrixFactorizationMechanism

logger = logging.getLogger(name=__name__)


def maybe_inject_arguments_from_config():
    # Check if config is provided in command-line using a temporary parser
    arg_parser = argparse.ArgumentParser(add_help=False)
    arg_parser.add_argument('--args_config')
    temp_args, _ = arg_parser.parse_known_args()

    if temp_args.args_config:
        with open(temp_args.args_config, 'r') as file:
            config = yaml.safe_load(file)

        # Inject config arguments into sys.argv
        for key, value in config.items():
            # Only add config items if they are not already in sys.argv
            if f"--{key}" not in sys.argv:
                sys.argv.extend([f"--{key}", str(value)])


def add_seed_arguments(
        parser: argparse.ArgumentParser) -> argparse.ArgumentParser:

    parser.add_argument(
        "--seed", type=int, default=0, help=('Seed to use for pseudo rng'))

    return parser


def add_filepath_arguments(
        parser: argparse.ArgumentParser) -> argparse.ArgumentParser:

    parser.add_argument(
        '--args_config',
        help='Path to YAML configuration file containing the arguments.')

    parser.add_argument(
        '--data_path',
        required=True,
        help='The path from which the dataset will be read.')

    parser.add_argument(
        "--restore_model_path",
        default=None,
        help='Path to model checkpoint to restore')

    return parser


def add_weighting_arguments(
        parser: argparse.ArgumentParser) -> argparse.ArgumentParser:

    parser.add_argument(
        '--weighting',
        choices=[
            'user', 'token', 'sqrt_token', 'cuberoot_token', 'log_token',
            'clip_token'
        ],
        default='token',
        help=(
            'Weighting strategy to use. Weight by number of users, number of '
            'tokens or a function sublinear to the number of tokens.'))

    parser.add_argument(
        "--weight_clip",
        type=int,
        default=1600,
        help='What maximum weight to use when weighting=clip_token.')

    return parser


def parse_weighting_strategy(name, weight_clip):
    if name == 'user':
        weighting_strategy = WeightByUser()
    elif name == 'token':
        weighting_strategy = WeightByTokens()
    elif name == 'cuberoot_token':
        weighting_strategy = WeightByCubeRootTokens()
    elif name == 'log_token':
        weighting_strategy = WeightByLogTokens()
    elif name == 'clip_token':
        weighting_strategy = WeightByTokensClipped(weight_clip)
    else:
        assert name == 'sqrt_token'
        weighting_strategy = WeightBySqrtTokens()
    return weighting_strategy


class store_bool(argparse.Action):
    def __init__(self, option_strings, dest, **kwargs):
        argparse.Action.__init__(self, option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        false_values = set(['false', 'no'])
        true_values = set(['true', 'yes'])

        values = values.lower()

        if not values in (false_values | true_values):
            raise argparse.ArgumentError(
                self, 'Value must be either "true" or "false"')
        value = (values in true_values)

        setattr(namespace, self.dest, value)


def add_iterative_arguments(argument_parser):
    argument_parser.add_argument(
        '--central_num_iterations',
        type=int,
        default=200,
        help='Number of iterations of global training, i.e. '
        'the number of minibatches.')

    argument_parser.add_argument(
        "--evaluation_frequency",
        type=int,
        default=10,
        help=('Perform an evaluation step every n iterations, where n is '
              'the value of this argument.'))

    return argument_parser


def add_dnn_training_arguments(argument_parser):

    argument_parser = add_iterative_arguments(argument_parser)

    argument_parser.add_argument(
        '--cohort_size',
        type=int,
        default=1000,
        help='The target number of users for one iteration '
        'of training.')

    argument_parser.add_argument(
        '--noise_cohort_size',
        type=int,
        default=1000,
        help=('The cohort size to use in calculating noise for DP. '
              'If you run cohort_size=100 but noise_cohort_size=1000, '
              'then your results will only be valid if running with '
              'cohort_size=1000 outside simulation'))

    argument_parser.add_argument(
        '--val_cohort_size',
        type=int,
        default=200,
        help='The target number of users for distributed evaluation.')

    argument_parser.add_argument(
        '--learning_rate',
        type=float,
        default=1.0,
        help='Learning rate for training of the centralised model.')

    argument_parser.add_argument(
        '--local_num_epochs',
        type=int,
        default=5,
        help='Number of epochs for local training of one user.')

    argument_parser.add_argument(
        '--local_batch_size',
        type=int,
        default=None,
        help='Batch size for local training of one user.')

    argument_parser.add_argument(
        '--local_learning_rate',
        type=float,
        default=0.1,
        help='Learning rate for training on the client.')

    argument_parser.add_argument(
        '--weight_by_samples',
        action=store_bool,
        default=False,
        help='Weight each user by how many samples they train on.')

    argument_parser.add_argument(
        '--central_eval_batch_size',
        type=int,
        default=256,
        help='Batch size for central evaluation callback.')

    return argument_parser


def add_mechanism_arguments(argument_parser):

    # Arguments that govern the local privacy mechanism.
    argument_parser.add_argument(
        '--local_privacy_mechanism',
        choices=[
            'none',
            'gaussian',
            'privunit',
            'laplace',
            'norm_clipping_only',
        ],
        default='none',
        help='The type of privacy mechanism to apply for each user.')

    argument_parser.add_argument(
        '--local_epsilon',
        type=float,
        default=1.,
        help='Bound on the privacy loss (often called ε) for the local privacy '
        'mechanism.')

    argument_parser.add_argument(
        '--local_delta',
        type=float,
        default=1e-05,
        help='Bound on the probability that the privacy loss '
        'is greater than ε (often called δ) for the local privacy mechanism.')

    argument_parser.add_argument(
        '--local_privacy_clipping_bound',
        type=float,
        default=1.0,
        help=
        'Bound for norm clipping so that the local privacy mechanism can be '
        'applied.')

    class OrderAction(argparse.Action):
        def __init__(self, option_strings, dest, nargs=None, **kwargs):
            if nargs is not None:
                raise ValueError("nargs not allowed")
            super(OrderAction, self).__init__(option_strings, dest, **kwargs)

        def __call__(self, parser, namespace, values, option_string=None):
            if values is None:
                return
            if values == 'inf':
                setattr(namespace, self.dest, np.inf)
            else:
                setattr(namespace, self.dest, float(values))

    argument_parser.add_argument(
        '--local_order',
        action=OrderAction,
        help='Order of the lp-norm for local norm clipping only.')

    # Arguments that govern the central privacy mechanism.

    argument_parser.add_argument(
        '--central_epsilon',
        type=float,
        default=1.,
        help=
        'Bound on the privacy loss (often called ε) for the central privacy '
        'mechanism.')

    argument_parser.add_argument(
        '--central_delta',
        type=float,
        default=1e-05,
        help='Bound on the probability that the privacy loss '
        'is greater than ε (often called δ) for the central privacy mechanism.'
    )

    argument_parser.add_argument(
        '--central_privacy_clipping_bound',
        type=float,
        default=1.0,
        help='Bound for norm clipping for each user, so '
        'that the central privacy mechanism can be applied.')

    argument_parser.add_argument(
        '--central_privacy_mechanism',
        choices=[
            'none', 'gaussian', 'gaussian_moments_accountant', 'laplace',
            'norm_clipping_only', 'banded_matrix_factorization'
        ],
        default='none',
        help='The type of privacy mechanism to apply for each model update.')

    argument_parser.add_argument(
        '--central_order',
        action=OrderAction,
        help='Order of the lp-norm for central norm clipping only.')

    argument_parser.add_argument(
        '--population',
        type=float,
        default=1e7,
        help='Size of population (i.e. number of training users).')

    argument_parser.add_argument(
        '--min_separation',
        type=int,
        default=48,
        help='The minimum separation of iterations that a device can be '
        'sampled to participate again. This parameter is used in '
        '`BandedMatrixFactorizationMechanism`.')

    return argument_parser


def parse_mechanism(mechanism_name,
                    clipping_bound=None,
                    epsilon=None,
                    delta=None,
                    order=None,
                    cohort_size=None,
                    noise_cohort_size=None,
                    num_epochs=None,
                    population=None,
                    min_separation=None,
                    is_central=False):
    if mechanism_name == 'none':
        mechanism = NoPrivacy()

    elif mechanism_name == 'gaussian':
        assert clipping_bound is not None
        assert epsilon is not None and delta is not None
        mechanism = GaussianMechanism.construct_single_iteration(
            clipping_bound, epsilon, delta)

    elif mechanism_name == 'privunit':
        assert clipping_bound is not None
        assert epsilon is not None
        mechanism = PrivUnitMechanism(clipping_bound, epsilon)

    elif mechanism_name == 'gaussian_moments_accountant':
        assert clipping_bound is not None
        assert epsilon is not None
        assert delta is not None
        assert cohort_size is not None
        assert num_epochs is not None
        assert population is not None
        if noise_cohort_size is not None:
            noise_scale = cohort_size / noise_cohort_size
            max_cohort_size = max(cohort_size, noise_cohort_size)
        else:
            noise_scale = 1.0
            max_cohort_size = cohort_size
        mechanism = MomentsAccountantGaussianMechanism(
            clipping_bound=clipping_bound,
            epsilon=epsilon,
            delta=delta,
            num_iterations=num_epochs,
            max_cohort_size=max_cohort_size,
            population_size=population,
            noise_scale=noise_scale)

    elif mechanism_name == 'banded_matrix_factorization':
        assert clipping_bound is not None
        assert epsilon is not None
        assert delta is not None
        assert cohort_size is not None
        assert num_epochs is not None
        assert population is not None
        assert min_separation is not None
        if noise_cohort_size is not None:
            noise_scale = cohort_size / noise_cohort_size
            max_cohort_size = max(cohort_size, noise_cohort_size)
        else:
            noise_scale = 1.0
            max_cohort_size = cohort_size
        mechanism = BandedMatrixFactorizationMechanism(
            epsilon=epsilon,
            delta=delta,
            clipping_bound=clipping_bound,
            num_iterations=num_epochs,
            min_separation=min_separation,
            sampling_rate=max_cohort_size / population,
            noise_scale=noise_scale)

    elif mechanism_name == 'laplace':
        assert clipping_bound is not None
        assert epsilon is not None
        mechanism = LaplaceMechanism(clipping_bound, epsilon)

    elif mechanism_name == 'norm_clipping_only':
        assert clipping_bound is not None
        assert order is not None
        mechanism = NormClippingOnly(order, clipping_bound)

    else:
        assert False, "Please specify `mechanism_name`. If you don't want to \
        use any privacy, specify 'none'."

    if is_central:
        assert isinstance(mechanism, CentrallyApplicablePrivacyMechanism), (
            '`is_central=True` will wrap the mechanism into a central '
            f'mechanism, but {mechanism} is not centrally applicable')
        mechanism = CentrallyAppliedPrivacyMechanism(mechanism)

    return mechanism


def add_algorithm_arguments(
        parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """
    Add `algorithm_name` argument to parser and add
    algorithm-specific arguments depending on the algorithm
    specified in `algorithm_name` argument.
    """

    parser.add_argument(
        '--algorithm_name',
        choices=[
            'fedavg',
            'fedprox',
        ],
        default='fedavg',
        help='Which algorithm to train with.')

    # Get the value of `algorithm_name` argument and dynamically add
    # arguments depending on which algorithm is chosen.
    known_args, _ = parser.parse_known_args()

    if known_args.algorithm_name == 'fedavg':
        # No additional parameters.
        pass

    if known_args.algorithm_name == 'fedprox':
        parser.add_argument(
            "--mu",
            type=float,
            default=1.0,
            help='Scales the additional loss term added by FedProx.')

    return parser


def get_algorithm(args: argparse.Namespace):
    """
    Initialize the TensorFlow v2 model specified by ``args.model_name`` with
    other required arguments also available in ``args``.
    Use ``add_model_arguments`` to dynamically add arguments required by
    the selected model.
    """
    assert 'algorithm_name' in vars(args)
    algorithm_name = args.algorithm_name.lower()
    logger.info(f'initializing algorithm {algorithm_name}')

    if algorithm_name == 'fedavg':
        algorithm_params = NNAlgorithmParams(
            central_num_iterations=args.central_num_iterations,
            evaluation_frequency=args.evaluation_frequency,
            train_cohort_size=args.cohort_size,
            val_cohort_size=args.val_cohort_size)
        algorithm = FederatedAveraging()
    elif algorithm_name == 'fedprox':
        algorithm_params = FedProxParams(
            central_num_iterations=args.central_num_iterations,
            evaluation_frequency=args.evaluation_frequency,
            train_cohort_size=args.cohort_size,
            val_cohort_size=args.val_cohort_size,
            mu=args.mu)
        algorithm = FedProx()
    else:
        raise TypeError(f'Algorithm {algorithm_name} not found.')

    return algorithm, algorithm_params

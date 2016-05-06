# Copyright (c) 2016. Mount Sinai School of Medicine
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

"""
Allele specific MHC Class I binding affinity predictor
"""
from __future__ import (
    print_function,
    division,
    absolute_import,
)

from os import listdir
from os.path import exists, join

import numpy as np

from .common import normalize_allele_name
from .paths import CLASS1_MODEL_DIRECTORY
from .feedforward import make_embedding_network
from .class1_allele_specific_kmer_ic50_predictor_base import (
    Class1AlleleSpecificKmerIC50PredictorBase,
)
from .serialization_helpers import (
    load_keras_model_from_disk,
    save_keras_model_to_disk
)
from .peptide_encoding import check_valid_index_encoding_array
from .feedforward_hyperparameters import LOSS, OPTIMIZER
from .regression_target import MAX_IC50
from .training_helpers import (
    combine_training_arrays,
    extend_with_negative_random_samples,
)

_allele_predictor_cache = {}

class Class1BindingPredictor(Class1AlleleSpecificKmerIC50PredictorBase):
    """
    Allele-specific Class I MHC binding predictor which uses
    fixed-length (k-mer) index encoding for inputs and outputs
    a value between 0 and 1 (where 1 is the strongest binder).
    """
    def __init__(
            self,
            model,
            name=None,
            max_ic50=MAX_IC50,
            allow_unknown_amino_acids=True,
            verbose=False,
            kmer_size=9):
        Class1AlleleSpecificKmerIC50PredictorBase.__init__(
            self,
            name=name,
            max_ic50=max_ic50,
            allow_unknown_amino_acids=allow_unknown_amino_acids,
            verbose=verbose,
            kmer_size=kmer_size)
        self.name = name
        self.model = model

    @classmethod
    def from_disk(
            cls,
            model_json_path,
            weights_hdf_path=None,
            name=None,
            optimizer=OPTIMIZER,
            loss=LOSS,
            **kwargs):
        """
        Load model from stored JSON representation of network and
        (optionally) load weights from HDF5 file.
        """
        model = load_keras_model_from_disk(
            model_json_path,
            weights_hdf_path,
            name=name)
        # In some cases I haven't been able to use a model after loading it
        # without compiling it first.
        model.compile(optimizer=optimizer, loss=loss)
        return cls(model=model, **kwargs)

    def to_disk(self, model_json_path, weights_hdf_path, overwrite=False):
        save_keras_model_to_disk(
            self.model,
            model_json_path,
            weights_hdf_path,
            overwrite=overwrite)

    @classmethod
    def from_hyperparameters(
            cls,
            name=None,
            max_ic50=MAX_IC50,
            peptide_length=9,
            n_amino_acids=20,
            allow_unknown_amino_acids=True,
            embedding_output_dim=20,
            layer_sizes=[50],
            activation="tanh",
            init="lecun_uniform",
            output_activation="sigmoid",
            dropout_probability=0,
            loss=LOSS,
            optimizer=OPTIMIZER,
            **kwargs):
        """
        Create untrained predictor with the given hyperparameters.
        """
        model = make_embedding_network(
            peptide_length=peptide_length,
            n_amino_acids=n_amino_acids + int(allow_unknown_amino_acids),
            embedding_output_dim=embedding_output_dim,
            layer_sizes=layer_sizes,
            activation=activation,
            init=init,
            loss=loss,
            optimizer=optimizer,
            output_activation=output_activation,
            dropout_probability=dropout_probability)
        return cls(
            name=name,
            max_ic50=max_ic50,
            model=model,
            allow_unknown_amino_acids=allow_unknown_amino_acids,
            kmer_size=peptide_length,
            **kwargs)

    def fit_kmer_encoded_arrays(
            self,
            X,
            Y,
            sample_weights=None,
            X_pretrain=None,
            Y_pretrain=None,
            sample_weights_pretrain=None,
            n_random_negative_samples=0,
            pretrain_decay=lambda epoch: np.exp(-epoch),
            n_training_epochs=200,
            verbose=False,
            batch_size=128):
        """
        Train predictive model from index encoding of fixed length k-mer
        peptides.

        Parameters
        ----------
        X : array
            Training data with shape (n_samples, n_dims)

        Y : array
            Training labels with shape (n_samples,)

        sample_weights : array
            Weight of each training sample with shape (n_samples,)

        X_pretrain : array
            Extra samples used for soft pretraining of the predictor,
            should have same number of dimensions as X. During training the weights
            of these samples will decay after each epoch.

        Y_pretrain : array
            Labels for extra samples, shape

        pretrain_decay : int -> float function
            decay function for pretraining, mapping epoch number to decay
            factor

        sample_weights_pretrain : array
            Initial weights for the rows of X_pretrain. If not specified then
            initialized to ones.

        n_random_negative_samples : int
            Number of random samples to generate as negative examples.

        n_training_epochs : int

        verbose : bool

        batch_size : int
        """
        X_combined, Y_combined, combined_weights, n_pretrain = \
            combine_training_arrays(
                X, Y, sample_weights,
                X_pretrain, Y_pretrain, sample_weights_pretrain)

        total_pretrain_sample_weight = combined_weights[:n_pretrain].sum()
        total_train_sample_weight = combined_weights[n_pretrain:].sum()
        total_combined_sample_weight = (
            total_pretrain_sample_weight + total_train_sample_weight)

        if self.verbose:
            print("-- Total pretrain weight = %f (%f%%), sample weight = %f (%f%%)" % (
                total_pretrain_sample_weight,
                100 * total_pretrain_sample_weight / total_combined_sample_weight,
                total_train_sample_weight,
                100 * total_train_sample_weight / total_combined_sample_weight))

        for epoch in range(n_training_epochs):
            decay_factor = pretrain_decay(epoch)

            # if the contribution of synthetic samples is less than a
            # thousandth of the actual data, then stop using it
            pretrain_contribution = total_pretrain_sample_weight * decay_factor
            pretrain_fraction_contribution = (
                pretrain_contribution / total_combined_sample_weight)

            use_pretrain_data = pretrain_fraction_contribution > 0.001

            # only use synthetic data if it contributes at least 1/1000th of
            # sample weight
            if verbose:
                real_data_percent = (
                    ((1.0 - pretrain_fraction_contribution) * 100)
                    if use_pretrain_data
                    else 100
                )
                pretrain_data_percent = (
                    (pretrain_fraction_contribution * 100)
                    if use_pretrain_data else 0
                )
                print(
                    ("-- Epoch %d/%d decay=%f, real data weight=%0.2f%%,"
                     " synth data weight=%0.2f%% (use_pretrain=%s)") % (
                        epoch + 1,
                        n_training_epochs,
                        decay_factor,
                        real_data_percent,
                        pretrain_data_percent,
                        use_pretrain_data))

            if use_pretrain_data:
                combined_weights[:n_pretrain] *= decay_factor
                X_curr_iter = X_combined
                Y_curr_iter = Y_combined
                weights_curr_iter = combined_weights
            else:
                X_curr_iter = X_combined[n_pretrain:]
                Y_curr_iter = Y_combined[n_pretrain:]
                weights_curr_iter = combined_weights[n_pretrain:]

            X_curr_iter, Y_curr_iter, weights_curr_iter = \
                extend_with_negative_random_samples(
                    X_curr_iter,
                    Y_curr_iter,
                    weights_curr_iter,
                    n_random_negative_samples,
                    max_amino_acid_encoding_value=self.max_amino_acid_encoding_value)

            self.model.fit(
                X_curr_iter,
                Y_curr_iter,
                sample_weight=weights_curr_iter,
                nb_epoch=1,
                verbose=0,
                batch_size=batch_size,
                shuffle=True)

    @classmethod
    def from_allele_name(
            cls,
            allele_name,
            model_directory=CLASS1_MODEL_DIRECTORY,
            max_ic50=MAX_IC50):
        if not exists(model_directory) or len(listdir(model_directory)) == 0:
            raise ValueError(
                "No MHC prediction models found in %s" % (model_directory,))

        allele_name = normalize_allele_name(allele_name)
        key = (allele_name, model_directory, max_ic50)
        if key in _allele_predictor_cache:
            return _allele_predictor_cache[key]

        if not exists(model_directory) or len(listdir(model_directory)) == 0:
            raise ValueError(
                "No MHC prediction models found in %s" % (model_directory,))

        model_json_filename = allele_name + ".json"
        model_json_path = join(model_directory, model_json_filename)

        weights_hdf_filename = allele_name + ".hdf"
        weights_hdf_path = join(model_directory, weights_hdf_filename)

        predictor = cls.from_disk(
            model_json_path=model_json_path,
            weights_hdf_path=weights_hdf_path,
            name=allele_name,
            max_ic50=max_ic50)

        _allele_predictor_cache[key] = predictor
        return predictor

    @classmethod
    def supported_alleles(cls, model_directory=CLASS1_MODEL_DIRECTORY):
        alleles_with_weights = set([])
        alleles_with_models = set([])
        for filename in listdir(model_directory):
            if filename.endswith(".hdf"):
                alleles_with_weights.add(filename.replace(".hdf", ""))
            elif filename.endswith(".json"):
                alleles_with_models.add(filename.replace(".json", ""))
        alleles = alleles_with_models.intersection(alleles_with_weights)
        return list(sorted(alleles))

    def predict_scores_for_kmer_array(self, X):
        """
        Given an encoded array of amino acid indices, returns a vector
        of predicted log IC50 values.
        """
        X = check_valid_index_encoding_array(
            X,
            allow_unknown_amino_acids=self.allow_unknown_amino_acids)
        return self.model.predict(X, verbose=False).flatten()

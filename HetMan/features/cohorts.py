
"""Consolidating -omics datasets.

This module contains classes for grouping expression datasets with other
-omic features such as variants, copy number alterations, and drug response
data so that the former can be used to predict the latter using machine
learning pipelines.

Author: Michal Grzadkowski <grzadkow@ohsu.edu>

"""

from .expression import get_expr_bmeg
from .variants import get_variants_mc3, MuTree
from .copies import get_copies_firehose
from .pathways import parse_sif
from .annot import get_gencode
from .drugs import get_expr_ioria, get_drug_ioria, get_drug_bmeg

import numpy as np
import pandas as pd

from scipy.stats import fisher_exact
import random
from functools import reduce


class Cohort(object):
    """A matched pair of expression and feature datasets for use in learning.

    Attributes:
        cohort (str): The source of the datasets.
        cv_seed (int): A seed used for random sampling from the datasets.

    """

    def __init__(self, cohort, cv_seed):
        self.cohort = cohort
        self.cv_seed = cv_seed

    def _validate_dims(self,
                       mtype=None, include_samps=None, exclude_samps=None,
                       gene_list=None, use_test=False):
        if include_samps is not None and exclude_samps is not None:
            raise ValueError("Cannot specify samples to be included and"
                             "samples to be excluded at the same time!")

        # get samples and genes from the specified cohort as specified
        if use_test:
            samps = self.test_samps_.copy()
            genes = set(self.test_expr_.columns)
        else:
            samps = self.train_samps_.copy()
            genes = set(self.train_expr_.columns)

        # remove samples and/or genes as necessary
        if include_samps is not None:
            samps &= set(include_samps)
        elif exclude_samps is not None:
            samps -= set(exclude_samps)
        if gene_list is not None:
            genes &= set(gene_list)

        # if a mutation type is specified include samples with that mutation
        if mtype is not None:
            samps |= mtype.get_samples(self.train_mut_)

        return samps, genes


class VariantCohort(Cohort):
    """An expression dataset used to predict genes' variant mutations.

    Args:
        syn (synapseclient.Synapse): A logged-into Synapse instance.
        mut_genes (list of str): Which genes' variants to include.
        mut_levels (list of str): What variant annotation level to consider.
        cv_prop (float): Proportion of samples to use for cross-validation.

    Attributes:
        train_expr (pandas DataFrame of floats)
        test_expr (pandas DataFrame of floats)
        train_mut (MuTree)
        test_mut (MuTree)
        path (dict)

    Examples:
        >>> syn = synapseclient.Synapse()
        >>> syn.login()
        >>> cdata = VariantCohort(
        >>>     syn, cohort='TCGA-BRCA', mut_genes=['TP53', 'PIK3CA'],
        >>>     mut_levels=['Gene', 'Form', 'Exon']
        >>>     )

    """

    def __init__(self,
                 syn, cohort, mut_genes, mut_levels=('Gene', 'Form'),
                 cv_seed=None, cv_prop=2.0/3):
        # TODO: double-check how Python handles random seeds
        if cv_prop <= 0 or cv_prop > 1:
            raise ValueError("Improper cross-validation ratio that is "
                             "not > 0 and <= 1.0")
        self.mut_genes = mut_genes
        self.cv_prop = cv_prop

        # loads gene expression and mutation data
        expr = get_expr_bmeg(cohort)
        variants = get_variants_mc3(syn)

        # loads the pathway neighbourhood of the variant genes, as well as
        # annotation data for all genes
        self.path = parse_sif(mut_genes)
        annot = get_gencode()

        # filters out genes that don't have any variation across the samples
        # or are not included in the annotation data
        expr = expr.loc[:, expr.apply(lambda x: np.var(x) > 0.005)].dropna()
        annot = {g: a for g,a in annot.items()
                 if a['gene_name'] in expr.columns}
        annot_genes = [a['gene_name'] for g, a in annot.items()]
        expr = expr.loc[:, annot_genes]

        # gets set of samples shared across expression and mutation datasets,
        # subsets these datasets to use only these samples
        self.samples = frozenset(variants['Sample']) & frozenset(expr.index)
        expr = expr.loc[self.samples, :]
        variants = variants.loc[variants['Gene'].isin(mut_genes), :]

        # gets annotation data for the genes whose mutations
        # are under consideration
        annot_data = {a['gene_name']: {'ID': g, 'Chr': a['chr'],
                                       'Start': a['Start'], 'End': a['End']}
                      for g, a in annot.items()
                      if a['gene_name'] in mut_genes}
        self.annot = annot
        self.mut_annot = annot_data

        # gets subset of samples to use for training, and split the expression
        # and variant datasets accordingly into training/testing cohorts
        random.seed(a=cv_seed)
        if cv_prop < 1:
            self.train_samps_ = frozenset(
                random.sample(population=self.samples,
                              k=int(round(len(self.samples) * cv_prop)))
                )
            self.test_samps_ = self.samples - self.train_samps_

            self.test_expr_ = expr.loc[self.test_samps_, :]
            self.test_mut_ = MuTree(
                muts=variants.loc[
                     variants['Sample'].isin(self.test_samps_), :],
                levels=mut_levels)

        else:
            self.train_samps_ = self.samples
            self.test_samps_ = None

        self.train_expr_ = expr.loc[self.train_samps_, :]
        self.train_mut_ = MuTree(
            muts=variants.loc[variants['Sample'].isin(self.train_samps_), :],
            levels=mut_levels)

        super(VariantCohort, self).__init__(cohort, cv_seed)

    def train_status(self, mtype):
        return self.train_mut_.status(self.train_expr_.index, mtype)

    def test_status(self, mtype):
        return self.test_mut_.status(self.test_expr_.index, mtype)

    def mutex_test(self, mtype1, mtype2):
        """Tests the mutual exclusivity of two mutation types.

        Args:
            mtype1, mtype2 (MuType)

        Returns:
            pval (float): The p-value given by a Fisher's one-sided exact test
                          using the training samples in the cohort.

        Examples:
            >>> self.mutex_test(MuType({('Gene', 'TP53'): None}),
            >>>                 MuType({('Gene', 'CDH1'): None}))
            >>> self.mutex_test(MuType({('Gene', 'PIK3CA'): None}),
            >>>                 MuType({('Gene', 'BRAF'): {
            >>>                             ('Location', '600'): None
            >>>                        }}))

        """
        samps1 = mtype1.get_samples(self.train_mut_)
        samps2 = mtype2.get_samples(self.train_mut_)

        if not samps1 or not samps2:
            pval = 1

        else:
            all_samps = set(self.train_expr_.index)
            both_samps = samps1 & samps2

            _, pval = fisher_exact(
                np.array([[len(all_samps - (samps1 | samps2)),
                           len(samps1 - both_samps)],
                          [len(samps2 - both_samps),
                           len(both_samps)]]),
                alternative='less')

        return pval


class MutCohort(VariantCohort):
    """An expression dataset used to predict mutations, including CNAs.

    A MutCohort is constructed by first constructing a VariantCohort with the
    same attributes, and then adding copy number alteration (CNA) data on top
    of the variant mutation data.

    Note that CNAs are split according to the 'Form' mutation level, with each
    branch at this level corresponding to a type of CNA, eg. -2 for homozygous
    loss, 1 for heterozygous amplification, etc. If further mutation levels
    specified they will only be added to the branches of the mutation trees
    corresponding to variants.

    Examples:
        >>> syn = synapseclient.Synapse()
        >>> syn.login()
        >>> cdata = MutCohort(
        >>>     syn, cohort='TCGA-OV', mut_genes=['RB1', 'TTN'],
        >>>     mut_levels=['Gene', 'Form', 'Protein']
        >>>     )

    """

    def __init__(self,
                 syn, cohort, mut_genes, mut_levels=('Gene', 'Type'),
                 cv_seed=None, cv_prop=2.0 / 3):
        if mut_levels[0] != 'Gene' or mut_levels[1] != 'Form':
            raise ValueError("A cohort with CNA info must use 'Gene' as the"
                             "first mutation level and 'Form' as the second!")

        # initiates a cohort with expression and variant mutation data
        super(MutCohort, self).__init__(syn, cohort, mut_genes, mut_levels,
                                        cv_seed, cv_prop)

        # loads copy number data, gets list of samples with CNA info
        copy_data = get_copies_firehose(cohort.split('-')[-1], mut_genes)
        copy_samps = frozenset(
            reduce(lambda x, y: x & y,
                   set(tuple(copies.keys())
                       for gn, copies in copy_data.items()))
            )

        # removes samples that don't have CNA info
        self.samples = self.samples & copy_samps
        self.train_samps_ = self.train_samps_ & copy_samps
        self.test_samps_ = self.test_samps_ & copy_samps

        # removes expression data for samples with no CNA info
        self.train_expr_ = self.train_expr_.loc[self.train_samps_, :]
        self.test_expr_ = self.test_expr_.loc[self.test_samps_, :]

        # removes variant data for samples with no CNA info
        self.train_mut_ = self.train_mut_.subtree(self.train_samps_)
        self.test_mut_ = self.test_mut_.subtree(self.test_samps_)

        # adds copy number alteration data to the mutation trees
        for gn in mut_genes:
            copy_vals = list(np.unique(list(copy_data[gn].values())))
            copy_vals.remove(0)
            val_labels = ['CNA_{}'.format(val) for val in copy_vals]

            if gn not in self.train_mut_._child:
                self.train_mut_._child[gn] = MuTree(
                    muts=pd.DataFrame(
                        {'Form': val_labels,
                         'Sample': [None for _ in val_labels]}
                        ),
                    levels=['Form'])

            if gn not in self.test_mut_._child:
                self.test_mut_._child[gn] = MuTree(
                    muts=pd.DataFrame(
                        {'Form': val_labels,
                         'Sample': [None for _ in val_labels]}
                        ),
                    levels=['Form'])

            for val_lbl in val_labels:
                self.train_mut_[gn]._child[val_lbl] = set()
                self.test_mut_[gn]._child[val_lbl] = set()

            for samp, val in copy_data[gn].items():
                if val != 0:
                    lbl_indx = copy_vals.index(val)

                    if samp in self.train_samps_:
                        self.train_mut_[gn]._child[val_labels[lbl_indx]].\
                            update({samp})
                    else:
                        self.test_mut_[gn]._child[val_labels[lbl_indx]].\
                            update({samp})

            for val_lbl in val_labels:
                if self.train_mut_[gn]._child[val_lbl]:
                    self.train_mut_[gn]._child[val_lbl] = frozenset(
                        self.train_mut_[gn]._child[val_lbl])
                else:
                    del(self.train_mut_[gn]._child[val_lbl])

                if self.test_mut_[gn]._child[val_lbl]:
                    self.test_mut_[gn]._child[val_lbl] = frozenset(
                        self.test_mut_[gn]._child[val_lbl])
                else:
                    del(self.test_mut_[gn]._child[val_lbl])


class DrugCohort(Cohort):
    """An expression dataset used to predict clinical drug response.

        Args:
            drug_list (list of str): Which drugs to include
            cv_prop (float): Proportion of samples to use for cross-validation

        Attributes:
            train_samps_(frozenset of str)
            test_samps_ (frozenset of str)
            train_expr_ (pandas DataFrame of floats)
            test_expr_ (pandas DataFrame of floats)
            train_resp_ (pandas DataFrame of floats)
            test_resp_ (pandas DataFrame of floats)

        Examples:

        """

    def __init__(self, cohort, drug_list, cv_seed=None, cv_prop=2.0/3):
        if cv_prop <= 0 or cv_prop > 1:
            raise ValueError("Improper cross-validation ratio that is "
                             "not > 0 and <= 1.0")
        self.drug_list = drug_list
        self.cv_prop = cv_prop

        cell_expr = get_expr_ioria()

        # TODO: choose a non-AUC measure of drug response
        drug_resp = get_drug_ioria(drug_list)

        # drops cell lines (rows) w/ no expression data & genes (cols) with any missing values
        cell_expr = cell_expr.dropna(axis=0, how='all').dropna(axis=1, how='any')

        # drops cell lines (rows) w/ no expression data
        drug_resp = drug_resp.dropna(axis=0, how='all')

        # gets set of cell lines ("samples") shared between drug_resp and cell_expr datasets
        self.samples = set(cell_expr.index) & set(drug_resp.index)

        # discards data for cell lines which are not in samples set
        cell_expr = cell_expr.loc[self.samples,:]
        drug_resp = drug_resp.loc[self.samples,:]

        # TODO: query bmeg for annotation data on each drug (def in drugs.py), set as attribute

        random.seed(a=cv_seed)
        if cv_prop < 1:

            # separate samples (cell line names) into train and test frozensets.
            self.train_samps_ = frozenset(
                random.sample(population=self.samples, k=int(round(len(self.samples) * cv_prop))))
            self.test_samps_ = self.samples - self.train_samps_

            # bifurcate cell_expr and drug_resp based on those sets.
            self.train_expr_ = cell_expr.loc[self.train_samps_, :]
            self.test_expr_ = cell_expr.loc[self.test_samps_, :]

            self.train_resp_ = drug_resp.loc[self.train_samps_, :]
            self.test_resp_ = drug_resp.loc[self.test_samps_, :]

        else:
            self.train_samps = self.samples
            self.test_samps_ = None

        super(DrugCohort, self).__init__(cohort, cv_seed)

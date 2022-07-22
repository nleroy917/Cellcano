import os
import math
import anndata
import numpy as np
import pandas as pd
import scanpy as sc
import scipy
import random
import tensorflow as tf

import matplotlib.pyplot as plt
plt.rcParams.update({'font.size': 18})

from sklearn.preprocessing import OneHotEncoder

from typing import TypeVar
A = TypeVar('anndata')  ## generic for anndata
ENC = TypeVar('OneHotEncoder')

from Cellcano.models.distiller import Distiller
from Cellcano.models.MLP import MLP

## get logger
#import logging
#logger = logging.getLogger(__name__)

RANDOM_SEED = 1993
random.seed(RANDOM_SEED)
np.random.seed(RANDOM_SEED)

MLP_DIMS = Teacher_DIMS = Student_DIMS = [64, 16]
BATCH_SIZE = 32
Celltype_COLUMN = "celltype"
PredCelltype_COLUMN = "pred_celltype"
ENTROPY_QUANTILE = 0.4  ## how many cells are used as second-round target

GPU_list = tf.config.list_physical_devices('GPU')
print("Num GPUs Available: %d" % len(GPU_list))
if len(GPU_list) == 0:
    os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
else:
    os.environ['CUDA_VISIBLE_DEVICES'] = '0'


def _COOmtx_data_loader(mtx_prefix: str) -> A:
    '''
    Load gene score matrix in COO format
    ---
    Input:
        - mtx_prefix: gene score matrix in COO format
    ---
    Output:
        - an anndata object
    '''
    adata = anndata.read_mtx(mtx_prefix+'.mtx.gz').T
    genes = pd.read_csv(mtx_prefix+'_genes.tsv', header=None, sep='\t')
    adata.var["genes"] = genes[0].values
    adata.var_names = adata.var["genes"]
    adata.var_names_make_unique(join="-")
    adata.var.index.name = None
    cells = pd.read_csv(mtx_prefix+'_barcodes.tsv', header=None, sep='\t')
    adata.obs["barcode"] = cells[0].values
    adata.obs_names = adata.obs['barcode']
    adata.obs_names_make_unique(join="-")
    adata.obs.index.name = None

    adata = adata[:, adata.var_names.notnull()]
    adata.var_names=[i.upper() for i in list(adata.var_names)]
    return adata

def _csv_data_loader(csv_input: str) -> A:
    '''
    Load gene score matrix in csv format
    ---
    Input:
        - csv_input: gene scre matrix in dense format with row as genes and cells as columns.
    ---
    Output:
        - an anndata object
    '''
    df = pd.read_csv(csv_input, index_col=0)
    obs = pd.DataFrame(data=df.columns, index=df.columns)
    obs.columns = ["barcode"]
    var = pd.DataFrame(data=df.index, index=df.index)
    var.columns = ['gene_symbols']
    adata = anndata.AnnData(X=df.T, obs=obs, var=var)
    adata.obs_names_make_unique(join="-")
    adata.var_names_make_unique(join="-")

    adata = adata[:, adata.var_names.notnull()]
    adata.var_names=[i.upper() for i in list(adata.var_names)]
    return adata

def _metadata_loader(metadata):
    '''Load metadata
    '''
    metadata = pd.read_csv(metadata, index_col=0, sep=',')
    return metadata


def _process_adata(adata, process_type='train', celltype_label='celltype'):
    '''Procedures for filtering single-cell gene scale data (can be gene expression, or gene scores)
       1. Filter nonsense genes;
       2. Normalize and log-transform the data;
       3. Remove cells with no labels; 
    '''
    adata = adata[:, adata.var_names.notnull()]  ## remove NA var_names, some genes generated by ArchR gene scores will be NA
    adata.var_names=[i.upper() for i in list(adata.var_names)] #avoid some genes having lower letter

    ## make names unique after removing
    adata.var_names_make_unique()
    adata.obs_names_make_unique()

    #prefilter_specialgene: MT and ERCC  -> refered from ItClust package
    Gene1Pattern="ERCC"
    Gene2Pattern="MT-"
    id_tmp1=np.asarray([not str(name).startswith(Gene1Pattern) for name in adata.var_names],dtype=bool)
    id_tmp2=np.asarray([not str(name).startswith(Gene2Pattern) for name in adata.var_names],dtype=bool)
    id_tmp=np.logical_and(id_tmp1,id_tmp2)
    adata._inplace_subset_var(id_tmp)

    ## handel exception when there are not enough cells or genes after filtering
    if adata.shape[0] < 3 or adata.shape[1] < 3:
        sys.exit("Error: too few genes or cells left to continue..")

    ## normalization,var.genes,log1p
    sc.pp.normalize_per_cell(adata, counts_per_cell_after=10000, min_counts=0)
    sc.pp.log1p(adata)

    ## cells with celltypes
    if process_type == 'train':
        cells = adata.obs.dropna(subset=[celltype_label]).index.tolist()
        adata = adata[cells]
    return adata

def _select_feature(adata: A, fs_method = "F-test", num_features: int = 3000) -> A:
    '''Select features
    ---
    Input:
        - anndata
        - fs_method: F-test / noFS / seurat
    '''
    ## Feature selection
    if fs_method == "noFS":
        print("Cellcano will not perform feature selection.\n")
        return adata
    else:
        if num_features > adata.shape[1]:
            print("Number of features is larger than data. Cellcano will not perform feature selection.\n")
            return adata

    if fs_method == "F-test":
        print("Use F-test to select features.\n")
        if scipy.sparse.issparse(adata.X) or \
                isinstance(adata.X, pd.DataFrame):
            tmp_data = adata.X.toarray()
        else:
            tmp_data = adata.X

        ## calculate F-test
        cell_annots = adata.obs[Celltype_COLUMN].tolist()
        uniq_celltypes = set(cell_annots)
        array_list = []
        for celltype in uniq_celltypes:
            idx = np.where(np.array(cell_annots) == celltype)[0].tolist()
            array_list.append(tmp_data[idx, :])
        F, p = scipy.stats.f_oneway(*array_list)
        F_updated = np.nan_to_num(F)
        sorted_idx = np.argsort(F_updated)[-num_features:]
        features = adata.var_names[sorted_idx].tolist()
        features.sort()
        adata = adata[:, features]

    if fs_method == "seurat":
        print("Use seurat in scanpy to select features.\n")
        sc.pp.highly_variable_genes(adata, n_top_genes=num_features, subset=True)
    return adata


def _scale_data(adata):
    '''Center scale
    '''
    adata_copy = sc.pp.scale(adata, zero_center=True, max_value=6, copy=True)
    return adata_copy

def _visualize_data(adata, output_dir, color_columns=["celltype"],
        reduction="tSNE", prefix="data"):
    '''Visualize data 

    ---
    Input:
        - reduction: tSNE or UMAP
        - color_columns: plot on categories
    '''
    sc.tl.pca(adata, random_state=RANDOM_SEED)

    if reduction == "tSNE":
        sc.tl.tsne(adata, use_rep="X_pca",
            learning_rate=300, perplexity=30, n_jobs=1, random_state=RANDOM_SEED)
        sc.pl.tsne(adata, color=color_columns)
        plt.tight_layout()
        plt.savefig(output_dir+os.sep+prefix+"tSNE_cluster.png")
    if reduction == "UMAP":
        sc.pp.neighbors(adata, n_neighbors=20, use_rep="X_pca", random_state=RANDOM_SEED) 
        sc.tl.umap(adata, random_state=RANDOM_SEED)
        sc.pl.umap(adata, color=color_columns)
        plt.tight_layout()
        plt.savefig(output_dir+os.sep+prefix+"umap_cluster.png")

def _save_adata(adata, output_dir, prefix=""):
    '''Save anndata as h5ad
    '''
    adata.write(output_dir+os.sep+prefix+'adata.h5ad')


def _prob_to_label(y_pred: np.ndarray, encoders: dict) -> list:
    '''Turn predicted probabilites to labels
    --- 
    Input:
        - y_pred: Predicted probabilities
        - encoders: dictionary with mapping information
    ---
    Output:
        - a list containing predicted cell types
    '''
    pred_labels = y_pred.argmax(1)
    pred_celltypes = [encoders[label] for label in pred_labels]
    print("=== Predicted celltypes: ", set(pred_celltypes))
    return pred_celltypes

def _label_to_onehot(labels: list, encoders:dict) -> np.ndarray:
    '''Turn predicted labels to onehot encoder
    ---
    Input: 
        - labels: the input predicted cell types
        - encoders: dictionary with mapping information
    '''
    inv_enc = {v: k for k, v in encoders.items()}
    onehot_arr = np.zeros((len(labels), len(encoders)))
    pred_idx = [inv_enc[l] for l in labels]
    onehot_arr[np.arange(len(labels)), pred_idx] = 1
    return onehot_arr


def _extract_adata(adata: A) -> np.ndarray:
    '''Extract adata.X to a numpy array
    ---
    Output:
         - matrix in np.ndarray format
    '''
    if scipy.sparse.issparse(adata.X) or isinstance(adata.X, pd.DataFrame) or isinstance(adata.X, anndata._core.views.ArrayView):
        X = adata.X.toarray()
    else:
        X = adata.X
    return X

def _init_MLP(x_train, y_train, dims=[64, 16], seed=0):
    '''Initialize MLP model based on input data
    '''
    mlp = MLP(dims)
    mlp.input_shape = (x_train.shape[1], )
    #mlp.n_classes = len(set(y_train.argmax(1)))
    mlp.n_classes = y_train.shape[1]
    mlp.random_state = seed
    mlp.init_MLP_model()  ## init the model
    return mlp

def _select_confident_cells(adata, celltype_col):
    '''Select low entropy cells from each predicted cell type
    ---
    Input:
        - adata: anndata object
        - celltype_col: the column indicator
    '''
    low_entropy_cells = []
    for celltype in set(adata.obs[celltype_col]):
        celltype_df = adata.obs[adata.obs[celltype_col] == celltype]
        entropy_cutoff = np.quantile(celltype_df['entropy'], q=ENTROPY_QUANTILE)
        ## change to < instead of <= to deal with ties
        cells = celltype_df.index[np.where(celltype_df['entropy'] <= entropy_cutoff)[0]].tolist()
        num_cells = math.ceil(ENTROPY_QUANTILE*celltype_df.shape[0])
        if len(cells) > num_cells:
            random.seed(RANDOM_SEED)
            selected_cells = random.sample(cells, num_cells)
        else:
            selected_cells = cells
        low_entropy_cells.extend(selected_cells)
    high_entropy_cells = list(set(adata.obs_names) - set(low_entropy_cells))
    adata.obs.loc[low_entropy_cells, 'entropy_status'] = "low"
    adata.obs.loc[high_entropy_cells, 'entropy_status'] = "high"
    return adata

def _run_distiller(x_train, y_train, student_model, teacher_model,
        epochs=30, alpha=0.1, temperature=3):
    '''Train KD model
    '''
    distiller = Distiller(student=student_model, teacher=teacher_model)
    distiller.compile(
        optimizer=tf.keras.optimizers.Adam(),
        metrics=["accuracy"],
        student_loss_fn=tf.keras.losses.CategoricalCrossentropy(from_logits=True),
        distillation_loss_fn=tf.keras.losses.KLDivergence(),
        alpha=alpha,
        temperature=temperature,
    )
    distiller.fit(x_train, y_train, epochs=epochs,
            validation_split=0.0, verbose=2)
    return distiller



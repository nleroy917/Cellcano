'''
Functions related to predict cell types
'''
import os, sys 
import logging

import tensorflow as tf
import numpy as np
from sklearn.preprocessing import OneHotEncoder

## import my package
from Pyramid.utils import _utils

## get the logger
logger = logging.getLogger(__name__)

def predict(args):
    model = tf.keras.models.load_model(ars.trained_model)
    feature_file = args.trained_model+os.sep+'features.txt'
    encoder_file = args.trained_model+os.sep+'onehot_encoder.txt'
    if not os.path.exists(feature_file) or not os.path.exists(encoder_file):
        sys.exit("Feature file or encoder mapping does not exist! Please check your tained model was trained successfully.")

    features = []
    with open(feature_file) as f:
        for line in f:
            features.append(line.strip())
    encoders = {}
    with open(encoder_file) as f:
        for line in f:
            line_info = line.strip().split(':')
            encoders[int(line_info[0])] = line_info[1]

    ## load input data
    logger.info("Loading data... \n This may take a while depending on your data size..")
    if '.csv' in inputfile:
        test_adata = _utils._csv_data_loader(args.input)
    else:
        test_adata = _utils._COOmtx_data_loader(args.input)
    ## process test adata
    test_adata = _utils._process_adata(test_adata, process_type='test')

    ## find paired data
    feature_idx = []
    find_cnt = 0
    for f_idx, feature in enumerate(features):
        find_flag = False
        for test_idx, gene in enumerate(test_adata.var_names):
            if gene == feature:
                feature_idx.append(test_idx)
                find_flag = True
                find_cnt += 1
                break
        if not find_flag:
            feature_idx.append(-1)

    if len(find_cnt) < 0.7*len(features):
        logger.warning("The common feature space between reference dataset and target dataset is too few with %d genes.\n This will result in inaccurate prediction." % len(common_features))
    else:
        logger.info("Common feature space between reference and target: %d genes" % len(common_features))

    test_adata = test_adata[:, feature_idx]
    logger.info("Data shape after processing: %d cells X %d genes"  % (test_adata.shape[0], test_adata.shape[1]))

    test_adata = _utils._scale_data(test_adata)
    test_data_mat = _utils._extract_adata(test_adata)

    y_pred = tf.nn.softmax(model.predict(test_data_mat)).numpy()
    pred_celltypes = _utils._prob_to_label(y_pred, encoders)
    test_adata.obs[_utils.PredCelltype_COLUMN] = pred_celltypes
    pred_celltypes = _utils._prob_to_label(y_pred, encoders)

    if args.predict_type == "direct_predict":
        test_adata.obs[PredCelltype_COLUMN] = pred_celltypes

    if args.predict_type == "tworound_predict":
        firstround_COLUMN = 'firstround_' + _utils.PredCelltype_COLUMN
        test_adata.obs[firstround_COLUMN] = pred_celltypes
        entropy = [-np.nansum(y_pred[i]*np.log(y_pred[i])) for i in range(y_pred.shape[0])]
        test_adata.obs['entropy'] = entropy
        test_adata = _utils._select_confident_cells(
                test_adata, celltype_col=firstround_COLUMN)

        low_entropy_cells = test_adata.obs_names[np.where(test_adata.obs['entropy_status'] == 'low')].tolist()
        high_entropy_cells = test_adata.obs_names[np.where(test_adata.obs['entropy_status'] == 'high')].tolist()
        test_ref_adata = test_adata[low_entropy_cells]
        test_tgt_adata = test_adata[high_entropy_cells]

        x_tgt_train = _utils._extract_adata(test_ref_adata)
        y_tgt_train = _utils._label_to_onehot(test_ref_adata.obs[firstround_COLUMN].tolist())
        x_tgt_test = _utils._extract_adata(test_tgt_adata)

        ## teahcer/studenmt model on original celltype label
        teacher = _utils._init_MLP(x_tgt_train, y_tgt_train, dims=_utils.Teacher_DIMS,
                seed=_utils.RANDOM_SEED)
        teacher.compile()
        teacher.fit(x_tgt_train, y_tgt_train, batch_size=_utils.BATCH_SIZE)
        ## student model -> actually same model, just used the concept of distillation
        student = _utils._init_MLP(x_tgt_train, y_tgt_train, dims=_utils.Student_DIMS, 
                seed=_utils.RANDOM_SEED)
        # Initialize and compile distiller
        distiller = _utils._run_distiller(x_tgt_train, y_tgt_train, 
                student_model=student.model,
                teacher_model=teacher.model)
        y_pred_tgt = tf.nn.softmax(distiller.student.predict(x_tgt_test)).numpy()

        pred_celltypes = _utils._prob_to_label(y_pred_tgt, encoders)
        test_adata.obs.loc[high_entropy_cells, _utils.PredCelltype_COLUMN] = pred_celltypes
    test_adata.obs.to_csv(args.output_dir+os.sep+args.prefix+'celltypes.csv')


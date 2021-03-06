import os
import time

from torch import nn

from arg_setting import args
from torch.utils.data import DataLoader

from cpm_GAN import CPMNet_Works_GAN
from data_loader import IEMOCAPDataset, IEMOCAPDatasetUtter, get_loaders, HDataset, MELDDataset, MELDDatasetUtter, \
    EmoryNlpDataset, EmoryNlpDatasetUtter
from cpm import CPMNet_Works
from dialogue import Dialogue_Works
from utils import get_sn, ave
import numpy as np
import torch
from sklearn.metrics import accuracy_score

os.environ['CUDA_VISIBLE_DEVICES'] = '0,1,2,3'

# torch.autograd.set_detect_anomaly(True)


if __name__ == "__main__":
    # import argparse
    #
    # parser = argparse.ArgumentParser()
    # parser.add_argument("--local_rank", type=int)
    # args_ = parser.parse_args()
    # torch.cuda.set_device(args.local_rank)
    data_name = args.data_name
    use_p = args.use_p
    epochs_ep = args.epochs_ep
    num_views = args.num_views
    data_path = args.data_path
    e_batch_size = args.e_batch_size
    p_batch_size = args.p_batch_size
    missing_rate = args.missing_rate
    steps_p = args.steps_p
    epochs_p = args.epochs_p
    # dimension of different modalities
    dim_features = args.dim_features
    # dimension of hidden representation from different modalities
    dim_init = sum(dim_features)
    dim_h = args.dim_h
    lr_p = args.lr_p
    lambda_p = args.lambda_p
    n_classes = args.n_classes
    device = torch.device(args.device)
    context_attention = args.context_attention
    lr_e = args.lr_e
    loss_weights = args.loss_weights
    model_type = args.model_type
    party_attention = args.party_attention

    dim_g = args.dim_g
    dim_p = args.dim_p
    dim_e = args.dim_e
    dim_y = args.dim_y
    dim_a = args.dim_a

    epochs_init = args.epochs_init

    rec_dropout = args.rec_dropout
    dropout = args.dropout
    epochs_e = args.epochs_e
    steps_e = args.steps_e
    party = args.party

    if data_name == 'IEMOCAP':
        train_set = IEMOCAPDataset(path=data_path, dims=dim_features + [dim_h], num_view=num_views)
        test_set = IEMOCAPDataset(path=data_path, dims=dim_features + [dim_h], num_view=num_views, train=False)
    elif data_name == 'MELD':
        train_set = MELDDataset(path=data_path, dims=dim_features + [dim_h], num_view=num_views)
        test_set = MELDDataset(path=data_path, dims=dim_features + [dim_h], num_view=num_views, train=False)
    elif data_name == 'EMORY':
        train_set = EmoryNlpDataset(path=data_path, dims=dim_features + [dim_h], num_view=num_views)
        test_set = EmoryNlpDataset(path=data_path, dims=dim_features + [dim_h], num_view=num_views, train=False)

    # Load dataset of emotion scenario (long video of dialogue)
    # train_set = IEMOCAPDataset(path=data_path, dims=dim_features + [dim_h], num_view=num_views)
    # test_set = IEMOCAPDataset(path=data_path, dims=dim_features + [dim_h], num_view=num_views, train=False)
    # video ids and utter lens of train/test sets
    train_keys_lens = train_set.get_keys_lens()
    test_keys_lens = test_set.get_keys_lens()

    # Load dataset of emotion clips (short video of utterance)
    if data_name == 'IEMOCAP':
        train_set_utter = IEMOCAPDatasetUtter(args.utterance_path, device)
    elif data_name == 'MELD':
        train_set_utter = MELDDatasetUtter(args.utterance_path, device)
    elif data_name == 'EMORY':
        train_set_utter = EmoryNlpDatasetUtter(args.utterance_path, device)
    train_data_utter = train_set_utter.get_data()
    train_gt_utter = train_set_utter.get_label()
    # test
    if data_name == 'IEMOCAP':
        test_set_utter = IEMOCAPDatasetUtter(args.utterance_path, device, train=False)
    elif data_name == 'MELD':
        test_set_utter = MELDDatasetUtter(args.utterance_path, device, train=False)
    elif data_name == 'EMORY':
        test_set_utter = EmoryNlpDatasetUtter(args.utterance_path, device, train=False)

    test_data_utter = test_set_utter.get_data()
    test_gt_utter = test_set_utter.get_label()

    # Randomly generated missing matrix
    len_train_utter = len(train_set_utter)
    len_test_utter = len(test_set_utter)

    Sn = get_sn(num_views, len_train_utter + len_test_utter, missing_rate)  # [num_samples, num_views]
    # Sn = np.concatenate([np.ones([len_train_utter + len_test_utter, 1]), Sn], axis=1)
    Sn_train = Sn[np.arange(len_train_utter)]
    Sn_test = Sn[np.arange(len_test_utter) + len_train_utter]

    Sn = torch.tensor(Sn, dtype=torch.long).cuda()
    Sn_train = torch.tensor(Sn_train, dtype=torch.long).cuda()
    Sn_test = torch.tensor(Sn_test, dtype=torch.long).cuda()

    # set Sn matrix to data set
    train_set.set_Sn(Sn_train)
    train_set_utter.set_Sn(Sn_train)
    test_set.set_Sn(Sn_test)
    test_set_utter.set_Sn(Sn_test)

    train_1hot = (torch.zeros((len_train_utter, n_classes)).cuda().scatter_(1, train_gt_utter, 1))

    # Model building
    model_p = CPMNet_Works(num_views + 1,  # number of view and context
                           len(train_set_utter),
                           len(test_set_utter),
                           dim_features + [2 * dim_e],
                           dim_h,
                           lr_p,
                           lambda_p,
                           p_batch_size).cuda()

    model_e = Dialogue_Works(model_type,
                             sum(dim_features) + dim_h, dim_g, dim_p, dim_e, dim_y, party,
                             n_classes=n_classes,
                             context_attention=context_attention,
                             party_attention=party_attention,
                             dropout_rec=rec_dropout,
                             dropout=dropout,
                             lr=lr_e,
                             loss_weights=loss_weights).cuda()

    data_set_e_train = train_set
    data_set_e_test = test_set

    for e_ep in range(epochs_ep):
        print("# =============== EP EPOCH {} =============== #".format(e_ep + 1))
        # train and test data loader
        data_loader_e_train = DataLoader(data_set_e_train,
                                         batch_size=e_batch_size,
                                         collate_fn=data_set_e_train.collate_fn,
                                         shuffle=False)

        data_loader_e_test = DataLoader(data_set_e_test,
                                        batch_size=e_batch_size,
                                        collate_fn=data_set_e_test.collate_fn,
                                        shuffle=False)

        # net parameter init
        model_e.init_model()

        # ep algorithm
        train_c, test_c = None, None
        for e_emo in range(args.epochs_e):
            st_time = time.time()
            # ----- train emotion model ----- #
            train_loss, train_acc, train_f1, train_c = model_e.train_test_model(data_loader_e_train,
                                                                                steps_e[0],
                                                                                train_keys_lens)
            # set context of emotion model to partial algorithm
            # train_set_utter.set_context(context)
            # ----- test emotion model ----- #
            test_loss, test_acc, test_f1, test_c = model_e.train_test_model(data_loader_e_test,
                                                                            steps_e[1],
                                                                            test_keys_lens,
                                                                            train=False)

            print('epoch {} train_loss {} train_acc {} train_f1 {} '
                  'test_loss {}  test_acc {} test_f1 {} time {}' \
                  .format(e_emo + 1, train_loss, train_acc, train_f1,
                          test_loss, test_acc, test_f1, round(time.time() - st_time, 2)))

        if use_p:
            train_set_utter.set_context(train_c)
            test_set_utter.set_context(test_c)
            # ----- train partial multi-view ----- #
            H_train = model_p.train_model(train_set_utter.get_data(),
                                          train_set_utter.get_Sn(),
                                          train_1hot,
                                          train_gt_utter,
                                          epochs_p[0],
                                          steps_p)
            # ----- test partial multi-view ----- #
            H_test = model_p.test_model(test_set_utter.get_data(),
                                        test_set_utter.get_Sn(),
                                        epochs_p[1])
            label_pre = ave(H_train, H_test, train_1hot)
            print('Accuracy on the test set is {:.4f}'.format(accuracy_score(test_gt_utter.cpu(), label_pre)))

            data_set_e_train.set_h(H_train)
            data_set_e_test.set_h(H_test)

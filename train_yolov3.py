from __future__ import division
from __future__ import print_function

from absl import app, flags, logging
from absl.flags import FLAGS
import os
import logging
import multiprocessing
import time
import warnings
import numpy as np
from tqdm import tqdm

from gluoncv import utils as gutils
from gluoncv.data.batchify import Tuple, Stack, Pad
# from gluoncv.data.transforms.presets.yolo import YOLO3DefaultTrainTransform, YOLO3DefaultValTransform
from gluoncv.data.dataloader import RandomTransformDataLoader
from gluoncv.model_zoo import get_model
from gluoncv.utils import LRScheduler, LRSequential
import mxnet as mx
from mxnet import gluon
from mxnet import autograd
from tensorboardX import SummaryWriter

from datasets.pascalvoc import VOCDetection
from datasets.mscoco import COCODetection
from datasets.imgnetdet import ImageNetDetection
from datasets.imgnetvid import ImageNetVidDetection
from datasets.combined import CombinedDetection

from metrics.pascalvoc import VOCMApMetric, VOCMApMetricTemporal
from metrics.mscoco import COCODetectionMetric

from models.definitions.yolo.wrappers import yolo3_darknet53, yolo3_no_backbone, yolo3_3ddarknet
from models.definitions.yolo.transforms import YOLO3DefaultTrainTransform, YOLO3DefaultInferenceTransform, \
    YOLO3VideoTrainTransform, YOLO3VideoInferenceTransform, YOLO3NBVideoTrainTransform, YOLO3NBVideoInferenceTransform

from utils.general import as_numpy

# disable autotune
os.environ['MXNET_CUDNN_AUTOTUNE_DEFAULT'] = '0'

logging.basicConfig(level=logging.INFO)

flags.DEFINE_string('network', 'darknet53',
                    'Base network name: darknet53')
flags.DEFINE_list('dataset', ['voc'],
                  'Datasets to train on.')
flags.DEFINE_list('dataset_val', [],
                  'Datasets to test on.')
flags.DEFINE_string('trained_on', '',
                    'Used for finetuning, specify the dataset the original model was trained on.')
flags.DEFINE_string('save_prefix', '0001',
                    'Model save prefix.')
flags.DEFINE_integer('log_interval', 100,
                     'Logging mini-batch interval.')
flags.DEFINE_integer('save_interval', -10,
                     'Saving parameters epoch interval, best model will always be saved. '
                     'Can enter a negative int to save every 1 epochs, but delete after reach -save_interval')
flags.DEFINE_integer('val_interval', 1,
                     'Epoch interval for validation.')
flags.DEFINE_string('resume', '',
                    'Resume from previously saved parameters if not None.')
flags.DEFINE_boolean('nd_only', False,
                     'Do not hybridize the model.')

flags.DEFINE_integer('batch_size', 64,
                     'Batch size for detection: higher faster, but more memory intensive.')
flags.DEFINE_integer('epochs', 200,
                     'How many training epochs to complete')
flags.DEFINE_integer('start_epoch', 0,
                     'Starting epoch for resuming, default is 0 for new training.'
                     'You can specify it to 100 for example to start from 100 epoch.'
                     'Set to -1 if using resume as a directory and resume from auto found latest epoch')
flags.DEFINE_integer('data_shape', 416,
                     'For evaluation, use 320, 416, 608... Training is with random shapes from (320 to 608).')
flags.DEFINE_float('lr', 0.001,
                   'Learning rate.')
flags.DEFINE_string('lr_mode', 'step',
                    'Learning rate scheduler mode. options are step, poly and cosine.')
flags.DEFINE_float('lr_decay', 0.1,
                   'Decay rate of learning rate.')
flags.DEFINE_integer('lr_decay_period', 0,
                     'Interval for periodic learning rate decays.')
flags.DEFINE_list('lr_decay_epoch', [160, 180],
                  'Epochs at which learning rate decays.')
# flags.DEFINE_float('warmup_lr', 0.0,  # not used
#                    'Starting warmup learning rate.')
flags.DEFINE_integer('warmup_epochs', 0,
                     'Number of warmup epochs.')
flags.DEFINE_float('momentum', 0.9,
                   'SGD momentum.')
flags.DEFINE_float('wd', 0.0005,
                   'Weight decay.')

flags.DEFINE_boolean('pretrained_cnn', True,
                     'Use an imagenet pretrained cnn as base network.')
flags.DEFINE_boolean('syncbn', False,
                     'Use synchronize BN across devices.')
flags.DEFINE_boolean('no_random_shape', False,
                     'Use fixed size(data-shape) throughout the training, which will be faster '
                     'and require less memory. However, final model will be slightly worse.')
flags.DEFINE_boolean('no_wd', False,
                     'Remove weight decay on bias, and beta/gamma for batchnorm layers.')
flags.DEFINE_boolean('mixup', False,
                     'Enable mixup?')
flags.DEFINE_integer('no_mixup_epochs', 20,
                     'Disable mixup training if enabled in the last N epochs.')
flags.DEFINE_boolean('label_smooth', False,
                     'Use label smoothing?')
flags.DEFINE_boolean('freeze_base', False,
                     'Freeze the base network?')
flags.DEFINE_boolean('allow_empty', True,
                     'Allow samples that contain 0 boxes as [-1s * 6]?')
flags.DEFINE_boolean('mult_out', False,
                     'Have one or multiple outs for timeseries data')
flags.DEFINE_boolean('temp', False,
                     'Use new temporal model')

flags.DEFINE_list('gpus', [0],
                  'GPU IDs to use. Use comma for multiple eg. 0,1.')
flags.DEFINE_integer('num_workers', -1,
                     'The number of workers should be picked so that it’s equal to number of cores on your machine '
                     'for max parallelization. If this number is bigger than your number of cores it will use up '
                     'a bunch of extra CPU memory. -1 is auto.')
flags.DEFINE_boolean('new_model', False,
                     'Use features Yolo (new) or stages Yolo (old)?')

flags.DEFINE_integer('num_samples', -1,
                     'Training images. Use -1 to automatically get the number.')
flags.DEFINE_float('every', 25,
                   'do every this many frames')
flags.DEFINE_list('window', [1, 1],
                  'Temporal window size of frames and the frame gap/stride of the windows samples')
flags.DEFINE_integer('seed', 233,
                     'Random seed to be fixed.')
flags.DEFINE_string('features_dir', None,
                    'If specified will use pre-saved DarkNet-53 features as input to YOLO backend, rather than images'
                    'into a full YOLO network. Useful for memory saving.')
flags.DEFINE_string('k_join_type', None,
                    'way to fuse k type, either max, mean, cat.')
flags.DEFINE_string('k_join_pos', None,
                    'position of k fuse, either early or late.')
flags.DEFINE_string('block_conv_type', '2',
                    "convolution type for the YOLO blocks: '2'2D, '3':3D or '21':2+1D, must be used with 'late' joining")
flags.DEFINE_string('rnn_pos', None,
                    "position of RNN, currently only supports 'late' or 'out")
flags.DEFINE_string('corr_pos', None,
                    "position of correlation features calculation, currently only supports 'early' or 'late")
flags.DEFINE_integer('corr_d', 0,
                     'The d value for the correlation filter.')
flags.DEFINE_string('motion_stream', None,
                    'Add a motion stream? can be flownet or r21d.')
flags.DEFINE_string('stream_gating', None,
                    'Use gating on the appearence stream using the motion stream. can be add or mul.')
flags.DEFINE_list('conv_types', [2, 2, 2, 2, 2, 2],
                  'Darknet Conv types for layers, either 2, 21, or 3 D')
flags.DEFINE_string('h_join_type', None,
                    'Type to join hierarchical darknet. can be max or conv.')
flags.DEFINE_list('hier', [1, 1, 1, 1, 1],
                  'the hierarchical factors, the input must be temporally equal to all these multiplied together')

flags.DEFINE_integer('max_epoch_time', -1,
                     'Max minutes an epoch can run for before we cut it off')


def get_dataset(dataset_name, dataset_val_name, save_prefix=''):
    train_datasets = list()
    val_datasets = list()

    if len(dataset_val_name) == 0:
        dataset_val_name = dataset_name

    # if dataset_name.lower() == 'voc':
    if 'voc' in dataset_name:
        train_datasets.append(VOCDetection(splits=[(2007, 'trainval'), (2012, 'trainval')],
                                           features_dir=FLAGS.features_dir))

    if 'voc' in dataset_val_name:
        val_datasets.append(VOCDetection(splits=[(2007, 'test')], features_dir=FLAGS.features_dir))
        val_metric = VOCMApMetric(iou_thresh=0.5, class_names=val_datasets[-1].classes)

    if 'coco' in dataset_name:
        train_datasets.append(COCODetection(splits=['instances_train2017'], use_crowd=False))

    if 'coco' in dataset_val_name:
        val_datasets.append(COCODetection(splits=['instances_val2017'], allow_empty=True))
        val_metric = COCODetectionMetric(val_datasets[-1], save_prefix + '_eval', cleanup=True,
                                         data_shape=(FLAGS.data_shape, FLAGS.data_shape))

    if 'det' in dataset_name:
        train_datasets.append(ImageNetDetection(splits=['train'], allow_empty=FLAGS.allow_empty))

    if 'det' in dataset_val_name:
        val_datasets.append(ImageNetDetection(splits=['val'], allow_empty=FLAGS.allow_empty))
        val_metric = VOCMApMetric(iou_thresh=0.5, class_names=val_datasets[-1].classes)

    if 'vid' in dataset_name:
        train_datasets.append(ImageNetVidDetection(splits=[(2017, 'train')], allow_empty=FLAGS.allow_empty,
                                             every=FLAGS.every, window=FLAGS.window, features_dir=FLAGS.features_dir,
                                             mult_out=FLAGS.mult_out))

    if 'vid' in dataset_val_name:
        val_datasets.append(ImageNetVidDetection(splits=[(2017, 'val')], allow_empty=FLAGS.allow_empty,
                                           every=FLAGS.every, window=FLAGS.window, features_dir=FLAGS.features_dir,
                                           mult_out=FLAGS.mult_out))
        if FLAGS.mult_out:
            val_metric = VOCMApMetricTemporal(t=int(FLAGS.window[0]), iou_thresh=0.5, class_names=val_datasets[-1].classes)
        else:
            val_metric = VOCMApMetric(iou_thresh=0.5, class_names=val_datasets[-1].classes)

    if len(train_datasets) == 0:
        raise NotImplementedError('Dataset: {} not implemented.'.format(dataset_name))
    elif len(train_datasets) == 1:
        train_dataset = train_datasets[0]
    else:
        train_dataset = CombinedDetection(train_datasets, class_tree=True)

    if len(val_datasets) == 0:
        raise NotImplementedError('Dataset: {} not implemented.'.format(dataset_name))
    elif len(val_datasets) == 1 and len(train_datasets) == 1:
            val_dataset = val_datasets[0]
    else:
        val_dataset = CombinedDetection(val_datasets, class_tree=True, validation=True)
        val_metric = VOCMApMetric(iou_thresh=0.5, class_names=val_dataset.classes)

    if FLAGS.mixup:
        from gluoncv.data import MixupDetection
        train_dataset = MixupDetection(train_dataset)

    return train_dataset, val_dataset, val_metric


def get_dataloader(net, train_dataset, val_dataset, batch_size):
    """Get dataloader."""
    width, height = FLAGS.data_shape, FLAGS.data_shape

    if FLAGS.features_dir is not None:  # the input is pre-saved features
        batchify_fn = Tuple(*([Stack() for _ in range(8)] + [Pad(axis=0, pad_val=-1) for _ in range(1)]))
        train_loader = gluon.data.DataLoader(
            train_dataset.transform(YOLO3NBVideoTrainTransform(FLAGS.window[0], width, height, net, mixup=FLAGS.mixup)),
            batch_size, True, batchify_fn=batchify_fn, last_batch='rollover', num_workers=FLAGS.num_workers)

        val_batchify_fn = Tuple(Stack(), Pad(pad_val=-1))
        val_batchify_fn = Tuple(*([Stack() for _ in range(3)] + [Pad(axis=0, pad_val=-1) for _ in range(1)]))
        val_loader = gluon.data.DataLoader(
            val_dataset.transform(YOLO3NBVideoInferenceTransform(width, height)),
            batch_size, False, batchify_fn=val_batchify_fn, last_batch='discard', num_workers=FLAGS.num_workers)

        return train_loader, val_loader

    # stack image, all targets generated
    if FLAGS.mult_out:
        batchify_fn = Tuple(*([Stack() for _ in range(6)] + [Pad(axis=1, pad_val=-1) for _ in range(1)]))  # pad the 1st dim
    else:
        batchify_fn = Tuple(*([Stack() for _ in range(6)] + [Pad(axis=0, pad_val=-1) for _ in range(1)]))

    if FLAGS.no_random_shape:
        train_loader = gluon.data.DataLoader(
            train_dataset.transform(YOLO3VideoTrainTransform(FLAGS.window[0], width, height, net, mixup=FLAGS.mixup)),
            # train_dataset.transform(YOLO3DefaultTrainTransform(width, height, net, mixup=FLAGS.mixup)),
            batch_size, True, batchify_fn=batchify_fn, last_batch='rollover', num_workers=FLAGS.num_workers)
    else:
        if FLAGS.motion_stream == 'flownet': # get shape errors for some of the rand shapes as the conv floor messes up on deconv
            transform_fns = [YOLO3VideoTrainTransform(FLAGS.window[0], x * 32, x * 32, net, mixup=FLAGS.mixup) for x in range(10, 20, 2)]
        else:
            transform_fns = [YOLO3VideoTrainTransform(FLAGS.window[0], x * 32, x * 32, net, mixup=FLAGS.mixup) for x in range(10, 20)]
        # transform_fns = [YOLO3DefaultTrainTransform(x * 32, x * 32, net, mixup=FLAGS.mixup) for x in range(10, 20)]
        train_loader = RandomTransformDataLoader(
            transform_fns, train_dataset, batch_size=batch_size, interval=10, last_batch='rollover',
            shuffle=True, batchify_fn=batchify_fn, num_workers=FLAGS.num_workers)

    if FLAGS.mult_out:
        val_batchify_fn = Tuple(Stack(), Pad(axis=1, pad_val=-1))
    else:
        val_batchify_fn = Tuple(Stack(), Pad(pad_val=-1))
    val_loader = gluon.data.DataLoader(
        val_dataset.transform(YOLO3VideoInferenceTransform(width, height)),
        # val_dataset.transform(YOLO3DefaultInferenceTransform(width, height)),
        batch_size, False, batchify_fn=val_batchify_fn, last_batch='discard', num_workers=FLAGS.num_workers)
    # NOTE for val batch loader last_batch='keep' changed to last_batch='discard' so exception not thrown
    # when last batch size is smaller than the number of GPUS (which throws exception) this is fixed in gluon
    # PR 14607: https://github.com/apache/incubator-mxnet/pull/14607 - but yet to be in official release
    # discarding last batch will incur minor changes in val results as some val data wont be processed

    return train_loader, val_loader


def save_params(net, best_map, current_map, epoch, save_interval, prefix):
    current_map = float(current_map)
    if current_map > best_map[0]:
        best_map[0] = current_map
        net.save_parameters('{:s}_best.params'.format(prefix, epoch, current_map))
        with open(prefix+'_best_map.log', 'a') as f:
            f.write('{:04d}:\t{:.4f}\n'.format(epoch, current_map))

    if save_interval > 0 and epoch % save_interval == 0:  # save only these epochs
        # net.save_parameters('{:s}_{:04d}_{:.4f}.params'.format(prefix, epoch, current_map))
        net.save_parameters('{:s}_{:04d}.params'.format(prefix, epoch))

    if save_interval < 0:  # save every epoch, but delete nonwanted when reach a desired interval...
        # good for if training stopped within intervals and dont want to waste space with save_interval = 1
        net.save_parameters('{:s}_{:04d}.params'.format(prefix, epoch))

        if epoch % -save_interval == 0:  # delete the ones we don't want
            st = epoch + save_interval + 1
            for d in range(max(0, st), epoch):
                if os.path.exists('{:s}_{:04d}.params'.format(prefix, d)):
                    os.remove('{:s}_{:04d}.params'.format(prefix, d))


def resume(net, async_net, resume, start_epoch):
    """Resume model, can find the latest automatically"""
    # Requires the first digit of epoch in save string is a 0, otherwise may need to reimplement with .split()
    if start_epoch == -1:
        files = os.listdir(resume.strip())
        files = [file for file in files if '_0' in file]
        files = [file for file in files if '.params' in file]
        files.sort()
        resume_file = files[-1]
        start_epoch = int(resume_file[:-7].split('_')[-1]) + 1

        net.load_parameters(os.path.join(resume.strip(), resume_file))
        async_net.load_parameters(os.path.join(resume.strip(), resume_file))
    else:
        net.load_parameters(resume.strip())
        async_net.load_parameters(resume.strip())

    return start_epoch


def get_net(trained_on_dataset, ctx, definition='ours'):
    # handle hierarchical classes, need to pass through a list of lists to the model used for masking classes for NMS
    # only used during testing/inference
    if FLAGS.features_dir is not None:
        if FLAGS.syncbn and len(ctx) > 1:
            net = yolo3_no_backbone(trained_on_dataset.classes,
                                    norm_layer=gluon.contrib.nn.SyncBatchNorm,
                                    norm_kwargs={'num_devices': len(ctx)})
            async_net = yolo3_no_backbone(trained_on_dataset.classes)  # used by cpu worker
        else:
            net = yolo3_no_backbone(trained_on_dataset.classes)
            async_net = net

    elif definition == 'ours':  # our model definition from definitions.py, atm equiv to defaults, but might be useful in future
        if FLAGS.network == 'darknet53':
            if FLAGS.syncbn and len(ctx) > 1:
                if FLAGS.conv_types[0] is 2:

                    net = yolo3_darknet53(trained_on_dataset.classes,
                                          pretrained_base=FLAGS.pretrained_cnn,
                                          norm_layer=gluon.contrib.nn.SyncBatchNorm,
                                          freeze_base=bool(FLAGS.freeze_base),
                                          norm_kwargs={'num_devices': len(ctx)},
                                          k=FLAGS.window[0], k_join_type=FLAGS.k_join_type, k_join_pos=FLAGS.k_join_pos,
                                          block_conv_type=FLAGS.block_conv_type, rnn_pos=FLAGS.rnn_pos,
                                          corr_pos=FLAGS.corr_pos, corr_d=FLAGS.corr_d, motion_stream=FLAGS.motion_stream,
                                          add_type=FLAGS.stream_gating, new_model=FLAGS.new_model,
                                          hierarchical=FLAGS.hier, h_join_type=FLAGS.h_join_type,
                                          temporal=FLAGS.temp, t_out=FLAGS.mult_out)
                    async_net = yolo3_darknet53(trained_on_dataset.classes,
                                                pretrained_base=False,
                                                freeze_base=bool(FLAGS.freeze_base),
                                                k=FLAGS.window[0], k_join_type=FLAGS.k_join_type, k_join_pos=FLAGS.k_join_pos,
                                                block_conv_type=FLAGS.block_conv_type, rnn_pos=FLAGS.rnn_pos,
                                                corr_pos=FLAGS.corr_pos, corr_d=FLAGS.corr_d,
                                                motion_stream=FLAGS.motion_stream, add_type=FLAGS.stream_gating,
                                                new_model=FLAGS.new_model,
                                                hierarchical=FLAGS.hier, h_join_type=FLAGS.h_join_type,
                                                temporal=FLAGS.temp, t_out=FLAGS.mult_out)  # used by cpu worker
                else:
                    net = yolo3_3ddarknet(trained_on_dataset.classes,
                                          pretrained_base=FLAGS.pretrained_cnn,
                                          norm_layer=gluon.contrib.nn.SyncBatchNorm,
                                          freeze_base=bool(FLAGS.freeze_base),
                                          norm_kwargs={'num_devices': len(ctx)},
                                          conv_types=FLAGS.conv_types)
                    async_net = yolo3_3ddarknet(trained_on_dataset.classes,
                                                pretrained_base=False,
                                                freeze_base=bool(FLAGS.freeze_base),
                                                conv_types=FLAGS.conv_types)  # used by cpu worker
            else:
                if FLAGS.conv_types[0] is 2:
                    net = yolo3_darknet53(trained_on_dataset.classes,
                                          pretrained_base=FLAGS.pretrained_cnn,
                                          freeze_base=bool(FLAGS.freeze_base),
                                          k=FLAGS.window[0], k_join_type=FLAGS.k_join_type, k_join_pos=FLAGS.k_join_pos,
                                          block_conv_type=FLAGS.block_conv_type, rnn_pos=FLAGS.rnn_pos,
                                          corr_pos=FLAGS.corr_pos, corr_d=FLAGS.corr_d, motion_stream=FLAGS.motion_stream,
                                          add_type=FLAGS.stream_gating, new_model=FLAGS.new_model,
                                          hierarchical=FLAGS.hier, h_join_type=FLAGS.h_join_type,
                                          temporal=FLAGS.temp, t_out=FLAGS.mult_out)
                    async_net = net
                else:
                    net = yolo3_3ddarknet(trained_on_dataset.classes,
                                          pretrained_base=FLAGS.pretrained_cnn,
                                          freeze_base=bool(FLAGS.freeze_base),
                                          conv_types=FLAGS.conv_types)
                    async_net = net

        else:
            raise NotImplementedError('Backbone CNN model {} not implemented.'.format(FLAGS.network))

    else:  # the default definition from gluoncv
        if FLAGS.network == 'darknet53':
            net_name = '_'.join(('yolo3', FLAGS.network, 'custom'))  # only get custom, use FLAGS.resume to load particular set (voc, coco) weights
            root = os.path.join('models', 'definitions', 'darknet', 'weights')

            if FLAGS.syncbn and len(ctx) > 1:
                net = get_model(net_name, root=root, pretrained_base=FLAGS.pretrained_cnn,
                                classes=trained_on_dataset.classes,
                                norm_layer=gluon.contrib.nn.SyncBatchNorm,
                                norm_kwargs={'num_devices': len(ctx)})
                async_net = get_model(net_name, pretrained_base=False, classes=trained_on_dataset.classes)
            else:
                net = get_model(net_name, root=root,
                                pretrained_base=FLAGS.pretrained_cnn, classes=trained_on_dataset.classes)
                async_net = net
        else:
            raise NotImplementedError('Backbone CNN model {} not implemented.'.format(FLAGS.network))

    if FLAGS.resume.strip():
        start_epoch = resume(net, async_net, FLAGS.resume, FLAGS.start_epoch)
    else:
        start_epoch = FLAGS.start_epoch
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            net.initialize()
            async_net.initialize()

    return net, async_net, start_epoch


def validate(net, val_data, ctx, eval_metric):
    """Test on validation dataset."""
    eval_metric.reset()
    # set nms threshold and topk constraint
    net.set_nms(nms_thresh=0.45, nms_topk=400)
    mx.nd.waitall()
    if not FLAGS.nd_only:
        net.hybridize()
    st = time.time()
    for bi, batch in tqdm(enumerate(val_data), total=len(val_data), desc='testing'):
        if FLAGS.features_dir is not None:
            f1 = gluon.utils.split_and_load(batch[0], ctx_list=ctx, batch_axis=0, even_split=False)
            f2 = gluon.utils.split_and_load(batch[1], ctx_list=ctx, batch_axis=0, even_split=False)
            f3 = gluon.utils.split_and_load(batch[2], ctx_list=ctx, batch_axis=0, even_split=False)
            label = gluon.utils.split_and_load(batch[3], ctx_list=ctx, batch_axis=0, even_split=False)
        else:
            data = gluon.utils.split_and_load(batch[0], ctx_list=ctx, batch_axis=0, even_split=False)
            label = gluon.utils.split_and_load(batch[1], ctx_list=ctx, batch_axis=0, even_split=False)
        det_bboxes = []
        det_ids = []
        det_scores = []
        gt_bboxes = []
        gt_ids = []
        gt_difficults = []
        if FLAGS.features_dir is not None:
            for x1, x2, x3, y in zip(f1, f2, f3, label):
                # get prediction results
                ids, scores, bboxes = net(x1, x2, x3)
                det_ids.append(ids)
                det_scores.append(scores)
                # clip to image size
                det_bboxes.append(bboxes.clip(0, batch[0].shape[-1]))  # clip to last dim which we assume is width
                # split ground truths
                gt_ids.append(y.slice_axis(axis=-1, begin=4, end=5))
                gt_bboxes.append(y.slice_axis(axis=-1, begin=0, end=4))
                gt_difficults.append(y.slice_axis(axis=-1, begin=5, end=6) if y.shape[-1] > 5 else None)
        else:
            for x, y in zip(data, label):
                # get prediction results
                ids, scores, bboxes = net(x)
                det_ids.append(ids)
                det_scores.append(scores)
                # clip to image size
                det_bboxes.append(bboxes.clip(0, batch[0].shape[-1]))  # clip to last dim which we assume is width
                # split ground truths
                gt_ids.append(y.slice_axis(axis=-1, begin=4, end=5))
                gt_bboxes.append(y.slice_axis(axis=-1, begin=0, end=4))
                gt_difficults.append(y.slice_axis(axis=-1, begin=5, end=6) if y.shape[-1] > 5 else None)

        # lists with results ran on each gpu (ie len of list is = num gpus) in each list is (BatchSize, Data
        # update metric
        # eval_metric.update(det_bboxes, det_ids, det_scores, gt_bboxes, gt_ids, gt_difficults)
        # lodged issue on github #872 https://github.com/dmlc/gluon-cv/issues/872
        eval_metric.update(as_numpy(det_bboxes), as_numpy(det_ids), as_numpy(det_scores),
                           as_numpy(gt_bboxes), as_numpy(gt_ids), as_numpy(gt_difficults))
    return eval_metric.get()


def train(net, train_data, train_dataset, val_data, eval_metric, ctx, save_prefix, start_epoch, num_samples):
    """Training pipeline"""
    net.collect_params().reset_ctx(ctx)
    if FLAGS.no_wd:
        for k, v in net.collect_params('.*beta|.*gamma|.*bias').items():
            v.wd_mult = 0.0

    if FLAGS.label_smooth:
        net._target_generator._label_smooth = True

    if FLAGS.lr_decay_period > 0:
        lr_decay_epoch = list(range(FLAGS.lr_decay_period, FLAGS.epochs, FLAGS.lr_decay_period))
    else:
        lr_decay_epoch = FLAGS.lr_decay_epoch

    # for handling reloading from past epoch
    lr_decay_epoch_tmp = list()
    for e in lr_decay_epoch:
        if int(e) <= start_epoch:
            FLAGS.lr = FLAGS.lr * FLAGS.lr_decay
        else:
            lr_decay_epoch_tmp.append(int(e) - start_epoch - FLAGS.warmup_epochs)
    lr_decay_epoch = lr_decay_epoch_tmp

    num_batches = num_samples // FLAGS.batch_size
    lr_scheduler = LRSequential([
        LRScheduler('linear', base_lr=0, target_lr=FLAGS.lr,
                    nepochs=FLAGS.warmup_epochs, iters_per_epoch=num_batches),
        LRScheduler(FLAGS.lr_mode, base_lr=FLAGS.lr,
                    nepochs=FLAGS.epochs - FLAGS.warmup_epochs - start_epoch,
                    iters_per_epoch=num_batches,
                    step_epoch=lr_decay_epoch,
                    step_factor=FLAGS.lr_decay, power=2),
    ])

    trainer = gluon.Trainer(
        net.collect_params(), 'sgd',
        {'wd': FLAGS.wd, 'momentum': FLAGS.momentum, 'lr_scheduler': lr_scheduler},
        kvstore='local')

    # targets
    sigmoid_ce = gluon.loss.SigmoidBinaryCrossEntropyLoss(from_sigmoid=False)
    l1_loss = gluon.loss.L1Loss()

    # metrics
    obj_metrics = mx.metric.Loss('ObjLoss')
    center_metrics = mx.metric.Loss('BoxCenterLoss')
    scale_metrics = mx.metric.Loss('BoxScaleLoss')
    cls_metrics = mx.metric.Loss('ClassLoss')

    # set up logger
    logging.basicConfig()
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    log_file_path = save_prefix + '_train.log'
    log_dir = os.path.dirname(log_file_path)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)
    fh = logging.FileHandler(log_file_path)
    logger.addHandler(fh)
    # logger.info(FLAGS)

    # set up tensorboard summary writer
    tb_sw = SummaryWriter(log_dir=os.path.join(log_dir, 'tb'), comment=FLAGS.save_prefix)

    # Check if wanting to resume
    logger.info('Start training from [Epoch {}]'.format(start_epoch))
    if FLAGS.resume.strip() and os.path.exists(save_prefix+'_best_map.log'):
        with open(save_prefix+'_best_map.log', 'r') as f:
            lines = [line.split()[1] for line in f.readlines()]
            best_map = [float(lines[-1])]
    else:
        best_map = [0]

    # Training loop
    num_batches = int(len(train_dataset)/FLAGS.batch_size)
    for epoch in range(start_epoch, FLAGS.epochs+1):

        st = time.time()
        if FLAGS.mixup:
            # TODO(zhreshold): more elegant way to control mixup during runtime
            try:
                train_data._dataset.set_mixup(np.random.beta, 1.5, 1.5)
            except AttributeError:
                train_data._dataset._data.set_mixup(np.random.beta, 1.5, 1.5)
            if epoch >= FLAGS.epochs - FLAGS.no_mixup_epochs:
                try:
                    train_data._dataset.set_mixup(None)
                except AttributeError:
                    train_data._dataset._data.set_mixup(None)

        tic = time.time()
        btic = time.time()
        if not FLAGS.nd_only:
            net.hybridize()
        for i, batch in enumerate(train_data):
            batch_size = batch[0].shape[0]

            if FLAGS.max_epoch_time > 0 and (time.time()-st)/60 > FLAGS.max_epoch_time:
                logger.info('Max epoch time of %d minutes reached after completing %d%% of epoch. '
                            'Moving on to next epoch' % (FLAGS.max_epoch_time, int(100*(i/num_batches))))
                break

            if FLAGS.features_dir is not None:
                f1 = gluon.utils.split_and_load(batch[0], ctx_list=ctx, batch_axis=0)
                f2 = gluon.utils.split_and_load(batch[1], ctx_list=ctx, batch_axis=0)
                f3 = gluon.utils.split_and_load(batch[2], ctx_list=ctx, batch_axis=0)
                # objectness, center_targets, scale_targets, weights, class_targets
                fixed_targets = [gluon.utils.split_and_load(batch[it], ctx_list=ctx, batch_axis=0) for it in range(3, 8)]
                gt_boxes = gluon.utils.split_and_load(batch[8], ctx_list=ctx, batch_axis=0)
            else:
                data = gluon.utils.split_and_load(batch[0], ctx_list=ctx, batch_axis=0)
                # objectness, center_targets, scale_targets, weights, class_targets
                fixed_targets = [gluon.utils.split_and_load(batch[it], ctx_list=ctx, batch_axis=0) for it in range(1, 6)]
                gt_boxes = gluon.utils.split_and_load(batch[6], ctx_list=ctx, batch_axis=0)
            sum_losses = []
            obj_losses = []
            center_losses = []
            scale_losses = []
            cls_losses = []
            if FLAGS.features_dir is not None:
                with autograd.record():
                    for ix, (x1, x2, x3) in enumerate(zip(f1, f2, f3)):
                        obj_loss, center_loss, scale_loss, cls_loss = net(x1, x2, x3, gt_boxes[ix], *[ft[ix] for ft in fixed_targets])
                        sum_losses.append(obj_loss + center_loss + scale_loss + cls_loss)
                        obj_losses.append(obj_loss)
                        center_losses.append(center_loss)
                        scale_losses.append(scale_loss)
                        cls_losses.append(cls_loss)
                    autograd.backward(sum_losses)
            else:
                with autograd.record():
                    for ix, x in enumerate(data):
                        obj_loss, center_loss, scale_loss, cls_loss = net(x, gt_boxes[ix], *[ft[ix] for ft in fixed_targets])
                        sum_losses.append(obj_loss + center_loss + scale_loss + cls_loss)
                        obj_losses.append(obj_loss)
                        center_losses.append(center_loss)
                        scale_losses.append(scale_loss)
                        cls_losses.append(cls_loss)
                    autograd.backward(sum_losses)

            if FLAGS.motion_stream is None:
                trainer.step(batch_size)
            else:
                trainer.step(batch_size, ignore_stale_grad=True)  # we don't use all layers of each stream
            obj_metrics.update(0, obj_losses)
            center_metrics.update(0, center_losses)
            scale_metrics.update(0, scale_losses)
            cls_metrics.update(0, cls_losses)

            if FLAGS.log_interval and not (i + 1) % FLAGS.log_interval:
                name1, loss1 = obj_metrics.get()
                name2, loss2 = center_metrics.get()
                name3, loss3 = scale_metrics.get()
                name4, loss4 = cls_metrics.get()
                logger.info('[Epoch {}][Batch {}/{}], LR: {:.2E}, Speed: {:.3f} samples/sec, {}={:.3f}, {}={:.3f}, '
                            '{}={:.3f}, {}={:.3f}'.format(epoch, i, num_batches, trainer.learning_rate,
                                                          batch_size/(time.time()-btic),
                                                          name1, loss1, name2, loss2, name3, loss3, name4, loss4))
                tb_sw.add_scalar(tag='Training_' + name1, scalar_value=loss1, global_step=(epoch * len(train_data) + i))
                tb_sw.add_scalar(tag='Training_' + name2, scalar_value=loss2, global_step=(epoch * len(train_data) + i))
                tb_sw.add_scalar(tag='Training_' + name3, scalar_value=loss3, global_step=(epoch * len(train_data) + i))
                tb_sw.add_scalar(tag='Training_' + name4, scalar_value=loss4, global_step=(epoch * len(train_data) + i))
            btic = time.time()

        name1, loss1 = obj_metrics.get()
        name2, loss2 = center_metrics.get()
        name3, loss3 = scale_metrics.get()
        name4, loss4 = cls_metrics.get()
        logger.info('[Epoch {}] Training cost: {:.3f}, {}={:.3f}, {}={:.3f}, {}={:.3f}, {}={:.3f}'.format(
            epoch, (time.time()-tic), name1, loss1, name2, loss2, name3, loss3, name4, loss4))
        if not (epoch + 1) % FLAGS.val_interval:
            # consider reduce the frequency of validation to save time

            logger.info('End Epoch {}: # samples: {}, seconds: {}, samples/sec: {:.2f}'.format(
                epoch, len(train_data)*batch_size, time.time() - st, (len(train_data)*batch_size)/(time.time() - st)))
            st = time.time()
            map_name, mean_ap = validate(net, val_data, ctx, eval_metric)
            logger.info('End Val: # samples: {}, seconds: {}, samples/sec: {:.2f}'.format(
                len(val_data)*batch_size, time.time() - st, (len(val_data) * batch_size)/(time.time() - st)))

            val_msg = '\n'.join(['{}={}'.format(k, v) for k, v in zip(map_name, mean_ap)])
            tb_sw.add_scalar(tag='Validation_mAP', scalar_value=float(mean_ap[-1]),
                             global_step=(epoch * len(train_data) + i))
            logger.info('[Epoch {}] Validation: \n{}'.format(epoch, val_msg))
            current_map = float(mean_ap[-1])
        else:
            current_map = 0.
        save_params(net, best_map, current_map, epoch, FLAGS.save_interval, save_prefix)


def main(_argv):
    FLAGS.window = [int(s) for s in FLAGS.window]
    FLAGS.conv_types = [int(s) for s in FLAGS.conv_types]
    FLAGS.hier = [int(s) for s in FLAGS.hier]

    if FLAGS.window[0] > 1:
        assert 'vid' in FLAGS.dataset, 'If using window size >1 you can only use the vid dataset'
    else:
        FLAGS.k_join_type = None  # can't pool 1 frame..
        FLAGS.k_join_pos = None

    if FLAGS.num_workers < 0:
        FLAGS.num_workers = multiprocessing.cpu_count()

    # fix seed for mxnet, numpy and python builtin random generator.
    gutils.random.seed(FLAGS.seed)

    # training contexts
    ctx = [mx.gpu(int(i)) for i in FLAGS.gpus]
    ctx = ctx if ctx else [mx.cpu()]

    # training data
    train_dataset, val_dataset, eval_metric = get_dataset(FLAGS.dataset, FLAGS.dataset_val,
                                                          os.path.join('models', 'experiments', FLAGS.save_prefix))

    trained_on_dataset = train_dataset
    if FLAGS.trained_on:
        # load the model with these classes then reset
        trained_on_dataset, _, _ = get_dataset(FLAGS.trained_on, os.path.join('models', 'experiments', FLAGS.save_prefix))

    # network
    if os.path.exists(os.path.join('models', 'experiments', FLAGS.save_prefix)) and not bool(FLAGS.resume.strip()) \
            and FLAGS.save_prefix != '0000':  # using 0000 for testing
        logging.error("{} exists so won't overwrite and restart training. You can resume training by using "
                      "--resume".format(os.path.join('models', 'experiments', FLAGS.save_prefix)))
        return
    os.makedirs(os.path.join('models', 'experiments', FLAGS.save_prefix), exist_ok=True)
    if isinstance(FLAGS.dataset, list):
        FLAGS.dataset = '-'.join(FLAGS.dataset)
    net_name = '_'.join(('yolo3', FLAGS.network, FLAGS.dataset))
    save_prefix = os.path.join('models', 'experiments', FLAGS.save_prefix, net_name)

    net, async_net, start_epoch = get_net(trained_on_dataset, ctx, definition='ours')  # 'gluon' or 'ours'

    if FLAGS.trained_on:
        net.reset_class(train_dataset.classes)
        async_net.reset_class(train_dataset.classes)

    if FLAGS.motion_stream == 'flownet':
        FLAGS.data_shape = 384  # cause 416 is a nasty shape

    # log a summary of the network
    if FLAGS.features_dir is not None:
        if FLAGS.window[0] > 1:
            logging.info(net.summary(mx.nd.ndarray.ones(shape=(FLAGS.batch_size, FLAGS.window[0], 256,
                                                               int(FLAGS.data_shape / 8), int(FLAGS.data_shape / 8))),
                                     mx.nd.ndarray.ones(shape=(FLAGS.batch_size, FLAGS.window[0], 512,
                                                               int(FLAGS.data_shape / 16), int(FLAGS.data_shape / 16))),
                                     mx.nd.ndarray.ones(shape=(FLAGS.batch_size, FLAGS.window[0], 1024,
                                                               int(FLAGS.data_shape / 32), int(FLAGS.data_shape / 32)))))
        else:
            logging.info(net.summary(mx.nd.ndarray.ones(shape=(FLAGS.batch_size, 256,
                                                               int(FLAGS.data_shape / 8), int(FLAGS.data_shape / 8))),
                                     mx.nd.ndarray.ones(shape=(FLAGS.batch_size, 512,
                                                               int(FLAGS.data_shape / 16), int(FLAGS.data_shape / 16))),
                                     mx.nd.ndarray.ones(shape=(FLAGS.batch_size, 1024,
                                                               int(FLAGS.data_shape / 32), int(FLAGS.data_shape / 32)))))
    else:
        if FLAGS.window[0] > 1:
            # gutils.viz.plot_network(net, shape=(FLAGS.batch_size, FLAGS.window[0], 3, FLAGS.data_shape, FLAGS.data_shape),
            #                         save_prefix=save_prefix)
            logging.info(net.summary(mx.nd.ndarray.ones(shape=(FLAGS.batch_size, FLAGS.window[0], 3, FLAGS.data_shape, FLAGS.data_shape))))
        else:
            # gutils.viz.plot_network(net, shape=(FLAGS.batch_size, 3, FLAGS.data_shape, FLAGS.data_shape),
            #                         save_prefix=save_prefix)
            logging.info(net.summary(mx.nd.ndarray.ones(shape=(FLAGS.batch_size, 3, FLAGS.data_shape, FLAGS.data_shape))))

    # load the dataloader
    train_data, val_data = get_dataloader(async_net, train_dataset, val_dataset, FLAGS.batch_size)

    num_samples = FLAGS.num_samples
    if num_samples < 0:
        num_samples = len(train_dataset)

    # training
    train(net, train_data, train_dataset, val_data, eval_metric, ctx, save_prefix, start_epoch, num_samples)


if __name__ == '__main__':

    try:
        app.run(main)
    except SystemExit:
        pass


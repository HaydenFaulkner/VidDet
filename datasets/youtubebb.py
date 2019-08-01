"""YouTube-BB object detection dataset."""
from __future__ import absolute_import
from __future__ import division

from absl import app, flags, logging
import csv
import json
import math
import os
from subprocess import call
from tqdm import tqdm
import warnings
import numpy as np
try:
    import xml.etree.cElementTree as ET
except ImportError:
    import xml.etree.ElementTree as ET
import mxnet as mx
from gluoncv.data.base import VisionDataset

from utils.video import video_to_frames


class YouTubeBBDetection(VisionDataset):
    """YouTube-BB detection Dataset.

    Parameters
    ----------
    root : str, default '~/mxnet/datasets/ytbb'
        Path to folder storing the dataset.
    splits : tuple, default ('train')
        Candidates can be: 'train', 'val', 'test'.
    transform : callable, default None
        A function that takes data and label and transforms them. Refer to
        :doc:`./transforms` for examples.

        A transform function for object detection should take label into consideration,
        because any geometric modification will require label to be modified.
    index_map : dict, default None
        In default, the 23 classes are mapped into indices from 0 to 22. We can
        customize it by providing a str to int dict specifying how to map class
        names to indices. Use by advanced users only, when you want to swap the orders
        of class labels.
    """

    def __init__(self, root=os.path.join('~', '.mxnet', 'datasets', 'ytbb'),
                 splits=('train',), allow_empty=False, videos=False, clips=True,
                 transform=None, index_map=None, frames=1, inference=False,
                 window_size=1, window_step=1):
        super(YouTubeBBDetection, self).__init__(root)
        self._im_shapes = {}
        self._root = os.path.expanduser(root)
        self._transform = transform
        assert len(splits) == 1, print('Can only take one split currently as otherwise conflicting image ids')
        self._splits = splits
        self._videos = videos
        self._clips = clips  # used to specify if want to split clips as YTBB default one obj inst per clip, or split based on video ID
        self._frames = frames
        self._inference = inference
        if videos or window_size > 1:
            allow_empty = True  # allow true if getting video volumes, prevent empties when doing framewise only
        self._allow_empty = allow_empty
        self._window_size = window_size
        self._window_step = window_step
        self._windows = None
        self._coco_path = os.path.join(self._root, 'jsons', '_'.join([str(s[0]) + s[1] for s in self._splits])+'.json')
        self._image_path = os.path.join(self._root, 'frames', '{}', '{}.jpg')
        self.index_map = index_map or dict(zip(self.class_ids, range(self.num_class)))
        self._samples = self._load_items(splits)
        self._sample_ids = sorted(list(self._samples.keys()))

    def __str__(self):
        return '\n\n' + self.__class__.__name__ + '\n' + self.stats()[0] + '\n'

    @property
    def classes(self):
        """Category names."""
        names = os.path.join('./datasets/names/youtubebb.names')
        with open(names, 'r') as f:
            classes = [line.strip() for line in f.readlines()]
        return classes

    @property
    def class_ids(self):
        """Category names."""
        names = os.path.join('./datasets/names/youtubebb_ids.names')
        with open(names, 'r') as f:
            class_ids = [int(line.strip()) for line in f.readlines()]
        return class_ids

    @property
    def wn_classes(self):
        """Category names."""
        names = os.path.join('./datasets/names/youtubebb_wn.names')
        with open(names, 'r') as f:
            wn_classes = [line.strip() for line in f.readlines()]
        return wn_classes

    # @property
    def image_ids(self):
    #     # if self._videos:
    #     #     return [sample[3].split('/')[0] for sample in self._samples.values()] # todo check which we want?
    #     #     # return [id for id in [sample[4] for sample in self._items]] # todo check which we want?
    #     # else:
        return self._sample_ids

    @property
    def motion_ious(self):
        ious = self._load_motion_ious()
        motion_ious = np.array([v for k, v in ious.items() if int(k) in self.image_ids])
        return motion_ious

    def __len__(self):
        return len(self._sample_ids)

    def __getitem__(self, idx):
        sample_id = self._sample_ids[idx]
        sample = self._samples[sample_id]
        if not self._videos:
            img_path = self._image_path.format(*[sample_id.split(',')[0], sample_id.split(',')[-1]])
            label = self._load_label(idx)[:, :-1]  # remove track id

            if self._window_size > 1:
                imgs = None
                window = self._windows[self._sample_ids[idx]]
                for sid in window:
                    img_path = self._image_path.format(*self._samples[sid])
                    img = mx.image.imread(img_path, 1)
                    if imgs is None:
                        imgs = img
                    else:
                        imgs = mx.ndarray.concatenate([imgs, img], axis=2)
                img = imgs
            else:
                img = mx.image.imread(img_path, 1)

            if self._inference:
                if self._transform is not None:
                    return self._transform(img, label, idx)
                return img, label, idx
            else:
                if self._transform is not None:
                    return self._transform(img, label)
                return img, label
        else:
            sample_id = self._sample_ids[idx]
            sample = self._samples[sample_id]
            vid = None
            labels = None
            for frame in sample[3]:
                img_id = (sample[0], sample[1], sample[2]+'/'+frame)
                img_path = self._image_path.format(*img_id)
                label = self._load_label(idx, frame=frame)
                img = mx.image.imread(img_path, 1)
                if self._transform is not None:
                    img, label = self._transform(img, label)

                label = self._pad_to_dense(label, 20)
                if labels is None:
                    vid = mx.ndarray.expand_dims(img, 0)  # extra mem used if num_workers wrong on the dataloader
                    labels = np.expand_dims(label, 0)
                else:
                    vid = mx.ndarray.concatenate([vid, mx.ndarray.expand_dims(img, 0)])
                    labels = np.concatenate((labels, np.expand_dims(label, 0)))
            return None, labels

    def sample_path(self, idx):
        return self._image_path.format(*self._samples[self._sample_ids[idx]])

    def _load_items(self, splits):
        """Load annotation data from .csv and setup with this datasets params."""
        ids = []
        for split in splits:
            root = self._root
            if split == 'val':
                split = 'validation'
            annotation_path = os.path.join(root, 'yt_bb_detection_'+split+'.csv')
            if os.path.exists(annotation_path):
                logging.info("Loading data from {}".format(annotation_path))
                with open(annotation_path, 'r') as csvfile:
                    rows = csv.reader(csvfile, delimiter=',')
                    for row in rows:
                        ids.append(row)

        # Lets go through the data and organise as desired by set params
        videos = dict()
        empty = 0
        if self._clips:
            for id in ids:
                if not self._allow_empty and id[5] == 'absent':  # this is an empty frame
                    empty += 1
                    continue
                vid_id = id[0] + ',' + id[2] + ',' + id[4]
                frame_id = id[1]
                if vid_id in videos:
                    if frame_id in videos[vid_id]:
                        print(videos[vid_id][frame_id])
                        videos[vid_id][frame_id].append(id[2:])
                    else:
                        videos[vid_id][frame_id] = [id[2:]]
                else:
                    videos[vid_id] = {frame_id: [id[2:]]}

        else:
            for id in ids:
                if not self._allow_empty and id[5] == 'absent':  # this is an empty frame
                    empty += 1
                    continue
                vid_id = id[0]
                frame_id = id[1]
                if vid_id in videos:
                    if frame_id in videos[vid_id]:
                        videos[vid_id][frame_id].append(id[2:])
                    else:
                        videos[vid_id][frame_id] = [id[2:]]
                else:
                    videos[vid_id] = {frame_id: [id[2:]]}

        if empty > 0:
            logging.info("Removed {} empty annotations".format(empty))

        # only take a % of the dataset
        if self._frames != 1:
            for vid_id in videos:
                frames = sorted(videos[vid_id].keys())
                if self._frames < 1: # cut down per video
                    frames = [frames[i] for i in range(0, len(frames), int(1 / self._frames))]
                elif self._frames > 1:  # cut down per video
                    frames = [frames[i] for i in range(0, len(frames), int(math.ceil(len(frames)/self._frames)))]

                # remove frames that aren't in the new frames
                frames_ = list(videos[vid_id].keys())
                for frame_id in frames_:
                    if frame_id not in frames:
                        del videos[vid_id][frame_id]

        if self._videos:
            return videos

        if self._window_size > 1:
            self._windows = dict()

            for vid_id in videos:  # lock to only getting frames from the same video
                frame_ids = [vid_id+','+frame_id for frame_id in sorted(videos[vid_id].keys())]
                for i in range(len(frame_ids)):  # for each frame in this video
                    window = list()  # setup a new window (will hold the frame_ids)

                    window_size = int(self._window_size / 2.0)  # halves and floors it

                    # lets step back through the video and get the frame_ids
                    for back_i in range(window_size*self._window_step, self._window_step-1, -self._window_step):
                        window.append(frame_ids[max(0, i-back_i)])  # will add first frame to pad if window too big

                    # add the current frame
                    window.append(frame_ids[i])

                    # lets step forward and add future frame_ids
                    for forw_i in range(self._window_step, window_size*self._window_step+1, self._window_step):
                        if len(window) == self._window_size:
                            break  # only triggered if self._window_size is even, we disregard last frame
                        window.append(frame_ids[min(len(frame_ids)-1, i+forw_i)])  # will add last frame to pad if window too big

                    # add the window frame ids to a dict to be used in the get() function
                    self._windows[frame_ids[i]] = window

        # make a frames dict as each sample is a frame not a video
        frames = dict()
        for vid_id in videos:
            for frame_id in videos[vid_id].keys():
                frames[vid_id+','+frame_id] = videos[vid_id][frame_id]
        return frames

    def _load_label(self, idx, frame=None):
        """Just get the label data from the cache."""

        sample_id = self._sample_ids[idx]
        sample = self._samples[sample_id]

        if self._videos:
            assert frame is not None
            sample = sample[frame]

        label = []
        for obj in sample:
            cls_id = int(obj[0])
            if cls_id not in self.class_ids:
                continue
            cls_id = self.index_map[cls_id]
            trk_id = int(obj[2])
            xmin = float(obj[4])
            ymin = float(obj[6])
            xmax = float(obj[5])
            ymax = float(obj[7])  # todo these should be pixels not percentages

            if obj[3] == 'absent' or xmin < 0 or xmax < 0 or ymin < 0 or ymax < 0:  # no box
                continue

            try:
                xmin, ymin, xmax, ymax = self._validate_label(xmin, ymin, xmax, ymax, sample_id)
            except AssertionError as e:
                logging.error("Invalid label at {}, {}".format(sample_id, e))
                continue

            label.append([xmin, ymin, xmax, ymax, cls_id, trk_id])

        if self._allow_empty and len(label) < 1:
            label.append([-1, -1, -1, -1, -1, -1])
        return np.array(label)

    @staticmethod
    def _validate_label(xmin, ymin, xmax, ymax, sample_id):
        """Validate labels."""
        if not 0 <= xmin < 1 or not 0 <= ymin < 1 or not xmin < xmax <= 1 or not ymin < ymax <= 1:
            logging.warning("box: {} {} {} {} incompatible for {}.".format(xmin, ymin, xmax, ymax, sample_id))

            xmin = min(max(0, xmin), 1)
            ymin = min(max(0, ymin), 1)
            xmax = min(max(xmin + 1, xmax), 1)
            ymax = min(max(ymin + 1, ymax), 1)

            logging.info("new box: {} {} {} {}.".format(xmin, ymin, xmax, ymax))

        assert 0 <= xmin < 1, "xmin must in [0, {}), given {}".format(1, xmin)
        assert 0 <= ymin < 1, "ymin must in [0, {}), given {}".format(1, ymin)
        assert xmin < xmax <= 1, "xmax must in (xmin, {}], given {}".format(1, xmax)
        assert ymin < ymax <= 1, "ymax must in (ymin, {}], given {}".format(1, ymax)

        return xmin, ymin, xmax, ymax

    @staticmethod
    def _validate_class_names(class_list):
        """Validate class names."""
        assert all(c.islower() for c in class_list), "uppercase characters"
        stripped = [c for c in class_list if c.strip() != c]
        if stripped:
            warnings.warn('white space removed for {}'.format(stripped))

    def _pad_to_dense(self, labels, maxlen=100):
        """Appends the minimal required amount of zeroes at the end of each
         array in the jagged array `M`, such that `M` looses its jagedness."""

        x = -np.ones((maxlen, 6))
        for enu, row in enumerate(labels):
            x[enu, :] += row + 1
        return x

    def image_size(self, id):
        # todo
        # if len(self._im_shapes) == 0:
        #     for idx in tqdm(range(len(self._sample_ids)), desc="populating im_shapes"):
        #         self._load_label(idx)
        # return self._im_shapes[id]
        return None

    def stats(self):
        cls_boxes = []
        n_videos = 0
        n_samples = len(self._sample_ids)
        n_boxes = [0]*len(self.classes)
        n_instances = [0]*len(self.classes)
        past_vid_id = ''
        for idx in tqdm(range(len(self._sample_ids))):
            sample_id = self._sample_ids[idx]
            vid_id = sample_id
            if not self._videos:
                vid_id = ','.join(sample_id.split(',')[:-1])  # remove frame_id

            if vid_id != past_vid_id:
                vid_instances = []
                past_vid_id = vid_id
                n_videos += 1
            if self._videos:
                for frame in self._samples[sample_id].keys():
                    for box in self._load_label(idx, frame):
                        n_boxes[int(box[4])] += 1
                        if int(box[5]) not in vid_instances:
                            vid_instances.append(int(box[5]))
                            n_instances[int(box[4])] += 1
            else:
                for box in self._load_label(idx):
                    n_boxes[int(box[4])] += 1
                    if int(box[5]) not in vid_instances:
                        vid_instances.append(int(box[5]))
                        n_instances[int(box[4])] += 1

        if self._videos:
            out_str = '{0: <10} {1}\n{2: <10} {3}\n{4: <10} {5}\n{6: <10} {7}\n{8: <10} {9}\n'.format(
                'Split:', ', '.join([s for s in self._splits]),
                'Videos:', n_samples,
                'Boxes:', sum(n_boxes),
                'Instances:', sum(n_instances),
                'Classes:', len(self.classes))
        else:
            out_str = '{0: <10} {1}\n{2: <10} {3}\n{4: <10} {5}\n{6: <10} {7}\n{8: <10} {9}\n'.format(
                'Split:', ', '.join([s for s in self._splits]),
                'Videos:', n_videos,
                'Frames:', n_samples,
                'Boxes:', sum(n_boxes),
                'Instances:', sum(n_instances),
                'Classes:', len(self.classes))
        out_str += '-'*35 + '\n'
        for i in range(len(n_boxes)):
            out_str += '{0: <3} {1: <10} {2: <15} {3} {4}\n'.format(i, self.wn_classes[i], self.classes[i],
                                                                    n_boxes[i], n_instances[i])
            cls_boxes.append([i, self.wn_classes[i], self.classes[i], n_boxes[i]])
        out_str += '-'*35 + '\n'

        return out_str, cls_boxes

    def build_coco_json(self):

        os.makedirs(os.path.dirname(self._coco_path), exist_ok=True)

        # handle categories
        categories = list()
        for ci, (cls, wn_cls) in enumerate(zip(self.classes, self.wn_classes)):
            categories.append({'id': ci, 'name': cls, 'wnid': wn_cls})

        # handle images and boxes
        images = list()
        done_imgs = set()
        annotations = list()
        for idx in range(len(self)):
            sample_id = self._sample_ids[idx]
            # width, height = self._im_shapes[idx]

            # img_id = self.image_ids[idx]
            if sample_id not in done_imgs:
                done_imgs.add(sample_id)
                images.append({'file_name': sample_id,
                               # 'width': int(width),
                               # 'height': int(height),
                               'id': sample_id})

            for box in self._load_label(idx):
                xywh = [int(box[0]), int(box[1]), int(box[2])-int(box[0]), int(box[3])-int(box[1])]
                annotations.append({'image_id': sample_id,
                                    'id': len(annotations),
                                    'bbox': xywh,  # todo not xywh atm but rather x1y1x2y2 and as % rather than pix
                                    'area': int(xywh[2] * xywh[3]),
                                    'category_id': int(box[4]),
                                    'iscrowd': 0})

        with open(self._coco_path, 'w') as f:
            json.dump({'images': images, 'annotations': annotations, 'categories': categories}, f)

        return self._coco_path

    def _load_motion_ious(self):
        path = os.path.join(self._root, '{}_motion_ious.json'.format(self._splits[0][1]))
        if not os.path.exists(path):
            generate_motion_ious(self._root, self._splits[0][1])

        with open(path, 'r') as f:
            ious = json.load(f)

        return ious


def iou(bb, bbgt):
    """
    helper single box iou calculation function for generate_motion_ious
    """
    ov = 0
    iw = np.min((bb[2], bbgt[2])) - np.max((bb[0], bbgt[0])) + 1
    ih = np.min((bb[3], bbgt[3])) - np.max((bb[1], bbgt[1])) + 1
    if iw > 0 and ih > 0:
        # compute overlap as area of intersection / area of union
        intersect = iw * ih
        ua = (bb[2] - bb[0] + 1.) * (bb[3] - bb[1] + 1.) + \
             (bbgt[2] - bbgt[0] + 1.) * \
             (bbgt[3] - bbgt[1] + 1.) - intersect
        ov = intersect / ua
    return ov


def generate_motion_ious(root=os.path.join('datasets', 'YouTubeBB'), split='val'):
    """
    Used to generate motion_ious .json files

    Note This script is relatively intensive

    :param root: the root path of the imagenetvid set
    :param split: train or val, no year necessary
    """

    dataset = YouTubeBBDetection(root=root, splits=(split,), allow_empty=True, videos=True, percent=1)

    all_ious = {} # using dict for better removing of elements on loading based on sample_id
    sample_id = 1 # messy but works
    for idx in tqdm(range(len(dataset)), desc="Generation motion iou groundtruth"):
        _, video = dataset[idx]
        for frame in range(len(video)):
            frame_ious = []
            for box_idx in range(len(video[frame])):
                trk_id = video[frame][box_idx][5]
                if trk_id > -1:
                    ious = []
                    for i in range(-10, 11):
                        frame_c = frame + i
                        if 0 <= frame_c < len(video) and i != 0:
                            for c_box_idx in range(len(video[frame_c])):
                                c_trk_id = video[frame_c][c_box_idx][5]
                                if trk_id == c_trk_id:
                                    a = video[frame][box_idx]
                                    b = video[frame_c][c_box_idx]
                                    ious.append(iou(a, b))
                                    break

                    frame_ious.append(np.mean(ious))
            # if frame_ious:  # if this frame has no boxes we add a 0.0
            #     all_ious.append(frame_ious)
            # else:
            #     all_ious.append([0.0])
            if frame_ious:  # if this frame has no boxes we add a 0.0
                all_ious[sample_id] = frame_ious
            else:
                all_ious[sample_id] = [0.0]
            sample_id += 1

    with open(os.path.join(root, '{}_motion_ious.json'.format(split)), 'w') as f:
        json.dump(all_ious, f)


def download_youtube_video(url, save_path, name=None):
    """
    download a video from youtube
    requires youtube-dl
    pip install youtube-dl

    :param url: the full youtube url
    :param save_path: the directory to save the file
    :param name: the filename to save the video as, will default to the url
    :return: the name.ext as a string or None if download was unsuccessful
    """
    if name is None:
        name = url

    extensions = [".mp4", ".mkv", ".mp4.webm"]

    # check if it exists first
    for ext in extensions:
        if os.path.exists(os.path.join(save_path, name+ext)):
            return name+ext

    # download it, import for ensuring it's downloaded but we call from shell
    call(["youtube-dl -o '" + os.path.join(save_path, name + ".mp4") + "' '" + url + "'"], shell=True)

    # check we have downloaded it
    for ext in extensions:
        if os.path.exists(os.path.join(save_path, name+ext)):
            return name+ext

    # if didn't work will return None
    return None


def download_videos(annotation_paths,
                    save_dir=os.path.join('datasets', 'YouTubeBB', 'videos')):
    """
    downloads the videos from youtube and can also extract the frames if frames dir specified

    :param annotation_paths: paths to the annotation .csv files for getting the youtube ids
    :param save_dir: the directory to store the downloaded videos
    :return: the number of videos that weren't downloaded
    """

    # Get video youtube ids from annotation files
    ids = set()
    for annotation_path in annotation_paths:
        if os.path.exists(annotation_path):
            logging.info("Loading data from {}".format(annotation_path))
            with open(annotation_path, 'r') as csvfile:
                rows = csv.reader(csvfile, delimiter=',')
                for row in rows:
                    ids.add(row[0])
        else:
            logging.error("{} does not exist.".format(annotation_path))
            return

    each_vid_size_approx_gb = .029620394
    expected_size = each_vid_size_approx_gb * len(ids)

    logging.warning("\n\nBe warned that this will attempt to download {} videos with an approximate size of {} GBs."
                    " This could take weeks or even months depending on your download speeds."
                    "\n\nContinue? (y/n)".format(len(ids), int(expected_size)))
    response = input()
    if response.lower() not in ['y', 'yes']:
        logging.info("User Cancelled")
        return

    # Download the files
    os.makedirs(save_dir, exist_ok=True)
    errors = set()
    for v_id in tqdm(ids, desc="Downloading Videos"):

        result = download_youtube_video(url="http://youtu.be/" + v_id, save_path=save_dir, name=v_id)

        if result is None:
            errors.add(v_id)

    logging.info("Successfully downloaded {} / {} videos".format(len(ids)-len(errors), len(ids)))

    if len(errors) > 0:
        logging.info("Saving Error file: {}".format(os.path.join(save_dir, "errors.txt")))
        with open(os.path.join(save_dir, "errors.txt"), "a") as f:
            for v_id in errors:
                f.write(v_id+"\n")

    return len(errors)


def extract_frames(videos_dir, save_dir, stats_dir):
    """
    extracts frames from the videos downloaded from youtube

    :param videos_dir: directory containing the videos
    :param save_dir: directory to save the frames
    :param stats_dir: directory to save video stats
    :return: the number of videos that weren't downloaded
    """
    videos = set(os.listdir(videos_dir))
    videos.remove('errors.txt')

    expected_size = 0

    logging.warning("\n\nBe warned that this will attempt to extract frames from {} videos with an approximate size of"
                    " {} GBs.\n\nContinue? (y/n)".format(len(videos), int(expected_size)))
    response = input()
    if response.lower() not in ['y', 'yes']:
        logging.info("User Cancelled")
        return

    errors = set()
    for video in tqdm(videos, desc="Extracting Frames"):
        # todo, modify to extract only the frames we have annotations for and name them correctly, will need to verify the milliseconds align
        # result = video_to_frames(os.path.join(videos_dir, video), save_dir, stats_dir, overwrite=True)
        result = None
        if result is None:
            errors.add(video)

    logging.info("Successfully extracted frames for {} / {} videos".format(len(videos)-len(errors), len(videos)))

    if len(errors) > 0:
        logging.info("Saving Error file: {}".format(os.path.join(save_dir, "errors.txt")))
        with open(os.path.join(save_dir, "errors.txt"), "a") as f:
            for video in errors:
                f.write(video+"\n")

    return len(errors)


if __name__ == '__main__':
    # annotation_paths = [os.path.join('datasets', 'YouTubeBB', 'yt_bb_detection_train.csv'),
    #                     os.path.join('datasets', 'YouTubeBB', 'yt_bb_detection_validation.csv')]
    # download_videos(annotation_paths,
    #                 save_dir=os.path.join('datasets', 'YouTubeBB', 'videos'))
    train_dataset = YouTubeBBDetection(
        root=os.path.join('datasets', 'YouTubeBB'), splits=('train',),
        allow_empty=True, videos=False, clips=True)
    val_dataset = YouTubeBBDetection(
        root=os.path.join('datasets', 'YouTubeBB'), splits=('val',),
        allow_empty=True, videos=False, clips=True)

    print(train_dataset)
    print(val_dataset)

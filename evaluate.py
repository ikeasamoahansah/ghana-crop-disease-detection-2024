import os

import numpy as np
import pandas as pd
import argparse
import gc
from tqdm import tqdm
from ensemble_boxes import weighted_boxes_fusion
import timm
import pickle
import multiprocessing
from multiprocessing import Pool

import torch
import torchvision
from torch.utils.data import Dataset, DataLoader
from torchvision.models.detection.faster_rcnn import FastRCNNPredictor
from torchvision.models.detection.rpn import AnchorGenerator
from torchvision.models.detection.backbone_utils import resnet_fpn_backbone
from apex import amp
from evaluation import calculate_final_score
from dataset import CropTestset
from dataset import TTAHorizontalFlip, TTAVerticalFlip, TTARotate90, TTACompose
from itertools import product

from effdet import get_efficientdet_config, EfficientDet, DetBenchEval
from effdet.efficientdet import HeadNet
from models import fasterrcnn_resnet_fpn, get_effdet
from utils import save_dict, MyThresh, wbf_optimize

from PIL import Image
from matplotlib import pyplot as plt
import cv2

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument("--network", default="fasterrcnn", type=str,  choices=['fasterrcnn', 'effdet'])
parser.add_argument("--backbone", default="resnet152", type=str,  choices=['ed0', 'ed1', 'ed2', 'ed3', 'ed4', 'ed5', 'ed6', 'ed7', 'resnet50', 'resnet101', 'resnet152'])
parser.add_argument("--img-size", default=1024, type=int)
parser.add_argument("--batch-size", default=8, type=int)
parser.add_argument("--workers", default=8, type=int)
parser.add_argument("--folds", nargs="+", type=int)
parser.add_argument("--use-amp", default=False, type=lambda x: (str(x).lower() == "true"))
args = parser.parse_args()

if args.network == 'fasterrcnn':
    args.use_amp = False
else:
    args.use_amp = True

import warnings
warnings.filterwarnings("ignore")

def collate_fn(batch):
    return tuple(zip(*batch))

if __name__ == "__main__":
    df_train = pd.read_csv('/kaggle/input/ghana-crop-disease/Train.csv')
    df = pd.read_csv('/kaggle/input/ghana-crop-disease/Test.csv')

    class_map = {i:v for i, v in sorted(df_train['class'].unique().tolist())}
    predictions = []

    ground_truth = {}
    box_pred = {}
    score_pred = {}
    label_pred = {}

    tta_transforms = []
    for tta_combination in product([TTAHorizontalFlip(args.img_size), None], [TTAVerticalFlip(args.img_size), None], [TTARotate90(args.img_size), None]):
        tta_transforms.append(TTACompose([tta_transform for tta_transform in tta_combination if tta_transform]))

    for fold in args.folds:
        valid_df = df[['Image_ID', 'class']].copy()

        valid_dataset = CropTestset(df=valid_df, img_size=args.img_size, root_dir='/kaggle/input/ghana-crop-disease/images')
        valid_loader = DataLoader(valid_dataset, batch_size=args.batch_size, shuffle=False, num_workers=args.workers, collate_fn=collate_fn)

        for image_id in list(np.unique(valid_df.Image_ID.values)):
            bbxs = []
            tmp_df = valid_df.loc[valid_df['Image_ID']==image_id]
            for _, row in tmp_df.iterrows():
                bbxs.append([float(row['xmin']),float(row['ymin']),float(row['xmax']),float(row['ymax'])])
            
            ground_truth[image_id] = np.array(bbxs)
            box_pred[image_id] = []
            score_pred[image_id] = []
            label_pred[image_id] = []

        if args.network == 'fasterrcnn':
            model = fasterrcnn_resnet_fpn(backbone_name=args.backbone, pretrained=False, pretrained_backbone=False)
            in_features = model.roi_heads.box_predictor.cls_score.in_features
            model.roi_heads.box_predictor = FastRCNNPredictor(in_features, 2)
            model = model.cuda()
        elif args.network == 'effdet':
            model = get_effdet(args.backbone, num_classes=1, img_size=args.img_size, mode='valid', pretrained=False, pretrained_backbone=False)
            model = model.cuda()
            if args.use_amp:
                model = amp.initialize(model, opt_level='O1')
        else:
            raise ValueError('NETWORK')

        CHECKPOINT = 'checkpoints/{}_{}_{}_fold0.pth'.format(args.network, args.backbone, args.img_size)
        checkpoint = torch.load(CHECKPOINT)
        if args.network == 'effdet':
            model.model.load_state_dict(checkpoint)
        else:
            model.load_state_dict(checkpoint)

        del checkpoint
        gc.collect()

        model.eval()

        for images, image_ids in tqdm(valid_loader):
            if args.network == 'fasterrcnn':
                images = list(image.cuda() for image in images)
                for tta_transform in tta_transforms:
                    with torch.set_grad_enabled(False):
                        outputs = model(tta_transform.fasterrcnn_augment(images.copy()))
                    for image_id, o in zip(image_ids, outputs):
                        boxes = o['boxes'].data.cpu().numpy()
                        scores = o['scores'].data.cpu().numpy()
                        labels = o['class'].data.cpu().numpy()

                        boxes = tta_transform.deaugment_boxes(boxes)

                        scores *= 0.8   # normalize to efficientdet score

                        idxs = np.where(scores>0.2)[0]
                        boxes = boxes[idxs]
                        scores = scores[idxs]
                        labels = labels[idxs]

                        if boxes.shape[0] > 0:
                            boxes /= float(args.img_size)
                            boxes = boxes.clip(min=0, max=1)

                        box_pred[image_id].append(boxes.tolist())
                        score_pred[image_id].append(scores.tolist())
                        label_pred[image_id].append(labels.tolist())

                        for score, bbox, label in zip(score_pred, box_pred, label_pred):
                    
                            predictions.append({
                                'Image_ID': image_id,
                                'confidence': score[image_id].item(),
                                'class': class_map[label_pred[image_id]],
                                'ymin': bbox[image_id][1],
                                'xmin': bbox[image_id][0],
                                'ymax': bbox[image_id][3],
                                'xmax': bbox[image_id][2]
                            })

            else:
                images = torch.stack(images)
                images = images.cuda()

                for tta_transform in tta_transforms:
                    with torch.set_grad_enabled(False):
                        dets = model(tta_transform.effdet_augment(images.clone()), torch.tensor([1]*images.shape[0]).float().cuda())
                    for det, image_id in zip(dets, image_ids):
                        boxes = det.detach().cpu().numpy()[:,:4]
                        scores = det.detach().cpu().numpy()[:,4]
                        boxes[:, 2] = boxes[:, 2] + boxes[:, 0]
                        boxes[:, 3] = boxes[:, 3] + boxes[:, 1]
                        
                        boxes = tta_transform.deaugment_boxes(boxes)

                        labels = np.ones_like(scores)
                        idxs = np.where(scores>0.2)[0]
                        boxes = boxes[idxs]
                        scores = scores[idxs]
                        labels = labels[idxs]

                        if boxes.shape[0] > 0:
                            boxes /= float(args.img_size)
                            boxes = boxes.clip(min=0, max=1)

                        box_pred[image_id].append(boxes.tolist())
                        score_pred[image_id].append(scores.tolist())
                        label_pred[image_id].append(labels.tolist())

        del model
        gc.collect()

    mythreshs = []
    cnt = 0
    if args.network == 'fasterrcnn':
        for pp_threshold in np.arange(0.3, 0.4, 0.02):
            for nms_threshold in np.arange(0.5, 0.6, 0.02):
                for box_threshold in np.arange(0.32, 0.44, 0.02):
                    mythreshs.append(MyThresh(cnt, 0, nms_threshold, box_threshold, pp_threshold))
                    cnt += 1
    else:
        for pp_threshold in np.arange(0.26, 0.38, 0.02):
            for nms_threshold in np.arange(0.48, 0.6, 0.02):
                for box_threshold in np.arange(0.32, 0.44, 0.02):
                    mythreshs.append(MyThresh(cnt, 0, nms_threshold, box_threshold, pp_threshold))
                    cnt += 1
    for i in range(len(mythreshs)):
        mythreshs[i].total = cnt
    with Pool(processes=12) as pool:
        results = [pool.apply_async(wbf_optimize, args=(mt, ground_truth, box_pred, score_pred, label_pred)) for mt in mythreshs]
        results = [ret.get() for ret in results]
    pool.close()
    idx = np.argmax(np.array(results))
    print('-'*30)
    print('[Best box threshold]: {:.2f}'.format(mythreshs[idx].box_thresh))
    print('[Best nms threshold]: {:.2f}'.format(mythreshs[idx].nms_thresh))
    print('[Best pp threshold]: {:.2f}'.format(mythreshs[idx].pp_threshold))
    print('[AP]: {:.5f}'.format(results[idx]))
    
    all_pred = []
    all_pred.append(pd.DataFrame(predictions))
    final_sub = pd.concat(all_pred, ignore_index=True)
    final_sub.to_csv('final_sub.csv', index=False)

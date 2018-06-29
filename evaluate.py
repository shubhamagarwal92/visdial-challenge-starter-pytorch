import argparse
import datetime
import gc
import json
import os
from tqdm import tqdm

import torch
from torch.autograd import Variable
from torch.utils.data import DataLoader

from dataloader import VisDialDataset
from utils.metrics import process_ranks

parser = argparse.ArgumentParser()
VisDialDataset.add_cmdline_args(parser)

parser.add_argument_group('Evaluation related arguments')
parser.add_argument('-load_path', default='checkpoints/model.pth',
                        help='Checkpoint to load path from')
parser.add_argument('-split', default='val', choices=['val', 'test'],
                        help='Split to evaluate on')
parser.add_argument('-use_gt', action='store_true',
                        help='Whether to use ground truth for retrieving ranks')
parser.add_argument('-batch_size', default=12, type=int, help='Batch size')
parser.add_argument('-gpuid', default=0, type=int, help='GPU id to use')

parser.add_argument_group('Submission related arguments')
parser.add_argument('-save_ranks', action='store_true',
                        help='Whether to save retrieved ranks')
parser.add_argument('-save_path', default='logs/ranks.json',
                        help='Path of json file to save ranks')

# ----------------------------------------------------------------------------
# input arguments and options
# ----------------------------------------------------------------------------

args = parser.parse_args()
if args.use_gt:
    if args.split == 'test':
        print("Warning: No ground truth for test split, changing use_gt to False.")
        args.use_gt = False
    elif args.split == 'val' and args.save_ranks:
        print("Warning: Cannot generate submission json if use_gt is True.")
        args.save_ranks = False

# seed for reproducibility
torch.manual_seed(1234)

# set device and default tensor type
if args.gpuid >= 0:
    torch.cuda.manual_seed_all(1234)
    torch.cuda.set_device(args.gpuid)

# ----------------------------------------------------------------------------
# read saved model and args
# ----------------------------------------------------------------------------

components = torch.load(args.load_path)
model_args = components['encoder'].args
model_args.gpuid = args.gpuid
model_args.batch_size = args.batch_size

# this is required by dataloader
args.img_norm = components['encoder'].args.img_norm

# set this because only late fusion encoder is supported yet
args.concat_history = True

for arg in vars(args):
    print('{:<20}: {}'.format(arg, getattr(args, arg)))

# ----------------------------------------------------------------------------
# loading dataset wrapping with a dataloader
# ----------------------------------------------------------------------------

dataset = VisDialDataset(args, [args.split])
dataloader = DataLoader(dataset,
                        batch_size=args.batch_size,
                        shuffle=False,
                        collate_fn=dataset.collate_fn)

# ----------------------------------------------------------------------------
# setup the model
# ----------------------------------------------------------------------------

encoder = components['encoder']
decoder = components['decoder']
print("Loaded model from {}".format(args.load_path))

if args.gpuid >= 0:
    encoder = encoder.cuda()
    decoder = decoder.cuda()

# ----------------------------------------------------------------------------
# evaluation
# ----------------------------------------------------------------------------

print("Evaluation start time: {}".format(
    datetime.datetime.strftime(datetime.datetime.utcnow(), '%d-%b-%Y-%H:%M:%S')))
encoder.eval()
decoder.eval()

if args.use_gt:
    all_ranks = []
    for i, batch in enumerate(tqdm(dataloader)):
        if args.gpuid >= 0:
            for key in batch:
                if not isinstance(batch[key], list):
                    batch[key] = Variable(batch[key].cuda(), volatile=True)

        enc_out = encoder(batch)
        scores = decoder(enc_out, batch).data
        gt_pos = batch['ans_ind'].data.view(-1, 1)
        gt_score = scores.gather(1, gt_pos)
        ranks = scores.gt(gt_score.expand_as(scores))
        all_ranks.append(ranks.sum(1) + 1)
    all_ranks = torch.stack(all_ranks, 0)
    process_ranks(all_ranks)
    gc.collect()
else:
    ranks_json = []
    for i, batch in enumerate(tqdm(dataloader)):
        if args.gpuid >= 0:
            for key in batch:
                if not isinstance(batch[key], list):
                    batch[key] = Variable(batch[key].cuda(), volatile=True)

        enc_out = encoder(batch)
        dec_out = decoder(enc_out, batch)
        # sort in descending order - largest score gets highest rank
        sorted_ranks, ranked_idx = dec_out.data.sort(1, descending=True)

        # convert from ranked_idx to ranks
        ranks = ranked_idx.clone().fill_(0)
        for i in range(ranked_idx.size(0)):
            for j in range(100):
                ranks[i][ranked_idx[i][j]] = j
        ranks = ranks + 1
        ranks = ranks.view(-1, 10, 100)

        for i in range(len(batch['img_fnames'])):
            # cast into types explicitly to ensure no errors in schema
            if args.split == 'test':
                ranks_json.append({
                    'image_id': int(batch['img_fnames'][i][-16:-4]),  # convert path to id
                    'round_id': int(batch['num_rounds'][i]),
                    'ranks': list(ranks[i][batch['num_rounds'][i] - 1])
                })
            else:
                for j in range(len(batch['num_rounds'][i])):
                    ranks_json.append({
                        'image_id': int(batch['img_fnames'][i][-16:-4]),
                        'round_id': int(j + 1),
                        'ranks': list(ranks[i][j])
                    })
        gc.collect()

if args.save_ranks:
    print("Writing ranks to {}".format(args.save_path))
    os.makedirs(os.path.dirname(args.save_path), exist_ok=True)
    json.dump(ranks_json, open(args.save_path, 'w'))

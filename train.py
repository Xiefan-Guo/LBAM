import os
import argparse

import torch
import torch.nn as nn
import torch.optim as optim
import torch.backends.cudnn as cudnn
from torch.utils.data import DataLoader
from torchvision.utils import save_image

from src.dataset import ImageDataset
from src.generator import LBAM
from src.discriminator import Discriminator
from src.utils import VGG16FeatureExtractor
from src.loss import generator_loss, discriminator_loss, calc_gradient_penalty

import torch.multiprocessing
torch.multiprocessing.set_sharing_strategy('file_system')

parser = argparse.ArgumentParser()
parser.add_argument('--num_workers', type=int, default=4, help='workers for dataloader')
parser.add_argument('--save_dir', type=str, default='snapshots/CelebA', help='path for saving models')
parser.add_argument('--log_dir', type=str, default='runs/CelebA', help='log with tensorboardX')
parser.add_argument('--pre_trained', type=str, default='', help='the path of checkpoint')
parser.add_argument('--save_interval', type=int, default=20, help='interval between model save')
parser.add_argument('--lr', type=float, default=0.0001)
parser.add_argument('--b1', type=float, default=0.5)
parser.add_argument('--b2', type=float, default=0.9)
parser.add_argument('--lambda_gp', type=float, default=10.0)
parser.add_argument('--batch_size', type=int, default=16)
parser.add_argument('--load_size', type=int, default=350, help='image loading size')
parser.add_argument('--crop_size', type=int, default=256, help='image training size')
parser.add_argument('--image_root', type=str, default='')
parser.add_argument('--mask_root', type=str, default='')
parser.add_argument('--start_iter', type=int, default=1, help='start iter')
parser.add_argument('--train_epochs', type=int, default=500, help='training epochs')
args = parser.parse_args()

# -----------
# GPU setting
# -----------
os.environ['CUDA_VISIBLE_DEVICES'] = '2, 4, 5, 6'
# CUDA_VISIBLE_DEVICES='2, 4, 5, 6' python -u train.py | tee -a ckpt-path
is_cuda = torch.cuda.is_available()
if is_cuda:
    print('Cuda is available')
    cudnn.enable = True
    cudnn.benchmark = True

# -----------------------
# samples and checkpoints
# -----------------------
if not os.path.exists(args.save_dir):
    os.makedirs('{:s}/images'.format(args.save_dir))
    os.makedirs('{:s}/ckpt'.format(args.save_dir))

# ----------
# Dataloader
# ----------
load_size = (args.load_size, args.load_size)
crop_size = (args.crop_size, args.crop_size)
image_dataset = ImageDataset(args.image_root, args.mask_root, load_size, crop_size)
data_loader = DataLoader(
    image_dataset,
    batch_size=args.batch_size,
    shuffle=True,
    num_workers=args.num_workers,
    drop_last=False,
    pin_memory=True
)

# -----
# model
# -----
generator = LBAM(4, 3)
discriminator = Discriminator(3)
extractor = VGG16FeatureExtractor()

# ----------
# load model
# ----------
start_iter = args.start_iter
if args.pre_trained != '':

    ckpt_dict_load = torch.load(args.pre_trained)
    start_iter = ckpt_dict_load['n_iter']
    generator.load_state_dict(ckpt_dict_load['generator'])
    discriminator.load_state_dict(ckpt_dict_load['discriminator'])

    print('Starting from iter ', start_iter)


# -----------------------------
# DataParallel and model cuda()
# -----------------------------
num_GPUS = 0
if is_cuda:

    generator = generator.cuda()
    discriminator = discriminator.cuda()
    extractor = extractor.cuda()

    num_GPUS = torch.cuda.device_count()
    print('The number of GPU is ', num_GPUS)

    if num_GPUS > 1:

        generator = nn.DataParallel(generator, device_ids=range(num_GPUS))
        discriminator = nn.DataParallel(discriminator, device_ids=range(num_GPUS))


# ---------
# optimizer
# ---------
G_optimizer = optim.Adam(generator.parameters(), lr=args.lr, betas=(args.b1, args.b2))
D_optimizer = optim.Adam(discriminator.parameters(), lr=args.lr * 0.1, betas=(args.b1, args.b2))

# --------
# Training
# --------
print('Start train...')
count = 0
for epoch in range(start_iter, args.train_epochs + 1):

    generator.train()
    for _, (input_images, ground_truths, masks) in enumerate(data_loader):

        count += 1

        if is_cuda:
            input_images, ground_truths, masks = input_images.cuda(), ground_truths.cuda(), masks.cuda()

        # ---------
        # Generator
        # ---------
        G_optimizer.zero_grad()

        outputs = generator(input_images, masks)

        d_fake = discriminator(outputs, masks).mean()

        G_loss = generator_loss(args.log_dir + '/Generator', input_images[:, 0:3, :, :],
                                masks, outputs, ground_truths, count, extractor, d_fake)
        G_loss.backward()
        G_optimizer.step()

        # -------------
        # Discriminator
        # -------------
        D_optimizer.zero_grad()

        d_real = discriminator(ground_truths, masks).mean()
        d_fake = discriminator(outputs.detach(), masks).mean()
        gp = calc_gradient_penalty(discriminator, ground_truths, outputs.detach(), masks, is_cuda, args.lambda_gp)

        D_loss = discriminator_loss(args.log_dir + '/Discriminator', count, d_real, d_fake, gp)

        D_loss.backward()
        D_optimizer.step()

        print('[Epoch %d/%d] [Batch %d/%d] [count %d] [G_loss %f] [D_loss %f]' %
                  (epoch, args.train_epochs, _ + 1, len(data_loader), count, G_loss, G_loss))

    # ----------
    # save model
    # ----------
    if epoch % args.save_interval == 0:

        ckpt_dict_save = {'n_iter': epoch + 1}
        if num_GPUS > 1:
            ckpt_dict_save['generator'] = generator.module.state_dict()
            ckpt_dict_save['discriminator'] = discriminator.module.state_dict()
        else:
            ckpt_dict_save['generator'] = generator.state_dict()
            ckpt_dict_save['discriminator'] = discriminator.state_dict()

        torch.save(ckpt_dict_save, '{:s}/ckpt/model_{:d}.pth'.format(args.save_dir, epoch))

    # ----------------
    # save train image
    # ----------------
    if epoch % args.save_interval == 0:

        sizes = ground_truths.size()
        save_images = torch.Tensor(sizes[0] * 4, sizes[1], sizes[2], sizes[3])
        damaged_images = ground_truths * masks + (1 - masks)
        for i in range(sizes[0]):
            save_images[4 * i] = 1 - masks[i]
            save_images[4 * i + 1] = damaged_images[i]
            save_images[4 * i + 2] = outputs[i]
            save_images[4 * i + 3] = ground_truths[i]

        save_image(save_images, '{:s}/images/image_{:d}.png'.format(args.save_dir, epoch),
                       nrow=args.batch_size)




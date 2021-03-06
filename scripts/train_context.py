import torch.nn.functional as F
import argparse
import random
import torch
import time
import os

from datasets.cityscapes_loader import CityscapesLoader
from tensorboardX import SummaryWriter
from torch.autograd import Variable
from torch.utils import data
from tqdm import tqdm

from scripts.loss import SemanticEncodingLoss, bootstrapped_cross_entropy2d
from scripts.utils import update_aggregated_weight_average
from models.rfmobilenetv2context import MobileNetV2Context
from scripts.utils import cosine_annealing_lr
from scripts.utils import poly_topk_scheduler
from scripts.utils import set_optimizer_lr
from scripts.cyclical_lr import CyclicLR
from scripts.metrics import RunningScore
from modules import InPlaceABNWrapper
from datasets.augmentations import *
from functools import partial


def train(args, data_root, save_root):
    weight_dir = "{}weights/".format(save_root)
    log_dir = "{}logs/MobileNetV2Context-{}".format(save_root, time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime()))

    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    # 1. Setup Augmentations
    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    net_h, net_w = int(args.img_rows*args.crop_ratio), int(args.img_cols*args.crop_ratio)

    augment_train = Compose([RandomHorizontallyFlip(), RandomSized((0.5, 0.75)),
                             RandomRotate(5), RandomCrop((net_h, net_w))])
    augment_valid = Compose([RandomHorizontallyFlip(), Scale((args.img_rows, args.img_cols)),
                             CenterCrop((net_h, net_w))])

    print("> # +++++++++++++++++++++++++++++++++++++++++++++++++++++++ #")
    print("> 0. Setting up DataLoader...")
    print("> # +++++++++++++++++++++++++++++++++++++++++++++++++++++++ #")
    train_loader = CityscapesLoader(data_root, gt="gtFine", is_transform=True, split='train',
                                    img_size=(args.img_rows, args.img_cols),
                                    augmentations=augment_train)
    valid_loader = CityscapesLoader(data_root, gt="gtFine", is_transform=True, split='val',
                                    img_size=(args.img_rows, args.img_cols),
                                    augmentations=augment_valid)

    n_classes = train_loader.n_classes

    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    # 2. Setup Metrics
    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    running_metrics = RunningScore(n_classes)

    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    # 4. Setup Model
    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    print("> # +++++++++++++++++++++++++++++++++++++++++++++++++++++++ #")
    print("> 1. Setting up Model...")

    model = MobileNetV2Context(n_class=19, in_size=(net_h, net_w), width_mult=1., out_sec=256, context=(32, 4),
                               norm_act=partial(InPlaceABNWrapper, activation="leaky_relu", slope=0.1))

    # np.arange(torch.cuda.device_count())
    model = torch.nn.DataParallel(model, device_ids=[0]).cuda()

    # 4.1 Setup Optimizer
    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    # Check if model has custom optimizer / loss
    if hasattr(model.module, 'optimizer'):
        optimizer = model.module.optimizer
    else:
        optimizer = torch.optim.SGD(model.parameters(), lr=args.l_rate, momentum=0.90,
                                    weight_decay=5e-4, nesterov=True)

    # 4.2 Setup Loss
    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    class_weight = None
    if hasattr(model.module, 'loss'):
        print('> Using custom loss')
        loss_fn = model.module.loss
    else:
        # loss_fn = cross_entropy2d

        class_weight = np.array([0.05570516, 0.32337477, 0.08998544, 1.03602707, 1.03413147, 1.68195437,
                                 5.58540548, 3.56563995, 0.12704978, 1.,         0.46783719, 1.34551528,
                                 5.29974114, 0.28342531, 0.9396095,  0.81551811, 0.42679146, 3.6399074,
                                 2.78376194], dtype=float)

        """
        class_weight = np.array([3.045384,  12.862123,   4.509889,  38.15694,  35.25279,  31.482613,
                                 45.792305,  39.694073,  6.0639296,  32.16484,  17.109228,   31.563286,
                                 47.333973,  11.610675,  44.60042,   45.23716,  45.283024,  48.14782,
                                 41.924667], dtype=float)/10.0
        """
        class_weight = torch.from_numpy(class_weight).float().cuda()
        se_loss = SemanticEncodingLoss(num_classes=19, ignore_label=250, alpha=0.20)
        ce_loss = bootstrapped_cross_entropy2d

    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    # 5. Resume Model
    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    best_iou = -100.0
    args.start_epoch = 0
    if args.resume is not None:
        full_path = "{}{}".format(weight_dir, args.resume)
        if os.path.isfile(full_path):
            print("> Loading model and optimizer from checkpoint '{}'".format(args.resume))

            checkpoint = torch.load(full_path)
            args.start_epoch = checkpoint['epoch']
            best_iou = checkpoint['best_iou']
            model.load_state_dict(checkpoint['model_state'])          # weights
            optimizer.load_state_dict(checkpoint['optimizer_state'])  # gradient state

            # for param_group in optimizer.param_groups:
            # s    param_group['lr'] = 1e-5

            del checkpoint
            print("> Loaded checkpoint '{}' (epoch {}, iou {})".format(args.resume,
                                                                       args.start_epoch,
                                                                       best_iou))

        else:
            print("> No checkpoint found at '{}'".format(args.resume))
    else:
        if args.pre_trained is not None:
            print("> Loading weights from pre-trained model '{}'".format(args.pre_trained))
            full_path = "{}{}".format(weight_dir, args.pre_trained)

            pre_weight = torch.load(full_path)
            pre_weight = pre_weight["model_state"]
            # pre_weight = pre_weight["state_dict"]

            model_dict = model.state_dict()

            pretrained_dict = {k: v for k, v in pre_weight.items() if k in model_dict}
            model_dict.update(pretrained_dict)
            model.load_state_dict(model_dict)

            del pre_weight
            del model_dict
            del pretrained_dict

    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    # 3. Setup visdom for visualization
    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    writer = None
    if args.visdom:
        writer = SummaryWriter(log_dir=log_dir, comment="MobileNetV2Context")

    if args.visdom:
        dummy_input = Variable(torch.rand(1, 3, net_h, net_w).cuda(), requires_grad=True)
        writer.add_graph(model, dummy_input)

    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    # 6. Train Model
    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    print("> 2. Model Training start...")
    train_loader = data.DataLoader(train_loader, batch_size=args.batch_size, num_workers=6, shuffle=True)
    valid_loader = data.DataLoader(valid_loader, batch_size=args.batch_size, num_workers=6)

    num_batches = int(math.ceil(len(train_loader.dataset.files[train_loader.dataset.split]) /
                                float(train_loader.batch_size)))

    lr_period = 20 * num_batches
    swa_weights = model.state_dict()

    # scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=0.90)
    # scheduler = CyclicLR(optimizer, base_lr=1.0e-3, max_lr=6.0e-3, step_size=2*num_batches)
    # scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=32, gamma=0.1)
    # scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=[30, 80], gamma=0.1)

    topk_init = 512
    # topk_multipliers = [64, 128, 256, 512]
    for epoch in np.arange(args.start_epoch, args.n_epoch):
        # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
        # 7.1 Mini-Batch Learning
        # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
        # print("> Training Epoch [%d/%d]:" % (epoch + 1, args.n_epoch))
        model.train()

        last_loss = 0.0
        topk_base = topk_init
        pbar = tqdm(np.arange(num_batches))
        for train_i, (images, labels) in enumerate(train_loader):  # One mini-Batch data, One iteration
            full_iter = (epoch * num_batches) + train_i + 1

            # poly_lr_scheduler(optimizer, init_lr=args.l_rate, iter=full_iter,
            #                   lr_decay_iter=1, max_iter=args.n_epoch*num_batches, power=0.9)

            batch_lr = args.l_rate * cosine_annealing_lr(lr_period, full_iter)
            optimizer = set_optimizer_lr(optimizer, batch_lr)

            topk_base = poly_topk_scheduler(init_topk=topk_init, iter=full_iter, topk_decay_iter=1,
                                            max_iter=args.n_epoch*num_batches, power=0.95)

            images = Variable(images.cuda(), requires_grad=True)   # Image feed into the deep neural network
            se_labels = se_loss.unique_encode(labels)
            se_labels = Variable(se_labels.cuda(), requires_grad=False)
            ce_labels = Variable(labels.cuda(), requires_grad=False)

            optimizer.zero_grad()
            enc1, enc2, net_out = model(images)  # Here we have 3 output for 3 loss

            topk = topk_base * 512
            if random.random() < 0.20:
                train_ce_loss = ce_loss(input=net_out, target=ce_labels, K=topk,
                                        weight=class_weight, size_average=True)
            else:
                train_ce_loss = ce_loss(input=net_out, target=ce_labels, K=topk,
                                        weight=None, size_average=True)
            
            train_se_loss1 = se_loss(predicts=enc1, enc_cls_target=se_labels, size_average=True)
            train_se_loss2 = se_loss(predicts=enc2, enc_cls_target=se_labels, size_average=True)

            train_loss = train_ce_loss + train_se_loss1 + train_se_loss2

            last_loss = train_loss.data[0]
            last_ce_loss = train_ce_loss.data[0]
            last_se_loss1 = train_se_loss1.data[0]
            last_se_loss2 = train_se_loss2.data[0]
            pbar.update(1)
            pbar.set_description("> Epoch [%d/%d]" % (epoch + 1, args.n_epoch))
            pbar.set_postfix(Loss=last_loss, CELoss=last_ce_loss,
                             SELoss1=last_se_loss1, SELoss2=last_se_loss2,
                             TopK=topk_base, LR=batch_lr)

            train_loss.backward()
            optimizer.step()

            if full_iter % lr_period == 0:
                swa_weights = update_aggregated_weight_average(model, swa_weights, full_iter, lr_period)
                state = {'model_state': swa_weights}
                torch.save(state, "{}{}_mobilenetv2context_swa_model.pkl".format(weight_dir, args.dataset))

            if (train_i + 1) % 31 == 0:
                loss_log = "Epoch [%d/%d], Iter: %d, Loss: \t %.4f, CELoss: \t %.4f, " \
                           "SELoss1: \t %.4f, SELoss2: \t%.4f, " % (epoch + 1, args.n_epoch,
                                                                    train_i + 1, last_loss,
                                                                    last_ce_loss,
                                                                    last_se_loss1,
                                                                    last_se_loss2)

                net_out = F.softmax(net_out, dim=1)
                pred = net_out.data.max(1)[1].cpu().numpy()
                gt = ce_labels.data.cpu().numpy()

                running_metrics.update(gt, pred)
                score, class_iou = running_metrics.get_scores()

                metric_log = ""
                for k, v in score.items():
                    metric_log += " {}: \t %.4f, ".format(k) % v
                running_metrics.reset()

                logs = loss_log + metric_log
                # print(logs)

                if args.visdom:
                    writer.add_scalar('Training/Losses', last_loss, full_iter)
                    writer.add_scalars('Training/Metrics', score, full_iter)
                    writer.add_text('Training/Text', logs, full_iter)

                    for name, param in model.named_parameters():
                        writer.add_histogram(name, param.clone().cpu().data.numpy(), full_iter)

        # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
        # 7.2 Mini-Batch Validation
        # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
        # print("> Validation for Epoch [%d/%d]:" % (epoch + 1, args.n_epoch))
        model.eval()

        mval_loss = 0.0
        vali_count = 0
        for i_val, (images, labels) in enumerate(valid_loader):
            vali_count += 1

            images = Variable(images.cuda(), volatile=True)
            ce_labels = Variable(labels.cuda(), requires_grad=False)

            enc1, enc2, net_out = model(images)  # Here we have 4 output for 4 loss

            topk = topk_base * 512
            val_loss = ce_loss(input=net_out, target=ce_labels, K=topk,
                               weight=None, size_average=False)

            mval_loss += val_loss.data[0]

            net_out = F.softmax(net_out, dim=1)
            pred = net_out.data.max(1)[1].cpu().numpy()
            gt = ce_labels.data.cpu().numpy()
            running_metrics.update(gt, pred)

        mval_loss /= vali_count

        loss_log = "Epoch [%d/%d] Loss: \t %.4f" % (epoch + 1, args.n_epoch, mval_loss)
        metric_log = ""
        score, class_iou = running_metrics.get_scores()
        for k, v in score.items():
            metric_log += " {} \t %.4f, ".format(k) % v
        running_metrics.reset()

        logs = loss_log + metric_log
        # print(logs)
        pbar.set_postfix(Train_Loss=last_loss, Vali_Loss=mval_loss, Vali_mIoU=score['Mean_IoU'])

        if args.visdom:
            writer.add_scalar('Validation/Losses', mval_loss, epoch)
            writer.add_scalars('Validation/Metrics', score, epoch)
            writer.add_text('Validation/Text', logs, epoch)

            for name, param in model.named_parameters():
                writer.add_histogram(name, param.clone().cpu().data.numpy(), epoch)

            # export scalar data to JSON for external processing
            # writer.export_scalars_to_json("{}/all_scalars.json".format(log_dir))

        if score['Mean_IoU'] >= best_iou:
            best_iou = score['Mean_IoU']
            state = {'epoch': epoch + 1,
                     "best_iou": best_iou,
                     'model_state': model.state_dict(),
                     'optimizer_state': optimizer.state_dict()}
            torch.save(state, "{}{}_mobilenetv2context_best_model.pkl".format(weight_dir, args.dataset))

        # scheduler.step()
        # scheduler.batch_step()
        pbar.close()

    if args.visdom:
        # export scalar data to JSON for external processing
        # writer.export_scalars_to_json("{}/all_scalars.json".format(log_dir))
        writer.close()
    print("> # +++++++++++++++++++++++++++++++++++++++++++++++++++++++ #")
    print("> Training Done!!!")
    print("> # +++++++++++++++++++++++++++++++++++++++++++++++++++++++ #")


if __name__ == '__main__':
    # +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++ #
    # 0. Hyper-params
    # +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++ #
    parser = argparse.ArgumentParser(description='Hyperparams')

    parser.add_argument('--dataset', nargs='?', type=str, default='cityscapes',
                        help='Dataset to use [\'cityscapes, mvd etc\']')
    parser.add_argument('--img_rows', nargs='?', type=int, default=512,
                        help='Height of the input image')
    parser.add_argument('--img_cols', nargs='?', type=int, default=1024,
                        help='Height of the input image')
    parser.add_argument('--n_epoch', nargs='?', type=int, default=200,
                        help='# of the epochs')
    parser.add_argument('--batch_size', nargs='?', type=int, default=7,
                        help='Batch Size')
    parser.add_argument('--l_rate', nargs='?', type=float, default=2.5e-3,
                        help='Learning Rate')
    parser.add_argument('--crop_ratio', nargs='?', type=float, default=0.875,
                        help='The ratio to crop the input image')
    parser.add_argument('--resume', nargs='?', type=str, default=None,
                        help='Path to previous saved model to restart from')
    parser.add_argument('--pre_trained', nargs='?', type=str, default="cityscapes_mobilenetv2context_best_model.pkl",
                        help='Path to pre-trained  model to init from')
    parser.add_argument('--visdom', nargs='?', type=bool, default=True,
                        help='Show visualization(s) on visdom | True by  default')

    # +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++ #
    # 1. Train the Deep Model
    # +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++ #
    os.environ["CUDA_VISIBLE_DEVICES"] = "1,0"
    data_path = "/zfs/zhang/Cityscapes"
    save_path = "/zfs/zhang/TrainLog/"
    train_args = parser.parse_args()
    train(train_args, data_path, save_path)

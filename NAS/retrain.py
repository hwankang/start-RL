import logging
import time
import datetime
from argparse import ArgumentParser

import torch
import torch.nn as nn
import os

import datasets
from macro import GeneralNetwork
from micro import MicroNetwork
from nni.nas.pytorch import enas
from nni.nas.pytorch.callbacks import (ArchitectureCheckpoint,
                                       LRSchedulerCallback,
                                       ModelCheckpoint)
from nni.nas.pytorch.fixed import apply_fixed_architecture
from nni.nas.pytorch.utils import AverageMeter
from utils import accuracy, reward_accuracy
import json


logger = logging.getLogger('nni')
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# NAS로 생성된 자식신경망 구조 학습 하기
def train(config, train_loader, model, optimizer, criterion, epoch, search_for):
    top1 = AverageMeter("top1")
    top5 = AverageMeter("top5")
    losses = AverageMeter("losses")

    cur_step = epoch * len(train_loader)
    cur_lr = optimizer.param_groups[0]["lr"]
    logger.info("Epoch %d LR %.6f", epoch, cur_lr)

    model.train()

    for step, (x, y) in enumerate(train_loader):
        x, y = x.to(device, non_blocking=True), y.to(device, non_blocking=True)
        bs = x.size(0)

        optimizer.zero_grad()
        if search_for == 'micro':
            logits, aux_logits = model(x)
        else:
            logits = model(x)
        loss = criterion(logits, y)
        if config.aux_weight > 0. and search_for == 'micro':
            loss += config.aux_weight * criterion(aux_logits, y)
        loss.backward()
        # gradient clipping
        nn.utils.clip_grad_norm_(model.parameters(), config.grad_clip)
        optimizer.step()

        acc = accuracy(logits, y, topk=(1, 5))
        losses.update(loss.item(), bs)
        top1.update(acc["acc1"], bs)
        top5.update(acc["acc5"], bs)

        if step % config.log_frequency == 0 or step == len(train_loader) - 1:
            logger.info(
                "Train: [{:3d}/{}] Step {:03d}/{:03d} Loss {losses.avg:.3f} "
                "Prec@(1,5) ({top1.avg:.1%}, {top5.avg:.1%})".format(
                    epoch + 1, config.retrain_epochs, step, len(train_loader) - 1, losses=losses,
                    top1=top1, top5=top5))

        cur_step += 1

    logger.info("Train: [{:3d}/{}] Final Prec@1 {:.4%}".format(epoch + 1, config.retrain_epochs, top1.avg))

# 학습된 모델 검증
def validate(config, valid_loader, model, criterion, epoch, cur_step):
    top1 = AverageMeter("top1")
    top5 = AverageMeter("top5")
    losses = AverageMeter("losses")

    model.eval()

    with torch.no_grad():
        for step, (X, y) in enumerate(valid_loader):
            X, y = X.to(device, non_blocking=True), y.to(device, non_blocking=True)
            bs = X.size(0)

            logits = model(X)
            loss = criterion(logits, y)

            acc = accuracy(logits, y, topk=(1, 5))
            losses.update(loss.item(), bs)
            top1.update(acc["acc1"], bs)
            top5.update(acc["acc5"], bs)

            if step % config.log_frequency == 0 or step == len(valid_loader) - 1:
                logger.info(
                    "Valid: [{:3d}/{}] Step {:03d}/{:03d} Loss {losses.avg:.3f} "
                    "Prec@(1,5) ({top1.avg:.1%}, {top5.avg:.1%})".format(
                        epoch + 1, config.retrain_epochs, step, len(valid_loader) - 1, losses=losses,
                        top1=top1, top5=top5))

    logger.info("Valid: [{:3d}/{}] Final Prec@1 {:.4%}".format(epoch + 1, config.retrain_epochs, top1.avg))

    return top1.avg

if __name__ == "__main__":
    parser = ArgumentParser("enas")
    parser.add_argument("--search-for", choices=["macro", "micro"], default="macro")
    parser.add_argument("--retrain-epochs", default=600, type=int)
    parser.add_argument("--batch-size", default=128, type=int)
    parser.add_argument("--workers", default=4)
    parser.add_argument("--aux-weight", default=0.4, type=float)
    parser.add_argument("--grad-clip", default=5., type=float)
    parser.add_argument("--log-frequency", default=10, type=int)
    parser.add_argument("--num-layers", default=6, type=int)
    parser.add_argument("nas_result")
    parser.add_argument("result_file")
    args = parser.parse_args()

    dataset_train, dataset_valid = datasets.get_dataset("cifar10")

    if args.search_for == "macro":
        model = GeneralNetwork()
    elif args.search_for == "micro":
        model = MicroNetwork(args.num_layers, out_channels=20, num_nodes=5, dropout_rate=0.1, use_aux_heads=True)
    apply_fixed_architecture(model, args.nas_result) # 1. 학습 결과 신경망 구조에 적용
    model.to(device)

    # 2. 모델을 학습하고 평가 하기 위한 설정들
    criterion = nn.CrossEntropyLoss()
    criterion.to(device)

    optimizer = torch.optim.SGD(model.parameters(), 0.05, momentum=0.9, weight_decay=1.0E-4)
    lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, args.retrain_epochs, eta_min=0.001)
    train_loader = torch.utils.data.DataLoader(dataset_train, batch_size=args.batch_size, shuffle=True, num_workers=args.workers, pin_memory=True)
    valid_loader = torch.utils.data.DataLoader(dataset_valid, batch_size=args.batch_size, shuffle=False, num_workers=args.workers, pin_memory=True)

    # 3. 모델 학습 
    best_top1 = 0.
    for epoch in range(args.retrain_epochs):
        train(args, train_loader, model, optimizer, criterion, epoch, args.search_for)
        cur_step = (epoch + 1) * len(train_loader)
        top1 = validate(args, valid_loader, model, criterion, epoch, cur_step)
        best_top1 = max(best_top1, top1)
        if top1 == best_top1:
            state_dict = model.state_dict()
            torch.save(state_dict, args.result_file) # 5. 가장 성능이 높은 모델 저장하기
        lr_scheduler.step()

    logger.info("Final best Prec@1 = {:.4%}".format(best_top1))

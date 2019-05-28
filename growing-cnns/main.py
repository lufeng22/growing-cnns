import argparse
import os
import random
import time
import warnings
import sys
import json
import shutil

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
import torch.multiprocessing as mp
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import torchvision.models as models

import utils
from growth_controller import GrowthController

experimentDir = None

def main(args):

    # Load settings file
    with open(args.settings_file, 'r') as settingsFile:
        settings = json.load(settingsFile)
    experiment_type = 'growing' if settings['growing'] else 'static'
    
    # Create experiments directory if it doesn't already exist
    experiments_dir = os.path.join(os.path.dirname(__file__), 'experiments')
    if not args.quiet and not os.path.isdir(experiments_dir):
        os.makedirs(experiments_dir)

    # Create experiment directory to store results and model, if args.quiet
    # is false and we are training, not evaluating a model.
    global experimentDir
    experimentDir = os.path.join(experiments_dir, args.name)
    if os.path.isdir(experimentDir) and not args.quiet and args.modelPath is None:
        if os.path.isdir(experimentDir):
            print("Experiment with name '%s' already exists!" % args.name)
            exit()
        else:
            os.makedirs(experimentDir)
            permanent_settings_file = os.path.join(experimentDir, args.name + '_settings.json')
            shutil.copyfile(args.settings_file, permanent_settings_file)

    # Set seed and other cuda settings
    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
        warnings.warn('You have chosen to seed training. '
                      'This will turn on the CUDNN deterministic setting, '
                      'which can slow down your training considerably! '
                      'You may see unexpected behavior when restarting '
                      'from checkpoints.')
    num_classes = 10 # Temporary
    cudnn.benchmark = True
    torch.cuda.set_device(args.gpu)

    # Define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().cuda(args.gpu)

    # Data loading
    IMAGENET_MEANS = (0.485, 0.456, 0.406)
    IMAGENET_STDS = (0.229, 0.224, 0.225)

    # Data transformations
    transform_train = transforms.Compose([
        #transforms.Resize(256),      # Note: These commented transformations should be added for imagenet when the time comes.
        #transforms.RandomCrop(224),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize(IMAGENET_MEANS, IMAGENET_STDS),
    ])
    transform_val = transforms.Compose([
        #transforms.Resize(256),
        #transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize(IMAGENET_MEANS, IMAGENET_STDS),
    ])

    # Load dataset
    train_dataset = datasets.CIFAR10(root='../data', train=True, download=True, transform=transform_train)
    val_dataset = datasets.CIFAR10(root='../data', train=False, download=True, transform=transform_val)

    train_sampler = None

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=settings['batch_size'], shuffle=(train_sampler is None),
        num_workers=args.workers, pin_memory=True, sampler=train_sampler)
    val_loader = torch.utils.data.DataLoader(
        val_dataset, batch_size=settings['batch_size'], shuffle=False,
        num_workers=args.workers, pin_memory=True)

    # Run training/evaluation
    if experiment_type == "growing":
        results = run_growing(num_classes, args, settings, criterion, train_loader, val_loader)
    else:
        results = run_static(num_classes, args, settings, criterion, train_loader, val_loader)

    # Write out results to log if this is a training session and not quiet mode
    if args.modelPath is None and not args.quiet:
        logPath = os.path.join(experimentDir, '%s.log' % args.name)
        with open(logPath, 'w') as logFile:
            json.dump(results, logFile, indent=4)

def run_static(num_classes, args, settings, criterion, train_loader, val_loader):

    # Create model
    if settings['pretrained']:
        print("=> using pre-trained model '{}'".format(settings['arch']))
        model = models.__dict__[settings['arch']](pretrained=True, num_classes=num_classes)
    else:
        print("=> creating model '{}'".format(settings['arch']))
        model = models.__dict__[settings['arch']](num_classes=num_classes)

    model = model.cuda(args.gpu)

    # Create optimizer
    optimizer = torch.optim.SGD(model.parameters(), settings['initial_learning_rate'],
                               momentum=settings['momentum'],
                               weight_decay=settings['weight_decay'])

    if args.modelPath is not None:
        validate(val_loader, model, criterion, 0, args, [])
        return

    # Results object to write out
    results = {}
    results['train_iterations_per_epoch'] = len(train_loader)
    train_results = []
    validate_results = []

    # Training loop
    best_acc1 = 0
    for epoch in range(settings['epochs']):
        if epoch > 0 and epoch % settings['lr_decay_epoch_step'] == 0:
            utils.adjust_learning_rate(optimizer, settings['lr_decay_epoch_ratio'])

        # Train for one epoch
        train(train_loader, model, criterion, optimizer, epoch, args, train_results)

        # Evaluate on validation set
        acc1 = validate(val_loader, model, criterion, epoch, args, validate_results)

        # Remember best acc@1 and save checkpoint
        is_best = acc1 > best_acc1
        best_acc1 = max(acc1, best_acc1)
        if not args.quiet:
            utils.save_checkpoint(experimentDir, args.name, {
                'epoch': epoch + 1,
                'arch': settings['arch'],
                'state_dict': model.state_dict(),
                'best_acc1': best_acc1,
                'optimizer': optimizer.state_dict(),
            }, is_best)

    results['train_results'] = list(train_results)
    results['validate_results'] = list(validate_results)
    return results

def run_growing(num_classes, args, settings, criterion, train_loader, val_loader):
    """initial_config = [('C', 64), ('M',), ('C', 128), ('R_Bottleneck', 128), 
            ('C', 128), ('M',), ('C', 256), ('M',), ('C', 512), ('M',), 
            ('C', 512), ('M',)]
    growth_steps = []
    growth_steps.append([(1, 'C', 64), (6, 'C', 128), (9, 'C', 256), (12, 'C', 512), (15, 'C', 512)])
    growth_steps.append([(10, 'C', 256), (14, 'C', 512), (18, 'C', 512)])
    """
    
    initial_config = [('C', 64), ('R_Bottleneck', 64), ('M',), ('C', 128),
            ('R_Bottleneck', 128), ('M',),
                        ('C', 256), ('R_Bottleneck', 256), ('M',), ('C', 512),
                        ('R_Bottleneck', 512), ('M',), 
                        ('C', 512), ('R_Bottleneck', 512), ('M',)]
    growth_steps = []
    growth_steps.append([(2, 'R_Bottleneck', 64), (6, 'R_Bottleneck', 128), (10,
                            'R_Bottleneck', 256), (14, 'R_Bottleneck', 512), (18, 'R_Bottleneck', 512)])
    growth_steps.append([(3, 'R_Bottleneck', 64), (8, 'R_Bottleneck', 128), (13,
                            'R_Bottleneck', 256), (18, 'R_Bottleneck', 512), (23,
                            'R_Bottleneck', 512)])
    
    # Create growth controller
    growth_controller = GrowthController(initial_config, growth_steps, num_classes, settings['batch_normalization'])
    total_epoch = 0

    # Only evaluate model, no training
    if args.modelPath is not None:
        checkpoint = torch.load(args.modelPath)

        total_steps = checkpoint['growth_step']
        for growth_step in range(total_steps + 1):
            if growth_step == 0:
                model = growth_controller.step()
            else:
                model = growth_controller.step(old_model=model)

            model = model.cuda(args.gpu)

        model.load_state_dict(checkpoint['state_dict'])
        validate(val_loader, model, criterion, 0, args, [], growth_step=total_steps)
        return

    # Results object to write out
    results = {}
    results['train_iterations_per_epoch'] = len(train_loader)
    train_results = []
    validate_results = []

    # Outer training loop
    best_acc1 = 0
    for i in range(len(growth_controller.growth_steps) + 1):

        # Create model and optimizer
        print("=> creating growth iteration %d for model" % i)
        if i == 0:
            model = growth_controller.step()
        else:
            model = growth_controller.step(old_model=model)

        model = model.cuda(args.gpu)

        optimizer_params = utils.get_initial_optimizer_params(model,
                growth_controller.growth_history,
                settings['initial_learning_rate'],
                settings['lr_decay_growth_ratio'], 
                i)
        optimizer = torch.optim.SGD(optimizer_params,
                momentum=settings['momentum'],
                weight_decay=settings['weight_decay'])

        # Inner training loop
        for epoch in range(settings['epochs_per_step']):
            if epoch > 0 and epoch % settings['lr_decay_epoch_step'] == 0:
                utils.adjust_learning_rate(optimizer,
                settings['lr_decay_epoch_ratio'])

            # train for one epoch
            train(train_loader, model, criterion, optimizer, epoch, args, train_results, growth_step=i)

            # evaluate on validation set
            acc1 = validate(val_loader, model, criterion, epoch, args, validate_results, growth_step=i)

            # remember best acc@1 and save checkpoint
            is_best = acc1 > best_acc1
            best_acc1 = max(acc1, best_acc1)
            if not args.quiet:
                utils.save_checkpoint(experimentDir, args.name, {
                    'growth_step': i,
                    'epoch': epoch + 1,
                    'state_dict': model.state_dict(),
                    'best_acc1': best_acc1,
                    'optimizer' : optimizer.state_dict(),
                }, is_best)

            total_epoch += 1

    results['train_results'] = list(train_results)
    results['validate_results'] = list(validate_results)
    return results

def train(train_loader, model, criterion, optimizer, epoch, args, train_results, growth_step=None):
    batch_time = utils.AverageMeter()
    data_time = utils.AverageMeter()
    losses = utils.AverageMeter()
    top1 = utils.AverageMeter()
    top5 = utils.AverageMeter()

    # switch to train mode
    model.train()

    end = time.time()
    for i, (input, target) in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)

        input = input.cuda(args.gpu, non_blocking=True)
        target = target.cuda(args.gpu, non_blocking=True)

        # compute output
        output = model(input)
        loss = criterion(output, target)

        # measure accuracy and record loss
        acc1, acc5 = utils.accuracy(output, target, topk=(1, 5))
        losses.update(loss.item(), input.size(0))
        top1.update(acc1[0], input.size(0))
        top5.update(acc5[0], input.size(0))

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            growth_msg = 0 if growth_step is None else growth_step
            print('Epoch: [{0},{1}][{2}/{3}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Acc@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                  'Acc@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                   growth_msg, epoch, i, len(train_loader), batch_time=batch_time,
                   data_time=data_time, loss=losses, top1=top1, top5=top5))
            current_result = {'epoch': epoch, 'iteration': i,
                              'time': batch_time.val, 'loss': losses.val, 'top1': top1.val.item(),
                              'top5': top5.val.item()}
            if growth_step is not None:
                current_result['growth_step'] = growth_step
            train_results.append(dict(current_result))


def validate(val_loader, model, criterion, epoch, args, validate_results, growth_step=None):
    batch_time = utils.AverageMeter()
    losses = utils.AverageMeter()
    top1 = utils.AverageMeter()
    top5 = utils.AverageMeter()

    # switch to evaluate mode
    model.eval()

    with torch.no_grad():
        end = time.time()
        for i, (input, target) in enumerate(val_loader):
            input = input.cuda(args.gpu, non_blocking=True)
            target = target.cuda(args.gpu, non_blocking=True)

            # compute output
            output = model(input)
            loss = criterion(output, target)

            # measure accuracy and record loss
            acc1, acc5 = utils.accuracy(output, target, topk=(1, 5))
            losses.update(loss.item(), input.size(0))
            top1.update(acc1[0], input.size(0))
            top5.update(acc5[0], input.size(0))

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if i % args.print_freq == 0:
                print('Test: [{0}/{1}]\t'
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                      'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                      'Acc@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                      'Acc@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                       i, len(val_loader), batch_time=batch_time, loss=losses,
                       top1=top1, top5=top5))

        print(' * Acc@1 {top1.avg:.3f} Acc@5 {top5.avg:.3f}'
              .format(top1=top1, top5=top5))
        current_result = {'epoch': epoch, 'loss': losses.avg,
                                 'top1': top1.avg.item(), 'top5': top5.avg.item()} 
        if growth_step is not None:
            current_result['growth_step'] = growth_step
        validate_results.append(dict(current_result))

    return top1.avg


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Growing CNNs with PyTorch')
    parser.add_argument('name', type=str, help='name of experiment')
    parser.add_argument('settings_file', type=str, help='name of settings file '
                        'containing hyperparameter and training settings. Example '
                        'settings file is example_growing_settings.json')
    parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                        help='number of data loading workers (default: 4)')
    parser.add_argument('-p', '--print-freq', default=10, type=int,
                        metavar='N', help='print frequency (default: 10)')
    parser.add_argument('--evaluate', dest='modelPath', type=str, default=None,
                        help='evaluate model with path modelPath on validation set')
    parser.add_argument('--seed', default=None, type=int,
                        help='seed for initializing training. ')
    parser.add_argument('--gpu', default=0, type=int,
                        help='GPU id to use.')
    parser.add_argument('--quiet', dest='quiet', default=False,
                        action='store_true', help='whether or not to save a' +
                        'results log and copy of settings file.')
    
    args = parser.parse_args()
    
    main(args)

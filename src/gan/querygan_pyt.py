#!usr/bin/env/python
import argparse
from collections import namedtuple
import configparser
import copy
import math
import random
import numpy as np
import os
import re
import time
from tqdm import tqdm

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader

from discriminator.treelstm import QueryGAN_Discriminator, MULTIVACDataset
from discriminator.treelstm import Trainer, utils
from gen_pyt.datasets.english.dataset import English
from gen_pyt.asdl.asdl import *
from gen_pyt.asdl.lang.eng.eng_asdl_helper import asdl_ast_to_english, english_ast_to_asdl_ast
from gen_pyt.asdl.lang.eng.eng_transition_system import EnglishTransitionSystem
from gen_pyt.common.registerable import Registrable
from gen_pyt.components.action_info import get_action_infos
from gen_pyt.components.dataset import Example, Batch, Dataset
from gen_pyt.components.evaluator import Evaluator
from gen_pyt.model import nn_utils
from gen_pyt.model.parser import Parser
from gen_pyt.utils.io_utils import deserialize_from_file, serialize_to_file
from rollout import Rollout

from multivac.src.rdf_graph.rdf_parse import StanfordParser
# from generator.learner import Learner
# from generator.components import Hyp
# from generator.dataset import DataEntry, DataSet, Vocab, Action

def DiscriminatorDataset(real_dir, fake_dir, vocab):
    '''
    Take real examples from existing training dataset and add them to the 
    Generated dataset for adversarial training.
    '''
    real_file = MULTIVACDataset(real_dir, vocab)
    combined_file = MULTIVACDataset(fake_dir, vocab)

    labels = torch.cat((combined_file.labels, 
                        real_file.labels[real_file.labels==1]), dim=0)

    for i, item in enumerate(real_file.labels):
        if item == 1:
            combined_file.trees.append(real_file.trees[i])
            combined_file.sentences.append(real_file.sentences[i])
            combined_file.labels = labels

    combined_file.size = combined_file.labels.size(0)

    return combined_file

def disc_trainer(model, glove_emb, glove_vocab, use_cuda=False):
    device = torch.device("cuda:0" if use_cuda else "cpu")
    criterion = nn.MSELoss()
    emb = torch.zeros(glove_vocab.size(), glove_emb.size(1), dtype=torch.float, 
                      device=device)
    emb.normal_(0, 0.05)

    for word in glove_vocab.labelToIdx.keys():
        if glove_vocab.getIndex(word) < glove_emb.size(0):
            emb[glove_vocab.getIndex(word)] = glove_emb[glove_vocab.getIndex(word)]
        else:
            emb[glove_vocab.getIndex(word)].zero_()

    # plug these into embedding matrix inside model
    model.emb.weight.data.copy_(emb)
    model.to(device), criterion.to(device)

    if model.args['optim'] == 'adam':
        opt = optim.Adam
    elif model.args['optim'] == 'adagrad':
        opt = optim.Adagrad
    elif model.args['optim'] == 'sgd':
        opt = optim.SGD

    optimizer = opt(filter(lambda p: p.requires_grad,
                                  model.parameters()), 
                           lr=model.args['lr'], weight_decay=model.args['wd'])

    return Trainer(model.args, model, criterion, optimizer, device)

def generate_samples(net, transition_system, vocab, seq_len, 
                     generated_num, oracle=False, writeout=False):
    samples = [[]] * generated_num
    texts = examples = [''] * generated_num
    max_query_len = 0
    max_actions_len = 0
    dst_dir = net.args['sample_dir']
    parser = StanfordParser(annots="depparse")

    for i in tqdm(range(generated_num), desc='Generating Samples... '):
        sample = []

        while len(sample) == 0:
            query = vocab.convertToLabels(random.sample(range(vocab.size()), 
                                          seq_len))
            sample = net.parse(query, beam_size=net.args['beam_size'])

        text = asdl_ast_to_english(sample[0].tree)

        actions = transition_system.get_actions(sample[0].tree)
        tgt_actions = get_action_infos(query, actions)
        example = Example(src_sent=query, tgt_actions=tgt_actions, 
                          tgt_text=text,  tgt_ast=sample[0].tree, idx=i)

        if len(example.src_sent) > max_query_len:
            max_query_len = len(example.src_sent)

        if len(example.tgt_actions) > max_actions_len:
            max_actions_len = len(example.tgt_actions)

        samples[i]  = sample[0]
        examples[i] = example

    if oracle:
        return Dataset(examples)
    elif writeout:
        sample_parses = parser.get_parse('?\n'.join([e.tgt_text for e in examples]))

        with open(os.path.join(dst_dir, 'text.toks'   ), 'w') as tokfile, \
             open(os.path.join(dst_dir, 'text.parents'), 'w') as parfile, \
             open(os.path.join(dst_dir, 'cat.txt'     ), 'w') as catfile:

            for i, parse in enumerate(sample_parses['sentences']):
                tokens = [x['word'] for x in parse['tokens']]
                deps = sorted(parse['basicDependencies'], 
                              key=lambda x: x['dependent'])
                parents = [x['governor'] for x in deps]
                tree = MULTIVACDataset.read_tree(parents)

                parfile.write(' '.join([str(x) for x in parents]) + '\n')
                tokfile.write(' '.join(tokens) + '\n')
                catfile.write('0' + '\n')
    
    return zip(samples, examples)

def query_to_data(query, annot_vocab):
    if isinstance(query, str):
        query_tokens = query.split(' ')
    else:
        query_tokens = query

    data = np.zeros((1, len(query_tokens)), dtype='int32')

    for tid, token in enumerate(query_tokens):
        token_id = annot_vocab[token]

        data[0, tid] = token_id

    return data

def emulate_embeddings(embeds, shape, device='cpu'):
    samples = torch.zeros(*shape, dtype=torch.float)
    samples.normal_(torch.mean(embeds), torch.std(embeds))
    return samples

def load_to_layer(layer, embeds, vocab, words=None):

    if words is None:
        words = vocab

    words = sorted(words.labelToIdx.items(), key=lambda x: x[1])

    new_tensor = layer.weight.data.new
    layer_rows = set(range(layer.num_embeddings))

    assert len(words) == layer.num_embeddings

    for word, idx in words:
        if word in vocab and vocab.getIndex(word) < embeds.size(0):
            word_id = vocab.getIndex(word)
            layer.weight[idx].data = new_tensor(embeds[word_id])
            layer_rows.remove(idx)

    layer_rows = list(layer_rows)
    layer.weight[layer_rows].data = new_tensor(emulate_embeddings(embeds=embeds, 
                                                                  shape=(len(layer_rows), 
                                                                         layer.embedding_dim)))

def run(cfg_dict):
    # Set up model and training parameters based on config file and runtime
    # arguments

    args = cfg_dict['ARGS']
    gargs = cfg_dict['GENERATOR']
    dargs = cfg_dict['DISCRIMINATOR']
    gan_args = cfg_dict['GAN']

    seed = gan_args['seed']
    batch_size = gan_args['batch_size']
    total_epochs = gan_args['total_epochs']
    generated_num = gan_args['generated_num']
    vocab_size = gan_args['vocab_size']
    sequence_len = gan_args['sequence_len']

    # rollout params
    rollout_update_rate = gan_args['rollout_update_rate']
    rollout_num = gan_args['rollout_num']

    g_steps = gan_args['g_steps']
    d_steps = gan_args['d_steps']
    k_steps = gan_args['k_steps']
    
    use_cuda = args['cuda']

    if not torch.cuda.is_available():
        use_cuda = False

    gargs['cuda'] = use_cuda
    gargs['verbose'] = gan_args['verbose']
    dargs['cuda'] = use_cuda
    dargs['verbose'] = gan_args['verbose']

    random.seed(seed)
    np.random.seed(seed)

    # 
    # NEED A DATASET FIRST, TO DEFINE EMBEDDINGS/RULES SIZES
    # 
    #   - given a grammar file
    #   - given GloVe vocab list

    if gan_args['verbose']: print("Checking for existing grammar...")

    if gargs['grammar']:
        grammar = deserialize_from_file(gargs['grammar'])
    else:
        grammar = None

    glove_vocab, glove_emb = utils.load_word_vectors(
        os.path.join(gan_args['glove_dir'], gan_args['glove_file']))

    #
    # HERE'S WHERE WE PUT IN THE PYTORCH VERSION OF THE GENERATOR
    # 

    samples_data, prim_vocab, grammar = English.generate_dataset(gargs['annot_file'],
                                                                      gargs['texts_file'],
                                                                      grammar)
    transition_system = EnglishTransitionSystem(grammar)

    if gan_args['verbose']: print("Grammar and language transition system initiated.")

    if gan_args['verbose']: print("Loading Generator component...")

    netG = Parser(gargs, glove_vocab, prim_vocab, transition_system)
    netG.train()

    optimizer_cls = eval('torch.optim.%s' % gargs['optimizer'])  # FIXME: this is evil!
    netG.optimizer = optimizer_cls(netG.parameters(), lr=gargs['lr'])

    if gargs['uniform_init']:
        print('uniformly initialize parameters [-{}, +{}]'.format(gargs['uniform_init'], 
                                                                  gargs['uniform_init']))
        nn_utils.uniform_init(-gargs['uniform_init'], gargs['uniform_init'], netG.parameters())
    elif gargs['glorot_init']:
        print('use glorot initialization')
        nn_utils.glorot_init(netG.parameters())

    if gan_args['verbose']: print("Loading GloVe vectors as Generator embeddings...")

    load_to_layer(netG.src_embed, glove_emb, glove_vocab)
    load_to_layer(netG.primitive_embed, glove_emb, glove_vocab, prim_vocab)
    if gargs['cuda']: netG.cuda()

    # Set up Discriminator component with given parameters

    if gan_args['verbose']: print("Loading Discriminator component...")

    dargs['vocab_size'] = glove_vocab.size()
    netD = QueryGAN_Discriminator(dargs, glove_vocab)
    trainer = disc_trainer(netD, glove_emb, glove_vocab, use_cuda)

    # Set up Oracle component with given parameters
    # ### This is super expensive memory wise. let's figure something else out
    # oracle = copy.deepcopy(netG)
    # oracle.oracle = True

    # Generate starting samples
    seq_len = 6
    # gen_set = generate_samples(netG, transition_system, glove_vocab, seq_len, 
    #                            generated_num, oracle=True)

    # 
    # PRETRAIN GENERATOR
    # 

    print('\nPretraining generator...\n')
    # Pre-train epochs are set in config.cfg file
    netG.pretrain(Dataset(samples_data))
    # netG.pretrain(gen_set)

    rollout = Rollout(netG, update_rate=rollout_update_rate, rollout_num=rollout_num)

    # pretrain discriminator
    print('Loading Discriminator pretraining dataset.')
    dis_set = MULTIVACDataset(os.path.join(netD.args['data'], "train"), 
                              glove_vocab)
    
    if gan_args['verbose']: print("Pretraining discriminator...")

    for epoch in range(k_steps):
        loss = trainer.train(dis_set)
        print('Epoch {} pretrain discriminator training loss: {}'.format(epoch + 1, loss))

    # adversarial training
    print('\n#####################################################')
    print('Adversarial training...\n')

    discriminator_losses = []
    generator_losses = []

    for epoch in range(total_epochs):
        for step in range(g_steps):
            samples = generate_samples(netG, transition_system, glove_vocab, 
                                       seq_len, generated_num)
            hyps, examples = list(zip(*samples))
            step_begin = time.time()
            pgloss = netG.pgtrain(hyps, examples, rollout, netD)
            print('[Generator {}]  step elapsed {}s'.format(step, 
                                                            time.time() - step_begin))
            print('Generator adversarial loss={}'.format(pgloss))
            generator_losses.append(pgloss)

        for d_step in range(d_steps):
            # train discriminator
            _ = generate_samples(netG, transition_system, glove_vocab, 
                                 seq_len, generated_num, writeout=True)
            dis_set = DiscriminatorDataset(os.path.join(netD.args['data'], "train"), 
                                           netG.args['sample_dir'],
                                           glove_vocab)
            # disloader = DataLoader(dataset=dis_set,
            #                        batch_size=BATCH_SIZE,
            #                        shuffle=True)
        
            for k_step in range(k_steps):
                loss = trainer.train(dis_set)
                print('D_step {}, K-step {} adversarial discriminator training loss: {}'.format(d_step + 1, k_step + 1, loss))
                discriminator_losses.append(loss)
                
        rollout.update_params()

        save_progress(trainer, netG, samples, epoch, discriminator_losses, generator_losses)

        # generate_samples(netG, BATCH_SIZE, GENERATED_NUM, EVAL_FILE)
        # val_set = GeneratorDataset(EVAL_FILE)
        # valloader = DataLoader(dataset=val_set,
        #                        batch_size=BATCH_SIZE,
        #                        shuffle=True)
        # loss = oracle.val(valloader)
        # print('Epoch {} adversarial generator val loss: {}'.format(epoch + 1, loss))


def save_progress(trainer, netG, examples, epoch, discriminator_losses, generator_losses):
    # Save Generator model state and metadata
    gen_save = os.path.join(netG.args['output_dir'], "gen_checkpoint.pth")
    gen_checkpoint = {'epoch': epoch,
                      'state_dict': netG.state_dict(),
                      'args': netG.args,
                      'transition_system': netG.transition_system,
                      'vocab': netG.vocab,
                      'optimizer': netG.optimizer.state_dict()}
    torch.save(gen_checkpoint, gen_save)

    # Save Discriminator model state and metadata
    disc_save = os.path.join(netG.args['output_dir'], "disc_checkpoint.pth")
    dis_checkpoint = {'epoch': epoch,
                      'state_dict': trainer.model.state_dict(),
                      'args': trainer.model.args,
                      'optimizer': trainer.optimizer.state_dict(),
                      'criterion': trainer.criterion}
    torch.save(dis_checkpoint, disc_save)

    # Save loss histories
    with open(os.path.join(netG.args['output_dir'], 
                           "generator_losses.csv"), "w") as f:
        for l in generator_losses:
            f.write(str(l) + '\n')

    with open(os.path.join(netG.args['output_dir'], 
                           "discriminator_losses.csv"), "w") as f:
        for l in discriminator_losses:
            f.write(str(l) + '\n')

    # Save example generator outputs for qualitative assessment of progress
    save_examples = random.sample(examples, 10)

    with open(os.path.join(netG.args['output_dir'], 
                           "samples_{}.csv".format(epoch)), "w") as f:
        for e in save_examples:
            f.write(e.tgt_text + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Build and train a Generative Adversarial Network to '
                    'produce coherent, well-formed English language questions. '
                    'Provide a config file with parameters for the system, and '
                    'optionally override any of these with commandline '
                    'arguments. To override a config parameter, pass an argument'
                    ' matching the parameter, but prefaced with the part of the'
                    ' system it refers to. '
                    'I.e., "--generator_pretrain_epochs 120" would override the'
                    'generator parameter "pretrain_epochs" and set this value'
                    'to "120". Valid system parts are "gan", "generator", and '
                    '"discriminator".')
    parser.add_argument('--cuda', default=False, action='store_true', 
                        help='Enable GPU training.')
    parser.add_argument('-c', '--config', required=False, 
                        help='Config file with updated parameters for generator;'
                             'defaults to "config.cfg" in this directory '
                             'otherwise.')

    all_args = parser.parse_known_args()
    args = vars(all_args[0])
    overrides = {}

    i = 0

    while i < len(all_args[1]):
        if all_args[1][i].startswith('--'):
            key = all_args[1][i][2:]
            value = all_args[1][i+1]

            if value.startswith('--'):
                overrides[key] = True
                i += 1
                continue
            else:
                overrides[key] = value
                i += 2
        else:
            i += 1
    
    cfg = configparser.ConfigParser()
    cfgDIR = os.path.dirname(os.path.realpath(__file__))

    if args['config'] is not None:
        cfg.read(args['config'])
    else:
        cfg.read(os.path.join(cfgDIR, 'config.cfg'))

    cfg_dict = cfg._sections
    cfg_dict['ARGS'] = args

    for arg in overrides:
        section, param = arg.split("_", 1)
        try:
            cfg[section.upper()][param] = overrides[arg]
        except KeyError:
            print("Section " + section.upper() + "not found in "
                  "" + args['config'] + ", skipping.")
            continue

    for name, section in cfg_dict.items():
        for carg in section:
            # Cast all arguments to proper types
            if section[carg] == 'None':
                section[carg] = None
                continue

            try:
                section[carg] = int(section[carg])
            except:
                try:
                    section[carg] = float(section[carg])
                except:
                    if section[carg] in ['True','False']:
                        section[carg] = eval(section[carg])

    run(cfg_dict)

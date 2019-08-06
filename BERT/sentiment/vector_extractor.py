
from __future__ import absolute_import, division, print_function

import argparse
import glob
import logging
import os, sys, re, pickle
import random

import numpy as np
import torch
from torch import nn
from torch.utils.data import (DataLoader, RandomSampler, SequentialSampler,
                              TensorDataset)
from torch.utils.data.distributed import DistributedSampler
from tensorboardX import SummaryWriter
from tqdm import tqdm, trange

from pytorch_transformers import (WEIGHTS_NAME, BertConfig,
                                  BertForSequenceClassification, BertTokenizer,
                                  XLMConfig, XLMForSequenceClassification,
                                  XLMTokenizer, XLNetConfig,
                                  XLNetForSequenceClassification,
                                  XLNetTokenizer)

from pytorch_transformers import AdamW, WarmupLinearSchedule

sys.path.append("/local/datdb/pytorch-transformers/examples")
import utils_glue as utils_glue


logger = logging.getLogger(__name__)


## extract vectors from bert model 

class Extractor2ndLast (nn.Module):
  def __init__(self,bert_model,**kwargs):

    super().__init__()
    self.bert_model = bert_model
    self.bert_model.bert.encoder.output_hidden_states = True ## turn on this option to see the layers. 

  def encode_label_desc (self, label_desc, label_len, label_mask): # @label_desc is matrix row=sentence, col=index

    # # zero padding is not 0, but it has some value, because every character in sentence is "matched" with every other char.
    # # convert padding to actually zero or -inf (if we take maxpool later)
    # encoded_layers.data[label_desc.data==0] = -np.inf ## mask to -inf
    # return mean_sent_encoder ( encoded_layers , label_len ) ## second to last, batch_size x num_word x dim

    ## **** use pooled_output
    # We "pool" the model by simply taking the hidden state corresponding
    # to the first token.
    # https://github.com/huggingface/pytorch-pretrained-BERT/blob/master/pytorch_pretrained_bert/modeling.py#L423
    # For classification tasks, the first vector (corresponding to [CLS]) is
    # used as as the "sentence vector". Note that this only makes sense because
    # the entire model is fine-tuned.
    # https://github.com/huggingface/pytorch-pretrained-BERT/blob/master/examples/extract_features.py#L95

    encoded_layer , _  = self.bert_model.bert (input_ids=label_desc, token_type_ids=None, attention_mask=label_mask)
    second_tolast = encoded_layer[3][-2] ## @encoded_layer is tuple in the format: sequence_output, pooled_output, (hidden_states), (attentions)
    second_tolast[label_mask == 0] = 0 ## mask to 0, so that summation over len will not be affected with strange numbers
    cuda_second_layer = (second_tolast).type(torch.FloatTensor).cuda()
    encode_sum = torch.sum(cuda_second_layer, dim = 1).cuda()
    label_sum = torch.sum(label_mask.cuda(), dim=1).unsqueeze(0).transpose(0,1).type(torch.FloatTensor).cuda()
    go_vectors = encode_sum/label_sum
    return go_vectors

  def write_vector (self,label_desc_loader,fout_name,label_name):

    self.eval()

    if fout_name is not None:
      fout = open(fout_name,'w')
      fout.write(str(len(label_name)) + " " + str(768) + "\n") ## based on gensim style, so we can plot it later

    label_emb = None

    counter = 0 ## count the label to be written
    for step, batch in enumerate(tqdm(label_desc_loader, desc="get label vec")):
      if self.args.use_cuda:
        batch = tuple(t.cuda() for t in batch)
      else:
        batch = tuple(t for t in batch)

      label_desc1, label_len1, label_mask1 = batch

      with torch.no_grad():
        label_desc1.data = label_desc1.data[ : , 0:int(max(label_len1)) ] # trim down input to max len of the batch
        label_mask1.data = label_mask1.data[ : , 0:int(max(label_len1)) ] # trim down input to max len of the batch
        label_emb1 = self.encode_label_desc(label_desc1,label_len1,label_mask1)
       
      label_emb1 = label_emb1.detach().cpu().numpy()

      if fout_name is not None:
        for row in range ( label_emb1.shape[0] ) :
          fout.write( label_name[counter] + " " + " ".join(str(m) for m in label_emb1[row]) + "\n" ) ## space, because gensim format
          counter = counter + 1

      if label_emb is None:
        label_emb = label_emb1
      else:
        label_emb = np.concatenate((label_emb, label_emb1), axis=0) ## so that we have num_go x dim

    if fout_name is not None:
      fout.close()

    return label_emb



class LabelDescProcessor(utils_glue.DataProcessor):

  def get_train_examples(self, data_dir, file_name):
    """See base class."""
    return self._create_examples(
      self._read_tsv(os.path.join(data_dir, file_name)), "train")

  def get_dev_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
      self._read_tsv(os.path.join(data_dir, "dev.tsv")), 
      "dev_matched")

  def get_labels(self):
    """See base class."""
    return ["entailment", "not_entailment"]

  def _create_examples(self, lines, set_type):
    """Creates examples for the training and dev sets."""
    examples = []
    for (i, line) in enumerate(lines):
      if i == 0:
        continue
      guid = "%s-%s" % (set_type, line[0])
      text_a = line[1]
      examples.append(
        utils_glue.InputExample(guid=guid, text_a=text_a, label=1)) ## just put @label=1, so we can reuse code 
    return examples



def convert_examples_to_features(examples, label_list, max_seq_length,
                 tokenizer, output_mode,
                 cls_token_at_end=False, pad_on_left=False,
                 cls_token='[CLS]', sep_token='[SEP]', pad_token=0,
                 sequence_a_segment_id=0, sequence_b_segment_id=1,
                 cls_token_segment_id=1, pad_token_segment_id=0,
                 mask_padding_with_zero=True):

  ### **** USE THE SAME FUNCTION AS GITHUB BERT, but we do not add [CLS]

  """ Loads a data file into a list of `InputBatch`s
    `cls_token_at_end` define the location of the CLS token:
      - False (Default, BERT/XLM pattern): [CLS] + A + [SEP] + B + [SEP]
      - True (XLNet/GPT pattern): A + [SEP] + B + [SEP] + [CLS]
    `cls_token_segment_id` define the segment id associated to the CLS token (0 for BERT, 2 for XLNet)
  """

  # label_map = {label : i for i, label in enumerate(label_list)}

  features = []
  for (ex_index, example) in enumerate(examples):
    if ex_index % 10000 == 0:
      logger.info("Writing example %d of %d" % (ex_index, len(examples)))

    tokens_a = tokenizer.tokenize(example.text_a)

    tokens_b = None
    if example.text_b:
      tokens_b = tokenizer.tokenize(example.text_b)
      # Modifies `tokens_a` and `tokens_b` in place so that the total
      # length is less than the specified length.
      # Account for [CLS], [SEP], [SEP] with "- 3"
      utils_glue._truncate_seq_pair(tokens_a, tokens_b, max_seq_length - 3)
    else:
      # Account for [CLS] and [SEP] with "- 2"
      if len(tokens_a) > max_seq_length - 2:
        tokens_a = tokens_a[:(max_seq_length - 2)]

    # The convention in BERT is:
    # (a) For sequence pairs:
    #  tokens:   [CLS] is this jack ##son ##ville ? [SEP] no it is not . [SEP]
    #  type_ids:   0   0  0    0    0     0       0   0   1  1  1  1   1   1
    # (b) For single sequences:
    #  tokens:   [CLS] the dog is hairy . [SEP]
    #  type_ids:   0   0   0   0  0     0   0
    #
    # Where "type_ids" are used to indicate whether this is the first
    # sequence or the second sequence. The embedding vectors for `type=0` and
    # `type=1` were learned during pre-training and are added to the wordpiece
    # embedding vector (and position vector). This is not *strictly* necessary
    # since the [SEP] token unambiguously separates the sequences, but it makes
    # it easier for the model to learn the concept of sequences.
    #
    # For classification tasks, the first vector (corresponding to [CLS]) is
    # used as as the "sentence vector". Note that this only makes sense because
    # the entire model is fine-tuned.

    ## !!!  DO NOT ADD [CLS] AND [SEP]

    tokens = tokens_a 
    segment_ids = [sequence_a_segment_id] * len(tokens)

    if tokens_b:
      tokens += tokens_b
      segment_ids += [sequence_b_segment_id] * (len(tokens_b) + 1)

    input_ids = tokenizer.convert_tokens_to_ids(tokens)

    # The mask has 1 for real tokens and 0 for padding tokens. Only real
    # tokens are attended to.
    input_mask = [1 if mask_padding_with_zero else 0] * len(input_ids)

    # Zero-pad up to the sequence length.
    padding_length = max_seq_length - len(input_ids)
    if pad_on_left:
      input_ids = ([pad_token] * padding_length) + input_ids
      input_mask = ([0 if mask_padding_with_zero else 1] * padding_length) + input_mask
      segment_ids = ([pad_token_segment_id] * padding_length) + segment_ids
    else:
      input_ids = input_ids + ([pad_token] * padding_length)
      input_mask = input_mask + ([0 if mask_padding_with_zero else 1] * padding_length)
      segment_ids = segment_ids + ([pad_token_segment_id] * padding_length)

    assert len(input_ids) == max_seq_length
    assert len(input_mask) == max_seq_length
    assert len(segment_ids) == max_seq_length


    if ex_index < 5:
      logger.info("*** Example ***")
      logger.info("guid: %s" % (example.guid))
      logger.info("tokens: %s" % " ".join(
          [str(x) for x in tokens]))
      logger.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
      logger.info("input_mask: %s" % " ".join([str(x) for x in input_mask]))
      logger.info("segment_ids: %s" % " ".join([str(x) for x in segment_ids]))

    features.append(
        utils_glue.InputFeatures(input_ids=input_ids,
                input_mask=input_mask,
                segment_ids=segment_ids,
                label_id=1))
  return features


def make_loader (args,file_name,tokenizer,batch_size): 
  processor = LabelDescProcessor()
  examples = processor.get_train_examples(args.data_dir,file_name) 
  features = convert_examples_to_features(examples, None, 512, tokenizer,output_mode=None)

  all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
  all_input_mask = torch.tensor([f.input_mask for f in features], dtype=torch.long)
  all_segment_ids = torch.tensor([f.segment_ids for f in features], dtype=torch.long)

  dataset = TensorDataset(all_input_ids, all_input_mask, all_segment_ids)
  
  sampler = SequentialSampler(dataset)
  return DataLoader(dataset, sampler=sampler, batch_size=batch_size)



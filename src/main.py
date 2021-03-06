
from collections import defaultdict
import sys
import _dynet as dy

import imdb_data_reader
import trustpilot_data_reader
import ag_data_reader
import dw_data_reader
import blog_data_reader

from classifier import MLP, MLP_sigmoid
from example import Example
from bilstm import HierarchicalBiLSTM
from vocabulary import Vocabulary, TypedEncoder
import vocabulary
from discriminator import Discriminator, Generator

def compute_conditional_baseline(cond_aux, main):
    results = []
    n_examples = sum(main.values())
    p_y = [main[v] / n_examples for v in sorted(main)]
    assert(abs(sum(p_y) - 1.0) < 1e-7)
    for task in cond_aux:
        distributions = [cond_aux[task][label] / main[label] for label in sorted(main)]
        baselines = [max(d, 1 -d) for d in distributions]
        cond_baseline = sum([p * b for p, b in zip(baselines, p_y)])
        results.append(cond_baseline)
    return results

def print_data_distributions(dataset):
    main = defaultdict(int)
    aux = defaultdict(int)
    
    cond_aux = defaultdict(lambda:defaultdict(int))
    
    total = len(dataset)
    for example in dataset:
        label = example.get_label()
        main[label] += 1
        meta = example.get_aux_labels()
        for caracteristic in meta:
            aux[caracteristic] += 1
            cond_aux[caracteristic][label] += 1
    
    d_main = np.array(list(main.values()))
    d_aux  = np.array(list(aux.values()))

    assert(d_main.sum() == total)
    dist = d_main / total
    mfb = max(dist)
    print("Distribution_main_labels: ", dist, " Most frequent baseline : {}".format(100 * mfb))

    d = d_aux / total
    db = [max(c, 1-c) for c in d]
    print("Aux_distributions_priors:   ", "\t".join(map(lambda x : str(round(x,4)), d)))
    print("Aux_distributions_baselines:", "\t".join(map(lambda x : str(round(x,4)), db)))
    
    cond_baselines = compute_conditional_baseline(cond_aux, main)
    #print(cond_baselines)
    print("Aux_distrib_cond_baselines: ", "\t".join(map(lambda x : str(round(x,4)), cond_baselines)))
    return mfb


def get_demographics_prefix(example):
    aux = example.get_aux_labels()
    gen = "F" if trustpilot_data_reader.GENDER in aux else "M"
    age = "O" if trustpilot_data_reader.BIRTH in aux else "U"
    return ["<g={}>".format(gen), "<a={}>".format(age)]


def extract_vocabulary(dataset, add_symbols=None):
    freqs = defaultdict(int)
    for example in dataset:
        s = example.get_sentence()
        for token in s:
            freqs[token] += 1
    if add_symbols is not None:
        for s in add_symbols:
            freqs[s] += 1000
    return Vocabulary(freqs)

def get_aux_labels(examples):
    labels = set()
    for ex in examples:
        for l in ex.get_aux_labels():
            labels.add(l)
    return labels


def compute_eval_metrics(n_tasks, gold, predictions):
    tp = 0
    all_pred = 0
    all_gold = 0
    
    for gs, ps in zip(gold, predictions):
        ctp = len([i for i in gs if i in ps])
        tp += ctp
        
        all_pred += len(ps)
        all_gold += len(gs)
    precision = 0
    recall = 0
    f = 0
    if all_pred != 0:
        precision = tp / all_pred
    if all_gold != 0:
        recall = tp / all_gold
    if precision != 0 and recall != 0:
        f = 2 * precision * recall / (precision + recall)
    
    p = round(precision * 100, 2)
    r = round(recall * 100, 2)
    f = round(f * 100, 2)
    
    
    acc_all = [0] * n_tasks
    for gs, ps in zip(gold, predictions):
        for i in range(n_tasks):
            if (i in gs) == (i in ps):
                acc_all[i] += 1
    
    acc_all = [round(i * 100 / len(gold), 2) for i in acc_all]
    
    return p, r, f, acc_all

class PrModel:
    
    def __init__(self, args, model, trainer, bilstm, main_classifier, adversary_classifier, discriminator, generator, voc):
        self.args = args
        
        self.vocabulary = voc
        self.model = model
        self.trainer = trainer
        self.output_folder = args.output
        
        self.bilstm = bilstm

        self.main_classifier = main_classifier
        self.adversary_classifier = adversary_classifier
        self.discriminator = discriminator
        self.generator = generator
        
        #self.adversary = False

    def _get_input(self, example, training, do_not_renew, backprop):
        prefix = get_demographics_prefix(example) if self.args.use_demographics else []
        encoding, _ = self.bilstm.build_representations(example.get_sentence(), training=training, prefix = prefix, do_not_renew=do_not_renew)
        return encoding

    def get_input(self, example, training, do_not_renew=False, backprop=True):
        encoding = self._get_input(example, training, do_not_renew, backprop)
        if backprop:
            return encoding
        else:
            return dy.nobackprop(encoding)

    #def train_one(self, example, target, classifier):
        #input_vec = self.get_input(example, training=True, backprop=not self.adversary)
        #loss = classifier.get_loss(input_vec, target)
        #loss.backward()
        #self.trainer.update()

    #def predict(self, example, target, classifier):
        #input_vec = self.get_input(example, training=False)
        #loss, prediction = classifier.get_loss_and_prediction(input_vec, target)
        #return loss, prediction

    def compute_hamming(self, e1, e2):
        n_output = self.adversary_classifier.output_size()
        m1 = e1.get_aux_labels()
        m2 = e2.get_aux_labels()
        res = 0
        for i in range(n_output):
            if (i in m1) == (i in m2):
                res += 1
        return res / n_output

    def privacy_train(self, example, train):

        sampled_example = np.random.choice(train)
        
        input_e1 = self.get_input(example, training=True, do_not_renew=False, backprop=True)
        input_e2 = self.get_input(sampled_example, training=True, do_not_renew=True, backprop=True)
        
        hamming = self.compute_hamming(example, sampled_example)
        assert(hamming >= 0 and hamming <= 1.0)
        
        loss = args.alpha * (0.5 - hamming) * dy.squared_norm(input_e1 - input_e2)
        loss.backward()

        self.trainer.update()

    def discriminator_train(self, example):

        real_labels = example.get_aux_labels()
        n_labels = self.adversary_classifier.output_size()
        fake_labels = set([i for i in range(n_labels) if i not in real_labels])
        
        input = self.get_input(example, training=True, do_not_renew=False, backprop=True)
        input_noback = dy.nobackprop(input)
        
        real_loss = self.discriminator.train_real(input_noback, real_labels)
        fake_loss = self.discriminator.train_fake(input, fake_labels)
        fake_loss.backward()
        
        self.discriminator.zero_gradient()
        
        self.trainer.update()
        
        return real_loss.value()

    def generator_train(self, example):

        text = example.sentence
        coded_text = self.vocabulary.code_chars(text)

        input = self.get_input(example, training=True, do_not_renew=False, backprop=True)
        input_noback = dy.nobackprop(input)
        
        real_loss = self.generator.train_real(input_noback, coded_text)
        
        fake_loss = - self.generator.train_fake(input, coded_text)
        fake_loss.backward()
        
        self.generator.zero_gradient()
        
        self.trainer.update()
        
        return real_loss.value()

    def evaluate_main(self, dataset, targets):
        loss = 0
        acc = 0
        tot = len(dataset)
        assert(len(targets) == len(dataset))
        self.bilstm.disable_dropout()
        predictions = []
        for i, ex in enumerate(dataset):
            #l, p = self.predict(ex, targets[i], self.main_classifier)
            #def predict(self, example, target, classifier):
            input_vec = self.get_input(ex, training=False, do_not_renew = False)
            l, p = self.main_classifier.get_loss_and_prediction(input_vec, targets[i])
            #return loss, prediction
            
            predictions.append(p)
            if p == targets[i]:
                acc += 1
            loss += l.value()
        return loss / tot, acc / tot * 100, predictions

    def train_main(self, train, dev):
        
        lr = self.args.learning_rate
        dc = self.args.decay_constant

        random.shuffle(train)
        sample_train = train[:len(dev)]
        self.trainer.learning_rate = lr
        n_updates = 0

        best = 0
        ibest=0
        
        
        for epoch in range(self.args.iterations):
            random.shuffle(train)
            self.bilstm.set_dropout(0.2)
            
            discriminator_loss = 0
            generator_loss = 0
            for i, example in enumerate(train):
                
                try: 
                    sys.stderr.write("\r{}%".format(i / len(train) * 100))
                    
                    #self.train_one(example, get_label(example), classifier)
                    #def train_one(self, example, target, classifier):
                    target = example.get_label()
                    input_vec = self.get_input(example, training=True, backprop=True)
                    loss = self.main_classifier.get_loss(input_vec, target)
                    loss.backward()
                    self.trainer.update()

                    # learning rate decay
                    self.trainer.learning_rate = lr / (1 + n_updates * dc)
                    
                    if self.args.ptraining:
                        self.privacy_train(example, train)
                    
                    if self.args.atraining:
                        discriminator_loss += self.discriminator_train(example)
                    
                    if self.args.generator:
                        generator_loss += self.generator_train(example)
                    
                    n_updates += 1
                except:
                    print("Error")
            
            sys.stderr.write("\r")
            
            extra_info = ""
            if self.args.atraining:
                extra_info = "D loss = {}".format(discriminator_loss/ len(train))
            if self.args.generator:
                extra_info = "G loss = {}".format(generator_loss / len(train))

            
            targets_t = [ex.get_label() for ex in sample_train]
            targets_d = [ex.get_label() for ex in dev]
            
            loss_t, acc_t, predictions_t = self.evaluate_main(sample_train, targets_t)
            loss_d, acc_d, predictions_d = self.evaluate_main(dev, targets_d)
            
            cmpare = acc_d
            
            #Fscore = ""
            #if self.adversary:
                #ftrain = compute_eval_metrics(classifier.output_size(), targets_t, predictions_t)
                #fdev = compute_eval_metrics(classifier.output_size(), targets_d, predictions_d)
                ##print(ftrain, fdev)
                #Fscore = "F: t = {} d = {}".format(ftrain, fdev)
                
                #cmpare = fdev[2]
            
            if cmpare >= best:
                best = cmpare
                ibest = epoch
                self.model.save("{}/main_model{}".format(self.output_folder, ibest))
            
            print("Epoch {} train: l={:.4f} acc={:.2f} dev: l={:.4f} acc={:.2f} {}".format(epoch, loss_t, acc_t, loss_d, acc_d, extra_info), flush=True)
        
        
        if self.args.iterations > 0:
            self.model.populate("{}/main_model{}".format(self.output_folder, ibest))
        return best

    #def train_main(self, train, dev):
        #get_label = lambda ex: ex.get_label()
        #return self._train(train, dev, self.args.iterations, self.main_classifier, get_label, False)

    #def train_adversary(self, train, dev):
        #get_label = lambda ex: ex.get_aux_labels()
        #return self._train(train, dev, self.args.iterations_adversary, self.adversary_classifier, get_label, True)

    def get_adversary_dataset(self, data):
        self.bilstm.disable_dropout()
        vectors = []
        for ex in data:
            input_vec = self.get_input(ex, training=True, backprop=False)
            
            pair = (input_vec.value(), ex.get_aux_labels())
            vectors.append(pair)
        return vectors
    
    def evaluate_adversary(self, dataset):
        loss = 0
        acc = 0
        tot = len(dataset)
        
        predictions = []
        for i, ex in enumerate(dataset):
            
            dy.renew_cg()
            vec, labels = ex
            vec = dy.inputVector(vec)
            
            l, p = self.adversary_classifier.get_loss_and_prediction(vec, labels)
            
            predictions.append(p)
            if p == labels:
                acc += 1
            loss += l.value()

        return loss / tot, acc / tot * 100, predictions

    
    def train_adversary(self, train, dev):
        lr = self.args.learning_rate
        dc = self.args.decay_constant
        
        random.shuffle(train)
        sample_train = train[:len(dev)]
        self.trainer.learning_rate = lr
        
        epochs = self.args.iterations_adversary
        
        n_updates = 0
        best = 0
        ibest=0
        
        for epoch in range(self.args.iterations_adversary):
            random.shuffle(train)
            
            for i, example in enumerate(train):
                
                dy.renew_cg()
                
                vec, label = example
                vec = dy.inputVector(vec)
                
                sys.stderr.write("\r{}%".format(i / len(train) * 100))
                
                loss = self.adversary_classifier.get_loss(vec, label)
                loss.backward()
                self.trainer.update()
                self.trainer.learning_rate = lr / (1 + n_updates * dc)
                
                n_updates += 1
            
            sys.stderr.write("\r")
            
            
            targets_t = [label for _, label in sample_train]
            targets_d = [label for _, label in dev]
            
            loss_t, acc_t, predictions_t = self.evaluate_adversary(sample_train)
            loss_d, acc_d, predictions_d = self.evaluate_adversary(dev)
            
            cmpare = acc_d
            
            ftrain = compute_eval_metrics(self.adversary_classifier.output_size(), targets_t, predictions_t)
            fdev = compute_eval_metrics(self.adversary_classifier.output_size(), targets_d, predictions_d)

            Fscore = "F: t = {} d = {}".format(ftrain, fdev)
            cmpare = fdev[2]
            
            if "tp" in self.args.dataset or "bl" in self.args.dataset:
                acc_all = fdev[3]
                cmpare = sum(acc_all) / len(acc_all)
            
            
            if cmpare >= best:
                best = cmpare
                ibest = epoch
                self.model.save("{}/adverse_model{}".format(self.output_folder, ibest))
            
            print("Epoch {} train: l={:.4f} acc={:.2f} dev: l={:.4f} acc={:.2f} {} ".format(epoch, loss_t, acc_t, loss_d, acc_d, Fscore), flush=True)
        
        if epochs > 0:
            self.model.populate("{}/adverse_model{}".format(self.output_folder, ibest))
        
        return best


    def train_baseline(self, train, dev, test, epochs):

        lr = self.args.learning_rate
        dc = self.args.decay_constant

        random.shuffle(train)
        sample_train = train[:len(dev)]
        self.trainer.learning_rate = lr
        n_updates = 0

        best = 0
        ibest=0
        
        for epoch in range(epochs):
            random.shuffle(train)
            self.bilstm.set_dropout(0.2)
            
            for i, example in enumerate(train):
                sys.stderr.write("\r{}%".format(i / len(train) * 100))
                
                try:
                    #self.train_one(example, example.get_aux_labels(), classifier)
                    target = example.get_aux_labels()
                    
                    input_vec = self.get_input(example, training=True, backprop=True, do_not_renew = False)
                    loss = self.adversary_classifier.get_loss(input_vec, target)
                    loss.backward()
                    self.trainer.update()

                    self.trainer.learning_rate = lr / (1 + n_updates * dc)
                    
                    n_updates += 1
                except:
                    print("error")
            
            sys.stderr.write("\r")
            
            targets_t = [ex.get_aux_labels() for ex in sample_train]
            targets_d = [ex.get_aux_labels() for ex in dev]
            
            dataset_t = self.get_adversary_dataset(sample_train)
            dataset_d = self.get_adversary_dataset(dev)
            
            loss_t, acc_t, predictions_t = self.evaluate_adversary(dataset_t)
            loss_d, acc_d, predictions_d = self.evaluate_adversary(dataset_d)
            
            ftrain = compute_eval_metrics(self.adversary_classifier.output_size(), targets_t, predictions_t)
            fdev = compute_eval_metrics(self.adversary_classifier.output_size(), targets_d, predictions_d)
            
            Fscore = "F: t = {} d = {}".format(ftrain, fdev)
            cmpare = fdev[2]
            
            if "tp" in self.args.dataset or "bl" in self.args.dataset:
                acc_all = fdev[3]
                cmpare = sum(acc_all) / len(acc_all)
            
            if cmpare >= best:
                best = cmpare
                ibest = epoch
                self.model.save("{}/_baseline_model{}".format(self.output_folder, ibest))
            
            print("Epoch {} train: l={:.4f} acc={:.2f} dev: l={:.4f} acc={:.2f} {}".format(epoch, loss_t, acc_t, loss_d, acc_d, Fscore), flush=True)
        
        if epochs > 0:
            self.model.populate("{}/_baseline_model{}".format(self.output_folder, ibest))
        
        targets_t = [ex.get_aux_labels() for ex in test]
        dataset_t = self.get_adversary_dataset(test)
        loss_t, acc_t, predictions_t = self.evaluate_adversary(dataset_t)
        #, targets_t, classifier, False)
        
        ftest = compute_eval_metrics(self.adversary_classifier.output_size(), targets_t, predictions_t)
        
        return acc_t, ftest



def main(args):
    import dynet as dy
    
    get_data = {"ag": lambda : ag_data_reader.get_dataset(args.num_NE),
                "dw": lambda : dw_data_reader.get_dataset(args.num_NE),
                "bl": lambda : blog_data_reader.get_dataset(),
                "tp_fr": lambda : trustpilot_data_reader.get_dataset("fr"),
                "tp_de": lambda : trustpilot_data_reader.get_dataset("de"),
                "tp_dk": lambda : trustpilot_data_reader.get_dataset("dk"),
                "tp_us": lambda : trustpilot_data_reader.get_dataset("us"),
                "tp_uk": lambda : trustpilot_data_reader.get_dataset("uk")}
    
    train, dev, test = get_data[args.dataset]()
    
    labels_main_task = set([ex.get_label() for ex in train])
    labels_main_task.add(0)
    
    assert(sorted(labels_main_task) == list(range(len(labels_main_task))))
    
    labels_adve_task = get_aux_labels(train)
    
    print("Train size: {}".format(len(train)))
    print("Dev size:   {}".format(len(dev)))
    print("Test size:  {}".format(len(test)))
    
    print("Train data distribution")
    mfb_train = print_data_distributions(train)

    print("Dev data distribution")
    mfb_dev = print_data_distributions(dev)

    print("Test data distribution")
    mfb_test = print_data_distributions(test)

    results = {}

    model = dy.Model()
    
    #if args.use_demographics:
    symbols = ["<g={}>".format(i) for i in ["F", "M"]] + ["<a={}>".format(i) for i in ["U", "O"]]
    vocabulary = extract_vocabulary(train, add_symbols=symbols)
    
    bilstm = HierarchicalBiLSTM(args, vocabulary, model)
    input_size = bilstm.size()
    main_classifier = MLP(input_size, len(labels_main_task), args.hidden_layers, args.dim_hidden, dy.rectify, model)
    
    trainer = dy.AdamTrainer(model)
    trainer.set_clip_threshold(5)
    
    args.learning_rate = trainer.learning_rate
    
    if args.subset:
        train = train[:args.subset]
        dev = dev[:args.subset]

    output_size = len(labels_adve_task)
    adversary_classifier = MLP_sigmoid(input_size, output_size, args.hidden_layers, args.dim_hidden, dy.rectify, model)
    
    discriminator = None
    if args.atraining:
        discriminator = Discriminator(input_size, output_size, args.hidden_layers, args.dim_hidden, dy.rectify, model, trainer)
    
    generator = None
    if args.generator:
        generator = Generator(args, vocabulary, model, trainer)

    #### add adversary classifier
    mod = PrModel(args, model, trainer, bilstm, main_classifier, adversary_classifier, discriminator, generator, vocabulary)
    
    
    if args.baseline:
        _, ftest = mod.train_baseline(train, dev, test, args.iterations)
        print(ftest)
        return
    
    
    print("Train main task")
    results["000_main_dev_acc"] = mod.train_main(train, dev)
    
    targets_test = [ex.get_label() for ex in test]
    loss_test, acc_test, _ = mod.evaluate_main(test, targets_test)
    print("\t Test results : l={} acc={}".format(loss_test, acc_test))
    results["001_main_test_acc"] = acc_test
    
    
    
    ##############
    ##############
    ##############
    ##############
    ##############
    ##############
    ##############    Adversary training / evaluate privacy
    ##############
    ##############
    ##############
    ##############
    ##############

    train_hidden, dev_hidden, test_hidden = [mod.get_adversary_dataset(dataset) for dataset in [train, dev, test]]
    
    
    trainer.restart()
    print("Train adversary")
    results["002_adv_dev_F"] = mod.train_adversary(train_hidden, dev_hidden)
    targets_test = [ex.get_aux_labels() for ex in test]
    loss_test, acc_test, predictions_test = mod.evaluate_adversary(test_hidden)
    
    print("\t Adversary Test results : l={} acc={}".format(loss_test, acc_test))
    outsize = mod.adversary_classifier.output_size()
    Fscore = compute_eval_metrics(outsize, targets_test, predictions_test)
    print("\tF          = {} ".format(Fscore))


    results["003_adv_test_fscore"] = Fscore[2]
    results["004_adv_test_precision"] = Fscore[0]
    results["005_adv_test_recall"] = Fscore[1]
    for i, acc in enumerate(Fscore[3]):
        results["{}_adv_test_acc_task_{}".format(str(i+6).zfill(3), i)] = acc


    preds = [set(range(outsize)) for _ in targets_test]
    Fscore = compute_eval_metrics(outsize, targets_test, preds)
    
    baseline_str = [Fscore[2], Fscore[0], Fscore[1]] + [x if x > 50.0 else 100 - x for x in Fscore[3]]
    
    
    
    line = ["Baseline", "NA", "NA", "NA", "NA", "NA", "NA", "NA", str(round(mfb_train * 100, 2)), str(round(mfb_test*100, 2)), "0"]
    print("\t".join(line) + "\t" + "\t".join(map(str, baseline_str)))
    
    
    for k in results:
        if type(results[k]) == float:
            results[k] = round(results[k], 2)
    
    
    results["#H"] = args.dim_hidden
    results["#h"] = args.hidden_layers
    results["#w"] = args.dim_word
    results["#W"] = args.dim_wrnn
    results["#Zatr"] = int(args.atraining)
    results["#Zptr"] = int(args.ptraining)
    results["#Zalpha"] = args.alpha
    
    keys = sorted(results)
    
    print("Model\t", end="")
    print("\t".join(keys))
    print("\t".join(map(str, [results[k] for k in keys])))



    #print("Sanity check")
    #targets_test = [ex.get_label() for ex in test]
    #loss_test, acc_test, _ = mod.evaluate_main(test, targets_test)
    #print("\t Test results : l={} acc={}".format(loss_test, acc_test))


if __name__ == "__main__":
    import argparse
    import random
    import numpy as np
    import os
    random.seed(10)
    np.random.seed(10)
    
    usage = """Implements the privacy evaluation protocol described in the article.

(i) Trains a classifier to predict text labels (topic, sentiment)
(ii) Generate a dataset with the hidden
  representations of each text {r(x), z} with:
    * z: binary private variables
    * x: text
    * r(x): vector representation of text
(iii) Trains the attacker to predict z from x and evaluates privacy
"""
    
    parser = argparse.ArgumentParser(description = usage, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("output", help="Output folder")
    parser.add_argument("dataset", choices=["ag", "dw", "tp_fr", "tp_de", "tp_dk", "tp_us", "tp_uk", "bl"], help="Dataset. tp=trustpilot, bl=blog")
    
    parser.add_argument("--iterations", "-i", type=int, default=20, help="Number of training iterations")
    parser.add_argument("--iterations-adversary", "-I", type=int, default=20, help="Number of training iterations for attacker")
    
    parser.add_argument("--decay-constant", type=float, default=1e-6)
    parser.add_argument("--learning-rate", type=float, default=0.1)
    parser.add_argument("--aux", action="store_true", help="Use demographics as aux tasks [not used in article]")
    parser.add_argument("--bidirectional", action="store_true", help="Use a bidirectional lstm instead of unidirectional")
    
    parser.add_argument("--adversary-type", choices=["logistic", "softmax"], default="logistic")

    parser.add_argument("--dynet-seed", type=int, default=4 , help="random seed for dynet (needs to be first argument!)")
    parser.add_argument("--dynet-weight-decay", type=float, default=1e-6, help="Weight decay for dynet")


    parser.add_argument("--dim-char","-c", type=int, default=50, help="Dimension of char embeddings")
    parser.add_argument("--dim-crnn","-C", type=int, default=50, help="Dimension of char lstm")
    parser.add_argument("--dim-word","-w", type=int, default=50, help="Dimension of word embeddings")
    parser.add_argument("--dim-wrnn","-W", type=int, default=50, help="Dimension of word lstm")
    
    parser.add_argument("--use-demographics", "-D", action="store_true", help="use demographic variables as input to bi-lstm [+DEMO setting in article]")
    
    parser.add_argument("--hidden-layers", "-L", type=int, default=1, help="Number of hidden layers")
    parser.add_argument("--dim-hidden", "-l", type=int, default=50, help="Dimension of hidden layers")
    parser.add_argument("--use-char-lstm", action="store_true", help="Use a character LSTM, [default=false]")
    
    parser.add_argument("--subset", "-S", type=int, default=None, help="Train on a subset of n examples for debugging")
    
    parser.add_argument("--num-NE", "-k", type=int, default=4, help="Number of named entities (topic classification only)")

    # Defense methods
    parser.add_argument("--atraining", action="store_true", help="Adversarial classification defense (multidetasking)")
    parser.add_argument("--ptraining", action="store_true", help="Declustering defense")
    parser.add_argument("--alpha", type=float, default=0.01, help="Scaling value declustering")
    
    parser.add_argument("--generator", action="store_true", help="Adversarial generation defense")
    
    parser.add_argument("--baseline", action="store_true", help="Train a full model on private variables (upper bound for the attacker)")

    args = parser.parse_args()
    
    os.makedirs(args.output, exist_ok=True)
    
    if "--dynet-seed" not in sys.argv:
        sys.argv.extend(["--dynet-seed", str(args.dynet_seed)])

    main(args)

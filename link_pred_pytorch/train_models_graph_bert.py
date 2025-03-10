
from train_inits import *
from utils.graph_bert_utils import save_encoding_data, get_encodings

def train_current_time_step(FLAGS, graphs, adjs, time_step, data_encode_dict, device):
    """
    Setup
    """
    
    # Set random seed
    random.seed(FLAGS.seed)
    np.random.seed(FLAGS.seed)
    torch.manual_seed(FLAGS.seed)
    print(FLAGS)
    
    FLAGS.time_step = time_step
    # Recursively make directories if they do not exist
    FLAGS.res_id = 'Final_%s_%s_seed_%d_time_%d'%(FLAGS.model_name, FLAGS.dataset, FLAGS.seed, FLAGS.time_step)
    log_file, output_dir = create_logger(FLAGS)
    print('Savr dir:', output_dir)
    logging.basicConfig(filename=log_file, level=logging.INFO, 
                        format='%(asctime)s - %(levelname)s: %(message)s',
                        datefmt='%m/%d/%Y %H:%M:%S')
    logging.info(vars(FLAGS))

    cur_window, train_start_time, eval_time = get_train_time_interval(FLAGS)
    FLAGS.cur_window = cur_window 

    graphs_train = graphs[train_start_time: eval_time]
    adjs_train = adjs[train_start_time: eval_time]
    graphs_eval = graphs[eval_time]
    adjs_eval = adjs[eval_time]

    norm_adjs_train = [normalize_graph_gcn(adj) for adj in adjs_train]

    train_edges, train_edges_false, val_edges, val_edges_false, test_edges, test_edges_false = \
        get_evaluation_data(adjs_train[-1], graphs_eval, FLAGS.time_step, FLAGS.dataset, force_regen=False)

    print("# train: {}, # val: {}, # test: {}\n".format(len(train_edges), len(val_edges), len(test_edges)))
    logging.info("# train: {}, # val: {}, # test: {}".format(len(train_edges), len(val_edges), len(test_edges)))

    # Load training context pairs (or compute them if necessary)
    if FLAGS.supervised:
        pass
    else:
        pass

    # Load node feats
    num_nodes_graph_eval = len(graphs_eval.nodes)
    feats = get_feats(adjs_train, num_nodes_graph_eval, train_start_time, eval_time, FLAGS)
    FLAGS.num_features = feats[0].shape[1]

    # Normalize and convert feats to sparse tuple format 
    feats_train = [preprocess_features(feat) for feat in feats]
    assert len(feats_train) == len(adjs_train)
    feats_train = [tuple_to_sparse(feature, torch.float32).to(device) for feature in feats_train]
    norm_adjs_train  = [tuple_to_sparse(adj, torch.float32).to(device) for adj in norm_adjs_train]
    print('# feas_train: %d, # adjs_train: %d'%(len(feats_train), len(adjs_train)))

    # Setup minibatchsampler
    if FLAGS.supervised:
        from utils.minibatch_sup import NodeMinibatchIterator
        minibatchIterator = NodeMinibatchIterator(
            negative_mult_training=FLAGS.neg_sample_size,      # negative sample size
            graphs=graphs_train,                               # graphs (total) 
            adjs=adjs_train,                                   # adjs (total)
        )
    else: # from DySAT paper
        pass

    # Setup model and optimizer
    model = load_model(FLAGS, device)
    optimizer = optim.Adam(model.parameters(), lr=FLAGS.learning_rate, weight_decay=FLAGS.weight_decay)

    # Setup result accumulator variables.
    epochs_test_result = defaultdict(lambda: [])
    epochs_val_result = defaultdict(lambda: [])

    """
    Training starts
    """
    epoch_train_loss_all = []
    best_valid_result = 0
    best_valid_epoch = 0
    best_valid_model_path = os.path.join(output_dir, 'best_valid_model_{}.pt'.format(FLAGS.dataset))
    best_valid_epoch_predict_true = None

    total_epoch_time = 0.0
    for epoch in range(FLAGS.num_epoches):
        model.train()
        minibatchIterator.shuffle()
        epoch_train_loss = []
        epoch_time = 0.0
        it = 0
        
        while not minibatchIterator.end(): 
            t = time.time()
            # sample; forward; loss
            if FLAGS.supervised:
                train_start, train_end, pos_edges, neg_edges = minibatchIterator.next_minibatch_feed_dict() 

                ###
                iter_i = train_end - 1
                t = train_start_time + iter_i
                wl_dict, batch_dict, hop_dict = data_encode_dict[t]
                raw_embeddings, wl_embedding, hop_embeddings, int_embeddings = get_encodings(feats[iter_i].toarray(),
                                                                                             wl_dict, batch_dict, hop_dict, device)
                ###
                output = model(raw_embeddings, wl_embedding, hop_embeddings, int_embeddings)
                loss = link_forecast_loss(output, pos_edges, neg_edges, FLAGS.neg_weight, device)
            else:
                pass
                        
            # backprop
            optimizer.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), FLAGS.max_gradient_norm)
            optimizer.step()
            epoch_train_loss.append(loss.data.item())
            # track current time
            epoch_time += time.time() - t
            # logging
            logging.info("Mini batch Iter: {} train_loss= {:.5f}".format(it, loss.data.item()))
            logging.info("Time for Mini batch Iter {}: {}".format(it, time.time() - t))
            it += 1

        epoch_train_loss_all.append(np.mean(epoch_train_loss))
        total_epoch_time += epoch_time
        logging.info("Time for epoch : {}".format(epoch_time))

        # Validation in training:
        if (epoch + 1) % FLAGS.test_freq == 0:
            model.eval()                                # disable dropout in model
            minibatchIterator.test_reset()
            
            iter_i = FLAGS.cur_window-1
            t = eval_time - 1
            wl_dict, batch_dict, hop_dict = data_encode_dict[t]
            raw_embeddings, wl_embedding, hop_embeddings, int_embeddings = get_encodings(feats[iter_i].toarray(),
                                                                                         wl_dict, batch_dict, hop_dict, device)
            ###
            output = model(raw_embeddings, wl_embedding, hop_embeddings, int_embeddings)
            emb = output.detach().cpu().numpy()            
            # Use external classifier to get validation and test results.

            val_results, test_results, val_pred_true, test_pred_true = evaluate_classifier(
                train_edges, train_edges_false, 
                val_edges, val_edges_false, 
                test_edges, test_edges_false, 
                emb, emb)

            val_HAD = val_results["HAD"][0]
            test_HAD = test_results["HAD"][0]
            val_SIGMOID = val_results["SIGMOID"][0]
            test_SIGMOID = test_results["SIGMOID"][0]

            print("Epoch %d, Val AUC_HAD %.4f, Test AUC_HAD %.4f, Val AUC_SIGMOID %.4f, Test AUC_SIGMOID %.4f, "%(epoch, val_HAD, test_HAD, val_SIGMOID, test_SIGMOID))
            logging.info("Epoch %d, Val AUC_HAD %.4f, Test AUC_HAD %.4f, Val AUC_SIGMOID %.4f, Test AUC_SIGMOID %.4f, "%(epoch, val_HAD, test_HAD, val_SIGMOID, test_SIGMOID))

            epochs_test_result["HAD"].append(test_HAD)
            epochs_val_result["HAD"].append(val_HAD)
            epochs_test_result["SIGMOID"].append(test_SIGMOID)
            epochs_val_result["SIGMOID"].append(val_SIGMOID)
            
            if val_HAD > best_valid_result:
                best_valid_result = val_HAD
                best_valid_epoch = epoch
                best_valid_epoch_predict_true = val_pred_true["HAD"],
                torch.save(model.state_dict(), best_valid_model_path)
            
            if epoch - best_valid_epoch > 100: # FLAGS.num_epoches/2:
                break

    """
    Done training: choose best model by validation set performance.
    """
    best_epoch = epochs_val_result["HAD"].index(max(epochs_val_result["HAD"]))
    logging.info("Total used time is: {}\n".format(total_epoch_time))
    print("Total used time is: {}\n".format(total_epoch_time))
    
    print("Best epoch ", best_epoch)
    logging.info("Best epoch {}".format(best_epoch))

    val_results, test_results = epochs_val_result["HAD"][best_epoch], epochs_test_result["HAD"][best_epoch]

    print("Best epoch val results {}".format(val_results))
    print("Best epoch test results {}".format(test_results))

    logging.info("Best epoch val results {}\n".format(val_results))
    logging.info("Best epoch test results {}\n".format(test_results))

    """
    Get final results
    """
    result = {
        'id': FLAGS.res_id,
        'best_epoch': best_epoch, 
        'best_valid_epoch_result': val_results,
        'best_test_epoch_result': test_results,
        'valid_epoch_auc': epochs_val_result["HAD"],
        'test_epoch_auc': epochs_test_result["HAD"],
        'epoch_train_loss': epoch_train_loss_all
    }

    with open(os.path.join(output_dir, 'result_{}.json'.format(FLAGS.dataset)), 'w') as outfile:
        json.dump(result, outfile)
    np.save(os.path.join(output_dir, 'test_pred_true.npy'), np.array(best_valid_epoch_predict_true))
    
    return result

if __name__ == '__main__':

    # import args
    FLAGS = flags()
    print(FLAGS)

    # Set random seed
    random.seed(FLAGS.seed)
    np.random.seed(FLAGS.seed)
    torch.manual_seed(FLAGS.seed)

    # Set device
    device = get_device(FLAGS)

    # load graphs
    graphs, adjs = load_graphs(FLAGS.dataset)
    data_encode_dict = save_encoding_data(graphs, FLAGS, force_regen=False)
    
    FLAGS.min_time, FLAGS.max_time = update_minmax_time(FLAGS.dataset)
    for time_step in range(FLAGS.min_time, FLAGS.max_time+1):
        print("Run time_step %d"%time_step)
        train_current_time_step(FLAGS, graphs, adjs, time_step, data_encode_dict, device)
import argparse
import os
import time
import traceback
import sys

import numpy as np

import tensorflow as tf
from tensorflow.python.client import device_lib

from sklearn.datasets import make_classification

def get_available_gpus():
    local_device_protos = device_lib.list_local_devices()
    return [x.name for x in local_device_protos if x.device_type == 'GPU']

def check_file_exists(parser, arg):
    try:
        data_file = str(arg)
    except ValueError:
        parser.error("Invalid String")
        return -1 
    if os.path.exists(arg):
        return data_file
    else :
        parser.error("Data File not Found")
    return -2

def is_valid_file(parser, arg):
    if not os.path.exists(arg):
        with open(arg, 'w') as f:
            f.write(','.join(['method_name', 'seed', 'num_GPUs', 'K', 'n_obs',
                        'n_dim', 'setup_time', 'initialization_time', 
                        'computation_time', 'n_iter']) + '\n')
    return str(arg)

def make_valid_int(parser, arg):
    try:
        ret = int(arg)
    except ValueError:
        parser.error("Invalid Integer")
        return -1    
    return ret

def make_valid_method(parser, arg):
    try:
        method_name = str(arg)
    except ValueError:
        parser.error("Invalid String")
        return -1 
    if method_name in ['distributedKMeans', 'distributedFuzzyCMeans']:
        return method_name
    else :
        parser.error("Invalid Method Name")
    return -2

def parse_valid_gpus_names(parser, arg):
    num_GPUs = make_valid_int(parser = parser,
                              arg    = arg    )
    gpu_names = get_available_gpus()
    available_num_GPUs = len(gpu_names)
    if num_GPUs > available_num_GPUs:
        parser.error("Number of GPUs Given is More Then Available")
        return -1
    if num_GPUs <= 0:
        parser.error("Number of GPUs Given is Non Positive")
        return -2
    gpus_to_use = np.random.choice(gpu_names, size = num_GPUs, replace = False)
    return gpus_to_use

def distribuited_fuzzy_C_means(X, K, GPU_names, initial_centers, n_max_iters, M = 2):
    setup_ts = time.time()
    number_of_gpus = len(GPU_names)
    
    X_list = np.split( X[ (X.shape[0] % number_of_gpus)  :, : ], number_of_gpus )
    
    partial_Mu_sum_list = []
    partial_Mu_X_sum_list = []
    
    with tf.name_scope('global'):
        with tf.device('/cpu:0'):
            global_centroids = tf.Variable(initial_centers)
            
    for GPU_num in range(number_of_gpus):
        GPU_name = GPU_names[GPU_num]
        
        (X_mat) = X_list.pop()
        (N, M) = X_mat.shape
  
        with tf.name_scope('scope_' + str(GPU_num)):
            with tf.device(GPU_name) :
                ####
                # In the coments we denote :
                # => N = Number of Observations
                # => M = Number of Dimensions
                # => K = Number of Centers
                ####
                # Data for GPU GPU_num to Clusterize
                X = tf.constant(X_mat)

                # Reshapes rep_centroids and  rep_points to format N x K x M so that 
                # the 2 matrixes have the same size
                rep_centroids = tf.reshape(tf.tile(global_centroids, [N, 1]), [N, K, M])
                rep_points = tf.reshape(tf.tile(X, [1, K]), [N, K, M])

                # Calculates sum_squares, a matrix of size N x K
                # This matrix is just(X-Y)^2
                dist_to_centers = tf.sqrt( tf.reduce_sum(tf.square(tf.subtract( rep_points, rep_centroids) ), 
                                                         reduction_indices = 2) )
                
                # Calculates cluster_membership, a matrix of size N x K
                tmp = tf.pow(dist_to_centers, -2 / (M - 1))
                cluster_membership_with_nan = tf.div( tf.transpose(tmp), tf.reduce_sum(tmp, 1))
                
                # Error treatment for when there are zeros in count_means_aux
                cluster_membership = tf.where(tf.is_nan(cluster_membership_with_nan), tf.zeros_like(cluster_membership_with_nan), cluster_membership_with_nan);
                
                MU = tf.pow(cluster_membership, M)
                
                # Calculates auxiliar matrixes 
                # Mu_X_sum of size 
                Mu_X_sum = tf.matmul(MU, X)
                Mu_sum = tf.reduce_sum(MU, 1)
                
                partial_Mu_sum_list.append( Mu_sum )
                partial_Mu_X_sum_list.append( Mu_X_sum )
                
    with tf.name_scope('global') :
        with tf.device('/cpu:0') :
            global_Mu_sum = tf.add_n( partial_Mu_sum_list )
            global_Mu_X_sum = tf.transpose(  tf.add_n(partial_Mu_X_sum_list) )
            
            new_centers = tf.transpose( tf.div(global_Mu_X_sum, global_Mu_sum) )
            
            update_centroid = tf.group( global_centroids.assign(new_centers) )
        
    setup_time = float( time.time() - setup_ts )
    initialization_ts = time.time()
    
    config = tf.ConfigProto( log_device_placement = True )
    config.gpu_options.allow_growth = True
    config.gpu_options.allocator_type = 'BFC'
    sess = tf.Session( config = config )
    
    init = tf.global_variables_initializer()
    sess.run(init)
    
    initialization_time = float( time.time() - initialization_ts ) 
    
    computation_time = 0.0
    for i in range(n_max_iters):
        aux_ts = time.time()
        [result, _] = sess.run([global_centroids, update_centroid])
        computation_time += float(time.time() - aux_ts)
    
    end_resut = {   'end_center'          : result             ,
                    'init_center'         : initial_centers    ,
                    'setup_time'          : setup_time         ,
                    'initialization_time' : initialization_time,
                    'computation_time'    : computation_time   ,
                    'n_iter'              : i+1
                }
    return end_resut

def old_distribuited_k_means(X, K, GPU_names, initial_centers, n_max_iters):
    setup_ts = time.time()
    number_of_gpus = len(GPU_names)

    X_list = np.split( X[ (X.shape[0] % number_of_gpus)  :, : ], number_of_gpus )
    
    partial_directions = []
    partial_values = []
    partial_results = []
    #partial_cost = [] ## Commented for performance
    
    with tf.name_scope('global'):
        with tf.device('/cpu:0'):
            global_centroids = tf.Variable(initial_centers)
            
    for GPU_num in range(number_of_gpus):
        GPU_name = GPU_names[GPU_num]
        
        (X_mat) = X_list.pop()
        (N, M) = X_mat.shape
  
        with tf.name_scope('scope_' + str(GPU_num)):
            with tf.device(GPU_name) :
                ####
                # In the coments we denote :
                # => N = Number of Observations
                # => M = Number of Dimensions
                # => K = Number of Centers
                ####
                # Data for GPU GPU_num to Clusterize
                X = tf.constant(X_mat)

                # Reshapes rep_centroids and rep_points to format N x K x M so that 
                # the 2 matrixes have the same size
                rep_centroids = tf.reshape(tf.tile(global_centroids, [N, 1]), [N, K, M])
                rep_points = tf.reshape(tf.tile(X, [1, K]), [N, K, M])

                # Calculates sum_squares, a matrix of size N x K
                # This matrix is not sqrt((X-Y)^2), it is just(X-Y)^2
                # Since we need just the argmin(sqrt((X-Y)^2)) wich is equal to 
                # argmin((X-Y)^2), it would be a waste of computation
                sum_squares = tf.reduce_sum(tf.square(tf.subtract( rep_points, rep_centroids) ), 
                                                reduction_indices = 2)

                # Use argmin to select the lowest-distance point
                # This gets a matrix of size N x 1
                best_centroids = tf.argmin(sum_squares, axis = 1)

                # This Sums vector X by the best_centroids indexes(assigned clusters)
                # And returns a vector of size K x M
                total_means_aux = tf.unsorted_segment_sum(X, best_centroids, K)

                # This counts how many data points by best_centroids indexes(assigned clusters)
                # And returns a vector of size K x M
                count_means_aux = tf.unsorted_segment_sum(tf.ones_like(X), best_centroids, K)
                
                # Calculates the new Center for this GPU
                # Returns a matrix of size K x M
                means_with_nan = tf.div( total_means_aux, count_means_aux )

                # Error treatment for when there are zeros in count_means_aux
                means = tf.where(tf.is_nan(means_with_nan), tf.zeros_like(means_with_nan), means_with_nan);

                # Cost Function, wihch would used for stopping criteria
                #cost = tf.reduce_sum( tf.reduce_min(sum_squares, axis = 1) ) ## Commented for performance
                #partial_cost.append(cost) ## Commented for performance
                
            with tf.device('/cpu:0'):
                y_count = tf.bincount(tf.to_int32(best_centroids), maxlength = K, minlength = K)
                y_count_float = tf.cast(y_count, dtype = tf.float64)
                partial_mu =  tf.multiply( tf.transpose(means), y_count_float )

                partial_directions.append( y_count_float )
                partial_values.append( partial_mu )
                
    with tf.name_scope('global') :
        with tf.device('/cpu:0') :
            sum_direction = tf.add_n( partial_directions )
            sum_mu = tf.add_n( partial_values )
            #total_cost = tf.add_n( partial_cost )

            rep_sum_direction = tf.reshape(tf.tile(sum_direction, [M]), [M, K])
            new_centers = tf.transpose( tf.div(sum_mu, rep_sum_direction) )

            update_centroid = tf.group( global_centroids.assign(new_centers) )
        
    setup_time = float( time.time() - setup_ts )
    
    initialization_ts = time.time()
    
    config = tf.ConfigProto( log_device_placement = True )
    config.gpu_options.allow_growth = True
    config.gpu_options.allocator_type = 'BFC'
    sess = tf.Session( config = config )
    
    init = tf.global_variables_initializer()
    sess.run(init)
    
    initialization_time = float( time.time() - initialization_ts ) 
    
    computation_time = 0.0
    for i in range(n_max_iters):
        aux_ts = time.time()
        [result, _] = sess.run([global_centroids, update_centroid])
        computation_time += float(time.time() - aux_ts)
    
    end_resut = {   'end_center'          : result             ,
                    'init_center'         : initial_centers    ,
                    'setup_time'          : setup_time         ,
                    'initialization_time' : initialization_time,
                    'computation_time'    : computation_time   ,
                    'n_iter'              : i+1
                }

    return end_resut

def distribuited_k_means(X, K, GPU_names, initial_centers, n_max_iters):
    setup_ts = time.time()
    number_of_gpus = len(GPU_names)

    X_list = np.split( X[ (X.shape[0] % number_of_gpus)  :, : ], number_of_gpus )
    
    partial_directions = []
    partial_values = []
    partial_results = []

    config = tf.ConfigProto( log_device_placement = True, allow_soft_placement = True )
    config.gpu_options.allow_growth = True
    config.gpu_options.allocator_type = 'BFC'
    sess = tf.Session( config = config )
    
    with tf.name_scope('global'):
        with tf.device('/cpu:0'):
            global_centroids = tf.Variable(initial_centers)

    initialization_ts = time.time()
    sess.run(tf.global_variables_initializer())
    initialization_time = float( time.time() - initialization_ts ) 
            
    for GPU_num in range(len(GPU_names)):
        GPU_name = GPU_names[GPU_num]
            
        (X_mat) = X_list[GPU_num]
        (N, M) = X_mat.shape
        
        with tf.name_scope('scope_' + str(GPU_num)):
            with tf.device(GPU_name) :
                ####
                # In the coments we denote :
                # => N = Number of Observations
                # => M = Number of Dimensions
                # => K = Number of Centers
                ####

                # Data for GPU GPU_num to Clusterize
                X = tf.constant(X_mat)

                # Reshapes rep_centroids and rep_points to format N x K x M so that 
                # the 2 matrixes have the same size
                rep_centroids = tf.reshape(tf.tile(global_centroids, [N, 1]), [N, K, M])
                rep_points = tf.reshape(tf.tile(X, [1, K]), [N, K, M])

                # Calculates sum_squares, a matrix of size N x K
                # This matrix is not sqrt((X-Y)^2), it is just(X-Y)^2
                # Since we need just the argmin(sqrt((X-Y)^2)) wich is equal to 
                # argmin((X-Y)^2), it would be a waste of computation
                sum_squares = tf.reduce_sum(tf.square(tf.subtract( rep_points, rep_centroids) ), axis = 2)

                # Use argmin to select the lowest-distance point
                # This gets a matrix of size N x 1
                best_centroids = tf.argmin(sum_squares, axis = 1)
                
                means = []
                for c in range(K):
                    means.append(
                        tf.reduce_mean(
                            tf.gather(X, tf.reshape(tf.where(tf.equal(best_centroids, c)), [1,-1])), axis=[1]))

                new_centroids = tf.concat(means, 0)
                    
            with tf.device('/cpu:0'):
                y_count = tf.cast(
                    tf.bincount(tf.to_int32(best_centroids), maxlength = K, minlength = K), dtype = tf.float64)
                
                partial_mu =  tf.multiply( tf.transpose(new_centroids), y_count )

                partial_directions.append( y_count )
                partial_values.append( partial_mu )
                
    with tf.name_scope('global') :
        with tf.device('/cpu:0') :
            sum_direction = tf.add_n( partial_directions )
            sum_mu = tf.add_n( partial_values )

            rep_sum_direction = tf.reshape(tf.tile(sum_direction, [M]), [M, K])
            new_centers = tf.transpose( tf.div(sum_mu, rep_sum_direction) )

            update_centroid = tf.group( global_centroids.assign(new_centers) )
        
    setup_time = float( time.time() - setup_ts )
    
    computation_time = 0.0
    for i in range(n_max_iters):
        aux_ts = time.time()
        [result, _] = sess.run([global_centroids, update_centroid])
        computation_time += float(time.time() - aux_ts)
    
    end_resut = {   'end_center'          : result             ,
                    'init_center'         : initial_centers    ,
                    'setup_time'          : setup_time         ,
                    'initialization_time' : initialization_time,
                    'computation_time'    : computation_time   ,
                    'n_iter'              : i+1
                }

    return end_resut

def make_data(n_obs, n_dim, seed):
    (X, Y) = make_classification(n_samples            = n_obs    , 
                                 n_features           = n_dim    ,
                                 n_informative        = n_dim    ,
                                 n_redundant          = 0        ,
                                 n_classes            = 2        ,
                                 n_clusters_per_class = 1        ,
                                 shuffle              = True     ,
                                 random_state         = seed      )
    return (X, Y)

def main(n_obs, n_dim, K, GPU_names, n_max_iters, seed , log_file, method_name, data_file):

    data = np.load(data_file)
    X = data['X']
    Y = data['Y']
    initial_centers = X[0:K, :]
    return_status = 0

    try:
        if method_name == 'distributedKMeans':
            run_result = distribuited_k_means(X               = X              ,
                                              K               = K              ,
                                              GPU_names       = GPU_names      ,
                                              n_max_iters     = n_max_iters    ,
                                              initial_centers = initial_centers )

        if method_name == 'distributedFuzzyCMeans':
            run_result = distribuited_fuzzy_C_means(X               = X              ,
                                                    K               = K              ,
                                                    GPU_names       = GPU_names      ,
                                                    n_max_iters     = n_max_iters    ,
                                                    initial_centers = initial_centers )

    except:
        exc_type, exc_value, exc_tb = sys.exc_info()
        print(traceback.print_tb(exc_tb))
        exc_name = exc_type.__name__

        run_result = {  'end_center'          : exc_name     ,
                        'init_center'         : exc_name     ,
                        'setup_time'          : exc_name     ,
                        'initialization_time' : exc_name     ,
                        'computation_time'    : exc_name     ,
                        'n_iter'              : n_max_iters

                     }

        return_status =  1 if exc_name == 'ValueError' else 0

    finally:

        data_to_append = {  'method_name'          : method_name                      ,
                            'seed'                 : seed                             ,
                            'num_GPUs'             : len(GPU_names)                   ,
                            'K'                    : K                                ,
                            'n_obs'                : n_obs                            ,
                            'n_dim'                : n_dim                            ,
                            'setup_time'           : run_result['setup_time']         ,
                            'initialization_time'  : run_result['initialization_time'],
                            'computation_time'     : run_result['computation_time']   ,
                            'n_iter'               : run_result['n_iter']               
                         }

        str_to_write = ','.join([ str( data_to_append['method_name'] ),
                                    str( data_to_append['seed'] ),
                                    str( data_to_append['num_GPUs'] ),
                                    str( data_to_append['K'] ),
                                    str( data_to_append['n_obs'] ),
                                    str( data_to_append['n_dim'] ),
                                    str( data_to_append['setup_time'] ),
                                    str( data_to_append['initialization_time'] ),
                                    str( data_to_append['computation_time'] ),
                                    str( data_to_append['n_iter'] ) ])

        str_to_write = str(str_to_write) + '\n'
        print('log_file =', log_file)

        with open(log_file, 'a') as f:
            f.write(str_to_write)    

    return return_status

if __name__ == "__main__":
  parser = argparse.ArgumentParser(description = 'Compile Distribuited K Means Results.')
  
  parser.add_argument("--n_obs"                                             ,
                      dest     = "n_obs"                                    ,
                      required = True                                       ,
                      metavar  = "int"                                      ,
                      type     = lambda x: make_valid_int(parser, x)        ,
                      help     = "Number of Observations for the Test !!!" )

  parser.add_argument("--n_dim"                                         ,
                      dest     = "n_dim"                                ,
                      required = True                                   ,
                      metavar  = "int"                                  ,
                      type     = lambda x: make_valid_int(parser, x)    ,
                      help     = "Number of Dimensions for the Test !!!" )

  parser.add_argument("--K"                                             ,
                      dest     = "K"                                    ,
                      required = True                                   ,
                      metavar  = "int"                                  ,
                      type     = lambda x: make_valid_int(parser, x)    ,
                      help     = "Number of K Centers for the Test !!!" )

  parser.add_argument("--n_GPUs"                                                ,
                      dest     = "GPU_names"                                    ,
                      required = True                                           ,
                      metavar  = "int"                                          ,
                      type     = lambda x: parse_valid_gpus_names(parser, x)    ,
                      help     = "Number of GPUs !!!" )

  parser.add_argument("--n_max_iters"                                           ,
                      dest     = "n_max_iters"                                  ,
                      required = True                                           ,
                      metavar  = "int"                                          ,
                      type     = lambda x: make_valid_int(parser, x)            ,
                      help     = "Number of iterations before stopping!!!" )

  parser.add_argument("--seed"                                      ,
                      dest     = "seed"                             ,
                      required = True                               ,
                      metavar  = "int"                              ,
                      type     = lambda x: make_valid_int(parser, x),
                      help     = "Seed Value !!!"                    )

  parser.add_argument("--log_file"                                       ,
                      dest     = "log_file"                              ,
                      required = True                                    ,
                      metavar  = "FILE"                                  ,
                      type     = lambda x: is_valid_file(parser, x)      ,
                      help     = "log_file Name, this would be a CSV !!!" )

  parser.add_argument("--method_name"                                                   ,
                      dest     = "method_name"                                          ,
                      required = True                                                   ,
                      metavar  = "str"                                                  ,
                      type     = lambda x: make_valid_method(parser, x)                 ,
                      help     = "Method Name Can Be :" + 
                                 "distribuitedKMeans or distribuitedFuzzyCMeans !!!" )

  parser.add_argument("--data_file"                                                     ,
                      dest     = "data_file"                                            ,
                      required = True                                                   ,
                      metavar  = "str"                                                  ,
                      type     = lambda x: check_file_exists(parser, x)                 ,
                      help     = "Unable to find data file !!!" )

  args = parser.parse_args()

  status = main(n_obs       = args.n_obs      ,
                n_dim       = args.n_dim      ,
                K           = args.K          ,
                GPU_names   = args.GPU_names  ,
                n_max_iters = args.n_max_iters,
                seed        = args.seed       ,
                log_file    = args.log_file   ,
                method_name = args.method_name,
                data_file   = args.data_file )

  sys.exit(status)
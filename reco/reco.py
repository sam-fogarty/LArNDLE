#!/usr/bin/env python
"""
Command-line interface to LArNDLE
"""
from build_events import *
from preclustering import *
import charge_event_cuts
import matching
from light import *
import h5py
import fire
import time
import os
from tqdm import tqdm
from adc64format import dtypes, ADC64Reader
import importlib.util

def reco_loop(nSec_start, nSec_end, PPS_indices, packets, mc_assn, pixel_xy, detector):
    ## loop through seconds of data and do charge reconstruction
    for sec in tqdm(range(nSec_start,int(nSec_end)+1),desc=" Seconds Processed: "):
        # Grab 1s at a time to analyze, plus the next 1s.
        # Each 1s is determined by getting the packets between PPS pulses (packet type 6).
        if sec == 1:
            packets_1sec = packets[0:PPS_indices[sec-1]]
            packets_nextPPS = packets[PPS_indices[sec-1]:PPS_indices[sec]]
            if mc_assn != None:
                mc_assn_1sec = mc_assn[0:PPS_indices[sec-1]]
                mc_assn_nextPPS = mc_assn[PPS_indices[sec-1]:PPS_indices[sec]]
        elif sec >= nSec_start and sec <= nSec_end:
            packets_1sec = packets[PPS_indices[sec-2]:PPS_indices[sec-1]]
            packets_nextPPS = packets[PPS_indices[sec-1]:PPS_indices[sec]]
            if mc_assn != None:
                mc_assn_1sec = mc_assn[PPS_indices[sec-2]:PPS_indices[sec-1]]
                mc_assn_nextPPS = mc_assn[PPS_indices[sec-1]:PPS_indices[sec]]
        
        # remove packets from the 1sec that belongs in the previous second
        packets_1sec_receipt_diff_mask = (packets_1sec['receipt_timestamp'].astype(int) - packets_1sec['timestamp'].astype(int) < 0)\
                & (packets_1sec['packet_type'] == 0)
        packets_1sec = packets_1sec[np.invert(packets_1sec_receipt_diff_mask)]
        
        # move packets from nextPPS to 1sec that belong 1sec earlier
        packets_nextPPS_receipt_diff_mask = (packets_nextPPS['receipt_timestamp'].astype(int) - packets_nextPPS['timestamp'].astype(int) < 0) \
                & (packets_nextPPS['packet_type'] == 0)
        # move those packets from nextPPS to 1sec. Now we will only work on packets_1sec
        packets_1sec = np.concatenate((packets_1sec, packets_nextPPS[packets_nextPPS_receipt_diff_mask]))
        if mc_assn != None:
            mc_assn_1sec = mc_assn_1sec[np.invert(packets_1sec_receipt_diff_mask)]
            mc_assn_1sec = np.concatenate((mc_assn_1sec, mc_assn_nextPPS[packets_nextPPS_receipt_diff_mask]))
        else:
            mc_assn_1sec = None
        # run reconstruction on selected packets.
        # this block is be run first and thus defines all the arrays for concatenation later.
        if sec == nSec_start:
            results_small_clusters, results_large_clusters, unix_pt7, PPS_pt7,\
                hits_small_clusters, hits_large_clusters = analysis(packets_1sec, pixel_xy, mc_assn_1sec, detector, 0, 0)
        elif sec > nSec_start:
            # making sure to continously increment cluster_index as we go onto the next PPS
            hits_small_clusters_max_cindex = np.max(hits_small_clusters['cluster_index'])+1
            if np.size(hits_large_clusters['cluster_index']) > 0:
                hits_large_clusters_max_cindex = np.max(hits_large_clusters['cluster_index'])+1
            else:
                hits_large_clusters_max_cindex = 0
            # run reconstruction and save temporary arrays of results
            results_small_clusters_temp, results_large_clusters_temp, unix_pt7_temp, PPS_pt7_temp,\
                hits_small_clusters_temp,hits_large_clusters_temp = analysis(packets_1sec, pixel_xy, mc_assn_1sec, detector,\
                                        hits_small_clusters_max_cindex, hits_large_clusters_max_cindex)
            # concatenate temp arrays to main arrays
            results_small_clusters = np.concatenate((results_small_clusters, results_small_clusters_temp))
            results_large_clusters = np.concatenate((results_large_clusters, results_large_clusters_temp))
            hits_small_clusters = np.concatenate((hits_small_clusters, hits_small_clusters_temp))
            hits_large_clusters = np.concatenate((hits_large_clusters, hits_large_clusters_temp))
            unix_pt7 = np.concatenate((unix_pt7, unix_pt7_temp))
            PPS_pt7 = np.concatenate((PPS_pt7, PPS_pt7_temp))
    return results_small_clusters, results_large_clusters, unix_pt7,PPS_pt7, hits_small_clusters, hits_large_clusters
    
def run_reconstruction(input_config_filename):
    ## main function
    
    # import input variables. Get variables with module.<variable>
    input_config_filepath = 'input_config/' + input_config_filename
    module_name = "detector_module"
    spec = importlib.util.spec_from_file_location(module_name, input_config_filepath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    
    # set variables from config file
    detector = module.detector
    input_packets_filename = module.input_packets_filename
    input_light_filename_1 = module.input_light_filename_1
    input_light_filename_2 = module.input_light_filename_2
    output_events_filename = module.output_events_filename
    nSec_start = module.nSec_start_packets
    nSec_end = module.nSec_end_packets
    nSec_start_light = module.nSec_start_light
    nSec_end_light = module.nSec_end_light
    sync_filename = module.sync_filename
    light_time_steps = module.light_time_steps
    nchannels_adc1 = module.nchannels_adc1
    nchannels_adc2 = module.nchannels_adc2
    matching_tolerance_unix = module.matching_tolerance_unix
    matching_tolerance_PPS = module.matching_tolerance_PPS
    
    packets_filename_base = input_packets_filename.split('.h5')[0]
    packets_folder = charge_data_folder + detector + '/'
    input_packets_filepath = packets_folder + input_packets_filename
    output_events_filepath = packets_folder + output_events_filename
    
    if detector not in ['module-0', 'module-3']:
        raise Exception("Possible values of 'detector' are only 'module-0' and 'module-3' (without quotes).")
    if os.path.exists(output_events_filepath):
        raise Exception('Output file '+ str(output_events_filepath) + ' already exists.')
    if nSec_start <= 0 or nSec_start - int(nSec_start) or nSec_end < -1 or nSec_end - int(nSec_end) > 0:
        raise ValueError('nSec_start and nSec_end must be greater than zero and be an integer.')
    if input_packets_filename.split('.')[-1] != 'h5':
        raise Exception('Input file must be an h5 file.')
    
    # load dictionary for calculating hit position on pixel plane. Made using larpix-readout-parser.
    if detector == 'module-0':
        dict_path = 'layout/module-0/multi_tile_layout-2.3.16.pkl'
    elif detector == 'module-3':
        dict_path = 'layout/module-3/multi_tile_layout-module3.pkl'
    pixel_xy = load_geom_dict(dict_path)
    print('Using pixel layout dictionary: ', dict_path)
    
    # open packets file
    print('Opening packets file: ', input_packets_filepath)
    f_packets = h5py.File(input_packets_filepath)
    try:
        f_packets['packets']
    except: 
        raise KeyError('Packets not found in ' + input_packets_filepath)
    
    analysis_start = time.time()
    
    # open mc_assn dataset for MC
    mc_assn=None
    try:
        mc_assn = f_packets['mc_packets_assn']
    except:
        mc_assn=None
        print("No 'mc_packets_assn' dataset found, processing as real data.")
    
    # get packets and indices of PPS pulses
    packets = f_packets['packets']
    if sync_filename is not None:
        # this is an option to load a pre-determined mask for the sync packets,
        # where a different sync file needs to be made for each packets file
        sync_filepath = charge_data_folder + detector + '/' + sync_filename
        print('Loading sync mask file ', sync_filepath, ' ...')
        PPS_mask_file = np.load(sync_filepath)
        PPS_mask = PPS_mask_file[PPS_mask_file.files[0]]
        PPS_indices = np.where(PPS_mask)[0]
    else:
        # note this can take at least a few minutes sometimes
        print('Finding sync packets on the fly (may take a few minutes)...')
        PPS_indices = np.where((packets['packet_type'] == 6) & (packets['trigger_type'] == 83))[0]

    if nSec_end == -1:
        nSec_end = len(PPS_indices)-1
        print('nSec_end was set to -1, so setting nSec_end to final second in data of ', nSec_end)
    
    print('Processing '+ str(nSec_end - nSec_start) + ' seconds of data, starting at '+\
         str(nSec_start) + ' seconds and stopping at ', str(nSec_end) + ' ...')
    
    # run reconstruction
    results_small_clusters, results_large_clusters, unix_pt7, PPS_pt7, hits_small_clusters,hits_large_clusters = \
        reco_loop(nSec_start, nSec_end, PPS_indices, packets, mc_assn, pixel_xy, detector)
    
    # do cuts on charge events. See toggles for cuts in consts.py.
    # if all toggles are False then this command simply returns `results` unchanged.
    #results_small_clusters = charge_event_cuts.all_charge_event_cuts(results_small_clusters)
    
    if do_match_of_charge_to_light:
        # loop through the light files and only select triggers within the second ranges specified
        # note that not all these light events will have a match within the packets data selection
        input_light_filepath_1 = adc_folder + detector + '/' + input_light_filename_1
        input_light_filepath_2 = adc_folder + detector + '/' +  input_light_filename_2
        adc_sn_1 = input_light_filename_1.split('_')[0]
        adc_sn_2 = input_light_filename_2.split('_')[0]
        print('Using light file ', input_light_filepath_1)
        print('Using light file ', input_light_filepath_2)
        print('Loading light files with a batch size of ', batch_size, ' ...')
        light_events_all = read_light_files(input_light_filepath_1, input_light_filepath_2, nSec_start_light, nSec_end_light, detector, light_time_steps, nchannels_adc1, nchannels_adc2,adc_sn_1, adc_sn_2)
    
        # match light events to ext triggers in packets (packet type 7)
        light_events_all = light_events_all[light_events_all['unix'] != 0]
        print('Matching light triggers to external triggers in packets...')
        indices_in_ext_triggers = matching.match_light_to_ext_trigger(light_events_all, PPS_pt7, unix_pt7, \
            matching_tolerance_unix, matching_tolerance_PPS) # length of light_events_all
        evt_mask = indices_in_ext_triggers != -1
        indices_in_ext_triggers = indices_in_ext_triggers[evt_mask]
        light_events_all = light_events_all[evt_mask]
        PPS_pt7_light = PPS_pt7[indices_in_ext_triggers]
        unix_pt7_light = unix_pt7[indices_in_ext_triggers]
        
        # match ext triggers / light events to charge events for the small clusters
        print('Performing charge-light matching for the small clusters...')
        results_small_clusters, results_small_clusters_light_events = matching.match_light_to_charge(light_events_all, results_small_clusters, PPS_pt7_light, unix_pt7_light,0)
        
        # match ext triggers / light events to charge events for the large clusters
        print('Performing charge-light matching for the large clusters...')
        results_large_clusters, results_large_clusters_light_events = matching.match_light_to_charge(light_events_all, results_large_clusters, PPS_pt7_light, unix_pt7_light,1)
    else:
        print('Skipping getting light events and doing charge-light matching...')
    
    print('Saving events to ', output_events_filepath)
    with h5py.File(output_events_filepath, 'w') as f:
        dset_small_clusters = f.create_dataset('small_clusters', data=results_small_clusters, dtype=results_small_clusters.dtype)
        dset_hits_small_clusters = f.create_dataset('small_clusters_hits', data=hits_small_clusters, dtype=hits_small_clusters.dtype)
        if do_match_of_charge_to_light:
            dset_light_events = f.create_dataset('small_clusters_matched_light', data=results_small_clusters_light_events, dtype=results_small_clusters_light_events.dtype)
            dset_light_events = f.create_dataset('large_clusters_matched_light', data=results_large_clusters_light_events, dtype=results_large_clusters_light_events.dtype)
        dset_large_clusters = f.create_dataset('large_clusters', data=results_large_clusters, dtype=results_large_clusters.dtype)
        dset_hits_large_clusters = f.create_dataset('large_clusters_hits', data=hits_large_clusters, dtype=hits_large_clusters.dtype)
    
    analysis_end = time.time()
    print('Time to do full analysis = ', analysis_end-analysis_start, ' seconds')
    print('Total small clusters = ', len(results_small_clusters), ' with a rate of ', len(results_small_clusters)/(nSec_end-nSec_start), ' Hz')
    print('Total large clusters = ', len(results_large_clusters), ' with a rate of ', len(results_large_clusters)/(nSec_end - nSec_start), ' Hz')

if __name__ == "__main__":
    fire.Fire(run_reconstruction)
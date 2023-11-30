from .si_based import ComponentsBasedSorter

import os
import shutil
import numpy as np

from spikeinterface.core import NumpySorting, load_extractor, BaseRecording, get_noise_levels, extract_waveforms
from spikeinterface.core.job_tools import fix_job_kwargs
from spikeinterface.preprocessing import common_reference, zscore, whiten, highpass_filter

try:
    import hdbscan

    HAVE_HDBSCAN = True
except:
    HAVE_HDBSCAN = False


class Spykingcircus2Sorter(ComponentsBasedSorter):
    sorter_name = "spykingcircus2"

    _default_params = {
        "general": {"ms_before": 2, "ms_after": 2, "radius_um": 100},
        "waveforms": {
            "max_spikes_per_unit": 200,
            "overwrite": True,
            "sparse": True,
            "method": "energy",
            "threshold": 0.25,
        },
        "filtering": {"freq_min": 150, "dtype": "float32"},
        "detection": {"peak_sign": "neg", "detect_threshold": 4},
        "selection": {
            "method": "smart_sampling_amplitudes",
            "n_peaks_per_channel": 5000,
            "min_n_peaks": 20000,
            "select_per_channel": False,
        },
        "clustering": {"legacy": False},
        "matching": {"method": "circus-omp-svd", "method_kwargs": {}},
        "apply_preprocessing": True,
        "shared_memory": True,
        "multi_units_only": False,
        "job_kwargs": {"n_jobs": 0.8},
        "debug": False,
    }

    handle_multi_segment = True

    _params_description = {
        "general": "A dictionnary to describe how templates should be computed. User can define ms_before and ms_after (in ms) \
                                        and also the radius_um used to consider during clustering",
        "waveforms": "A dictionnary to be passed to all the calls to extract_waveforms that will be perfomed internally. Default is \
                                        to consider sparse waveforms",
        "filtering": "A dictionnary for the high_pass filer used during preprocessing",
        "detection": "A dictionnary for the peak detection node (locally_exclusive)",
        "selection": "A dictionnary for the peak selection node. Default is to use smart_sampling_amplitudes, with a minimum of 20000 peak_sign\
                                         and 5000 peaks per electrode on average.",
        "clustering": "A dictionnary to be provided to the clustering method. By default, we used random_projections, but if legacy is set to\
                            True, one other clustering called circus will be used, similar to the one used in Spyking Circus 1",
        "matching": "A dictionnary to specify the matching engine used to recover spikes. The method default is circus-omp-svd, but other engines\
                                          can be used",
        "apply_preprocessing": "Boolean to specify is circus 2 should preprocess the recording or not. If yes, then high_pass filtering + common\
                                                    median reference + zscore",
        "shared_memory": "Boolean to specify if the code should use, as much as possible, to put internal datastructure in memory (faster)",
        "multi_units_only": "Boolean to get only multi units activity (i.e. one template per electrode)",
        "job_kwargs": "A dictionnary to specify how many jobs and which parameters they should used",
        "debug": "Boolean to specify if internal datastructure should be kept for debugging",
    }

    sorter_description = """Spyking Circus 2 is a rewritting of Spyking Circus, within the SpikeInterface framework
    It uses a more conservative clustering (compared to Spyking Circus) less prone to hallucinate units and/or find noise.
    In addition, it also uses a full Orthogonal Matching Pursuit engine to reconstruct the traces, leading to more spikes
    that can be discovered."""

    @classmethod
    def get_sorter_version(cls):
        return "2.0"

    @classmethod
    def _run_from_folder(cls, sorter_output_folder, params, verbose):
        assert HAVE_HDBSCAN, "spykingcircus2 needs hdbscan to be installed"

        # this is importanted only on demand because numba import are too heavy
        from spikeinterface.sortingcomponents.peak_detection import detect_peaks
        from spikeinterface.sortingcomponents.peak_selection import select_peaks
        from spikeinterface.sortingcomponents.clustering import find_cluster_from_peaks
        from spikeinterface.sortingcomponents.matching import find_spikes_from_templates

        job_kwargs = params["job_kwargs"].copy()
        job_kwargs = fix_job_kwargs(job_kwargs)
        job_kwargs["verbose"] = verbose
        job_kwargs["progress_bar"] = verbose

        recording = cls.load_recording_from_folder(sorter_output_folder.parent, with_warnings=False)

        sampling_frequency = recording.get_sampling_frequency()
        num_channels = recording.get_num_channels()

        ## First, we are filtering the data
        filtering_params = params["filtering"].copy()
        if params["apply_preprocessing"]:
            recording_f = highpass_filter(recording, **filtering_params)
            recording_f = common_reference(recording_f)
        else:
            recording_f = recording
            recording_f.annotate(is_filtered=True)

        # recording_f = whiten(recording_f, dtype="float32")
        recording_f = zscore(recording_f, dtype="float32")
        noise_levels = np.ones(num_channels, dtype=np.float32)

        ## Then, we are detecting peaks with a locally_exclusive method
        detection_params = params["detection"].copy()
        detection_params.update(job_kwargs)
        if "radius_um" not in detection_params:
            detection_params["radius_um"] = params["general"]["radius_um"]
        if "exclude_sweep_ms" not in detection_params:
            detection_params["exclude_sweep_ms"] = max(params["general"]["ms_before"], params["general"]["ms_after"])

        peaks = detect_peaks(recording_f, method="locally_exclusive", **detection_params)

        if verbose:
            print("We found %d peaks in total" % len(peaks))

        if params["multi_units_only"]:
            sorting = NumpySorting.from_peaks(peaks, sampling_frequency)
        else:
            ## We subselect a subset of all the peaks, by making the distributions os SNRs over all
            ## channels as flat as possible
            selection_params = params["selection"]
            selection_params["n_peaks"] = params["selection"]["n_peaks_per_channel"] * num_channels
            selection_params["n_peaks"] = max(selection_params["min_n_peaks"], selection_params["n_peaks"])

            selection_params.update({"noise_levels": noise_levels})
            selected_peaks = select_peaks(peaks, **selection_params)

            if verbose:
                print("We kept %d peaks for clustering" % len(selected_peaks))

            ## We launch a clustering (using hdbscan) relying on positions and features extracted on
            ## the fly from the snippets
            clustering_params = params["clustering"].copy()
            clustering_params["waveforms"] = params["waveforms"].copy()

            for k in ["ms_before", "ms_after"]:
                clustering_params["waveforms"][k] = params["general"][k]

            clustering_params.update(dict(shared_memory=params["shared_memory"]))
            clustering_params["job_kwargs"] = job_kwargs
            clustering_params["tmp_folder"] = sorter_output_folder / "clustering"

            if "legacy" in clustering_params:
                legacy = clustering_params.pop("legacy")
            else:
                legacy = False

            if legacy:
                clustering_method = "circus"
            else:
                clustering_method = "random_projections"

            labels, peak_labels = find_cluster_from_peaks(
                recording_f, selected_peaks, method=clustering_method, method_kwargs=clustering_params
            )

            ## We get the labels for our peaks
            mask = peak_labels > -1
            sorting = NumpySorting.from_times_labels(
                selected_peaks["sample_index"][mask], peak_labels[mask].astype(int), sampling_frequency
            )
            clustering_folder = sorter_output_folder / "clustering"
            clustering_folder.mkdir(parents=True, exist_ok=True)

            if not params["debug"]:
                shutil.rmtree(clustering_folder)
            else:
                np.save(clustering_folder / "labels", labels)
                np.save(clustering_folder / "peaks", selected_peaks)

            ## We get the templates our of such a clustering
            waveforms_params = params["waveforms"].copy()
            waveforms_params.update(job_kwargs)

            for k in ["ms_before", "ms_after"]:
                waveforms_params[k] = params["general"][k]

            if params["shared_memory"] and not params["debug"]:
                mode = "memory"
                waveforms_folder = None
            else:
                sorting = sorting.save(folder=clustering_folder)
                mode = "folder"
                waveforms_folder = sorter_output_folder / "waveforms"

            we = extract_waveforms(
                recording_f,
                sorting,
                waveforms_folder,
                return_scaled=False,
                precompute_template=["median"],
                mode=mode,
                **waveforms_params,
            )

            ## We launch a OMP matching pursuit by full convolution of the templates and the raw traces

            matching_method = params["matching"]["method"]

            matching_params = params["matching"]["method_kwargs"].copy()
            matching_job_params = {}
            matching_job_params.update(job_kwargs)
            if matching_method == "wobble":
                matching_params["templates"] = we.get_all_templates(mode="median")
                matching_params["nbefore"] = we.nbefore
                matching_params["nafter"] = we.nafter
            else:
                matching_params["waveform_extractor"] = we

            if matching_method == "circus-omp-svd":
                for value in ["chunk_size", "chunk_memory", "total_memory", "chunk_duration"]:
                    if value in matching_job_params:
                        matching_job_params.pop(value)
                matching_job_params["chunk_duration"] = "100ms"

            spikes = find_spikes_from_templates(
                recording_f, matching_method, method_kwargs=matching_params, **matching_job_params
            )

            if params["debug"]:
                fitting_folder = sorter_output_folder / "fitting"
                fitting_folder.mkdir(parents=True, exist_ok=True)
                np.save(fitting_folder / "spikes", spikes)

            if verbose:
                print("We found %d spikes" % len(spikes))

            ## And this is it! We have a spyking circus
            sorting = NumpySorting.from_times_labels(
                spikes["sample_index"], spikes["cluster_index"], sampling_frequency
            )

        sorting_folder = sorter_output_folder / "sorting"
        if sorting_folder.exists():
            shutil.rmtree(sorting_folder)

        sorting = sorting.save(folder=sorting_folder)

        return sorting

from __future__ import print_function, division, absolute_import#, unicode_literals # not casa compatible
from builtins import bytes, dict, object, range, map, input#, str # not casa compatible
from future.utils import itervalues, viewitems, iteritems, listvalues, listitems
from io import open

import distributed
from dask import array, delayed
from rfpipe import source, search, util, candidates
from dask.base import tokenize
import numpy as np

import logging
logger = logging.getLogger(__name__)
vys_timeout_default = 10


def pipeline_scan(st, segments=None, cl=None, host=None, cfile=None,
                  vys_timeout=vys_timeout_default):
    """ Given rfpipe state and dask distributed client, run search pipline.
    throttle option will submit only if worker state allows for it.
    parameters of throttling:
      read_overhead scales vismem to peak usage during read.
      read_totfrac is total memory usage allowed in bytes at submission time.
    """

    if cl is None:
        if host is None:
            cl = distributed.Client(n_workers=1, threads_per_worker=16,
                                    resources={"READER": 1, "MEMORY": 24,
                                               "CORES": 16},
                                    local_dir="/lustre/evla/test/realfast/scratch")
        else:
            cl = distributed.Client('{0}:{1}'.format(host, '8786'))

    if not isinstance(segments, list):
        segments = list(range(st.nsegment))

    futures = []
    for segment in segments:
        futures.append(pipeline_seg(st, segment, cl=cl, cfile=cfile,
                                    vys_timeout=vys_timeout))

    return futures  # list of dicts


def pipeline_seg(st, segment, cl, cfile=None,
                 vys_timeout=vys_timeout_default):
    """ Submit pipeline processing of a single segment to scheduler.
    Can use distributed client or compute locally.

    Uses distributed resources parameter to control scheduling of GPUs.
    Pipeline produces jobs per DM/dt.
    Returns a dict with values as futures of certain jobs (data, collection).
    """

    logger.info('Building dask for observation {0}, scan {1}, segment {2}.'
                .format(st.metadata.datasetId, st.metadata.scan, segment))

    futures = {}

# new style read *TODO: note hack on resources*
    data = lazy_read_segment(st, segment, cfile, vys_timeout)
    data = cl.compute(data, resources={tuple(data.__dask_keys__()[0][0][0]):
                                       {'READER': 1}})  # get future
# old style read
#    data = cl.submit(source.read_segment, st, segment, cfile, vys_timeout,
#                     resources={'READER': 1})

    futures['data'] = data  # save future

    # TODO: put this on READER worker?
    data_prep = cl.submit(source.data_prep, st, segment, data,
                          resources={'CORES': st.prefs.nthread},
                          priority=1)

    saved = []
    if st.fftmode == "fftw":
        searchresources = {'MEMORY': 2*st.immem+2*st.vismem,
                           'CORES': st.prefs.nthread}
        imgranges = [[(min(st.get_search_ints(segment, dmind, dtind)),
                     max(st.get_search_ints(segment, dmind, dtind)))
                      for dtind in range(len(st.dtarr))]
                     for dmind in range(len(st.dmarr))]
        wisdom = cl.submit(search.set_wisdom, st.npixx, st.npixy,
                           resources={'CORES': 1})

        for dmind in range(len(st.dmarr)):
            delay = cl.submit(util.calc_delay, st.freq, st.freq.max(),
                              st.dmarr[dmind], st.inttime,
                              resources={'CORES': 1})
            for dtind in range(len(st.dtarr)):
                data_corr = cl.submit(search.dedisperseresample, data_prep,
                                      delay, st.dtarr[dtind],
                                      parallel=st.prefs.nthread > 1,
                                      resources={'MEMORY': 2*st.vismem,
                                                 'CORES': st.prefs.nthread})

                im0, im1 = imgranges[dmind][dtind]
                integrationlist = [list(range(im0, im1)[i:i+st.chunksize])
                                   for i in range(0, im1-im0, st.chunksize)]
                for integrations in integrationlist:
                    saved.append(cl.submit(search.search_thresh_fftw, st,
                                           segment, data_corr, dmind, dtind,
                                           integrations=integrations,
                                           wisdom=wisdom,
                                           resources=searchresources))

    elif st.fftmode == "cuda":
        for dmind in range(len(st.dmarr)):
            saved.append(cl.submit(search.dedisperse_image_cuda, st, segment,
                                   data_prep, dmind,
                                   resources={'GPU': 1,
                                              'CORES': st.prefs.nthread},
                                   priority=2))

    # TODO: put these on dedicated worker to ensure quick processing?
    canddatalist = cl.submit(mergelists, saved,
                             resources={'CORES': 1},
                             priority=3)
    candcollection = cl.submit(candidates.calc_features, canddatalist,
                               resources={'CORES': 1}, priority=4)
    futures['candcollection'] = candcollection

    return futures


def pipeline_scan_delayed(st, segments=None, cl=None, host=None, cfile=None,
                          vys_timeout=vys_timeout_default):
    """ Submit pipeline processing of a single segment to scheduler.
    Uses delayed function and client.compute to schedule.

    Returns a list of dicts with futures of data, collection jobs.
    """

    if cl is None:
        if host is not None:
            cl = distributed.Client('{0}:{1}'.format(host, '8786'))

    if not isinstance(segments, list):
        segments = list(range(st.nsegment))

    futures = []
    for segment in segments:
        future = {}
        resources = {}

        logger.info('Building dask for observation {0}, scan {1}, segment {2}.'
                    .format(st.metadata.datasetId, st.metadata.scan, segment))

        data = delayed(source.read_segment)(st, segment, cfile, vys_timeout)
        resources[tuple(data.__dask_keys__())] = {'READER': 1}
        if cl is not None:
            future['data'] = cl.compute(data, resources=resources)

        assert st.fftmode == "cuda", "only cuda fftmode supported"
#        data_prep = delayed(source.data_prep)(st, segment, data)
#        canddatalist = delayed(search.dedisperse_image_cuda)(st, segment,
#                                                             data_prep)
#        candcollection = delayed(candidates.calc_features)(canddatalist)
        candcollection = delayed(prep_and_search)(st, segment, data)

        resources[tuple(candcollection.__dask_keys__())] = {'GPU': 1,
                                                            'CORES': st.prefs.nthread}
        if cl is not None:
            future['candcollection'] = cl.compute(candcollection,
                                                  resources=resources)
            futures.append(future)
        else:
            futures.append(candcollection)

    return futures


def prep_and_search(st, segment, data):
    """ Bundles prep and search functions to improve performance in distributed.
    """

    data_prep = source.data_prep(st, segment, data)
    canddatalist = search.dedisperse_image_cuda(st, segment, data_prep)
    candcollection = candidates.calc_features(canddatalist)

    return candcollection


def mergelists(futlists):
    """ Take list of lists and return single list
    ** TODO: could put logic here to find islands, peaks, etc?
    """

    return [fut for futlist in futlists for fut in futlist]


def lazy_read_segment(st, segment, cfile=None,
                      timeout=vys_timeout_default):
    """ rfpipe read_segment as a dask array.
    equivalent to making delayed version of function and then:
    arr = dask.array.from_delayed(dd, st.datashape, np.complex64).
    """

    shape = st.datashape
    chunks = ((shape[0],), (shape[1],), (shape[2],), (shape[3],))

    name = 'read_segment-' + tokenize([st, segment])
    dask = {(name, 0, 0, 0, 0): (source.read_segment, st, segment,
                                 cfile, timeout)}

    return array.Array(dask=dask, name=name, chunks=chunks, dtype=np.complex64)

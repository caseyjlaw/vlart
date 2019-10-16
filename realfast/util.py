from __future__ import print_function, division, absolute_import #, unicode_literals # not casa compatible
from builtins import bytes, dict, object, range, map, input#, str # not casa compatible
from future.utils import itervalues, viewitems, iteritems, listvalues, listitems
from io import open

from math import floor
import os
import shutil
import subprocess
import numpy as np
from astropy import time, coordinates, units
from time import sleep
from realfast import elastic, mcaf_servers
import distributed
from elasticsearch import NotFoundError

import logging
logger = logging.getLogger(__name__)
logger.setLevel(20)

_candplot_dir = 'claw@nmpost-master:/lustre/aoc/projects/fasttransients/realfast/plots'
_candplot_url_prefix = 'http://realfast.nrao.edu/plots'


def indexcands_and_plots(cc, scanId, tags, indexprefix, workdir):
    """ Wraps indexcands, makesummaryplot, and moveplots calls.
    """

    from rfpipe import candidates

    if len(cc):
        nc = elastic.indexcands(cc, scanId, tags=tags,
                                url_prefix=_candplot_url_prefix,
                                indexprefix=indexprefix)

        # TODO: makesumaryplot logs cands in all segments
        # this is confusing when only one segment being handled here
#        msp = makesummaryplot(workdir, scanId)
        candsfile = cc.state.candsfile
        msp = candidates.makesummaryplot(candsfile=candsfile)
        workdir = cc.prefs.workdir + '/'
        moveplots(workdir, scanId, destination='{0}/{1}'.format(_candplot_dir,
                                                                indexprefix))
    else:
        nc = 0
        msp = 0
        if len(cc.array) > 0:
            logger.warn('CandCollection in segment {0} is empty. '
                        '{1} realtime detections exceeded maximum'
                        .format(cc.segment, cc.array.ncands))

    if nc or msp:
        logger.info('Indexed {0} new cands to {1}, moved plots, and '
                    'summarized {2} total cands to {3} for scanId {4}'
                    .format(nc, indexprefix+'cands', msp, _candplot_dir,
                            scanId))
    else:
        logger.info('No candidates or plots found.')

    return cc


def moveplots(workdir, scanId, destination=_candplot_dir):
    """ For given fileroot, move candidate plots to public location
    """

    logger.info("Moving scanId {0} plots from {1} to {2}"
                .format(scanId, workdir, destination))

#    nplots = 0
#    candplots = glob.glob('{0}/cands_{1}_seg{2}-*.png'
#                          .format(workdir, scanId, segment))
#    for candplot in candplots:
#        success = rsync(candplot, destination)
#        if not success:  # make robust to a failure
#           success = rsync(candplot, destination)
#
#        nplots += success

    # move summary plot too
#    summaryplot = '{0}/cands_{1}.html'.format(workdir, scanId)
#    summaryplotdest = os.path.join(destination, os.path.basename(summaryplot))
#    if os.path.exists(summaryplot):
#        success = rsync(summaryplot, summaryplotdest)
#        if not success:
#            success = rsync(summaryplot, summaryplotdest)
#    else:
#        logger.warn("No summary plot {0} found".format(summaryplot))

    args = ["rsync", "-av", "--include", "cands_{0}*png".format(scanId),
            "--include", "cands_{0}*.html".format(scanId), "--exclude", "*",
            workdir, destination]

    subprocess.call(args)


def indexcandsfile(candsfile, indexprefix, tags=None):
    """ Use candsfile to index cands, scans, prefs, mocks, and noises.
    Should produce identical index results as real-time operation.
    """

    from rfpipe import candidates

    for cc in candidates.iter_cands(candsfile):
        st = cc.state
        scanId = st.metadata.scanId
        workdir = st.prefs.workdir
        mocks = st.prefs.simulated_transient

        elastic.indexscan(inmeta=st.metadata, preferences=st.prefs,
                          indexprefix=indexprefix)
        indexcands_and_plots(cc, scanId, tags, indexprefix, workdir)

        if os.path.exists(cc.state.noisefile):
            elastic.indexnoises(scanId, noisefile=cc.state.noisefile,
                                indexprefix=indexprefix)

        if mocks is not None:
            elastic.indexmock(scanId, mocks, indexprefix=indexprefix)


def calc_and_indexnoises(st, segment, data, indexprefix='new'):
    """ Wraps calculation and indexing functions.
    Should get calibrated data as input.
    Checks that scanId is indexed before indexing noise.
    """

    from rfpipe.util import calc_noise

    noises = calc_noise(st, segment, data)
    scindex = indexprefix+'scans'
    try:
        doc = elastic.get_doc(scindex, st.metadata.scanId)
        elastic.indexnoises(st.metadata.scanId, noises=noises,
                            indexprefix=indexprefix)
    except NotFoundError:
        logger.warn("scanId {0} not found in {1}. Not indexing noise estimate."
                    .format(st.metadata.scanId, scindex))


def createproducts(candcollection, data, indexprefix=None,
                   savebdfdir='/lustre/evla/wcbe/data/realfast/'):
    """ Create SDMs and BDFs for a given candcollection (time segment).
    Takes data future and calls data only if windows found to cut.
    This uses the mcaf_servers module, which calls the sdm builder server.
    Currently BDFs are moved to no_archive lustre area by default.
    """

    if isinstance(candcollection, distributed.Future):
        candcollection = candcollection.result()

    if len(candcollection) == 0:
        logger.info('No candidates to generate products for.')
        return []

    if isinstance(data, distributed.Future):
        data = data.result()

    assert isinstance(data, np.ndarray) and data.dtype == 'complex64'

    logger.info("Creating an SDM for {0}, segment {1}, with {2} candidates"
                .format(candcollection.metadata.scanId, candcollection.segment,
                        len(candcollection)))

    wait = candcollection.metadata.endtime_mjd + 10/(24*3600)  # end+10s
    now = time.Time.now().mjd
    if now < wait:
        logger.info("Waiting until {0} for ScanId {1} to complete"
                    .format(wait, candcollection.metadata.scanId))

        while now < wait:
            sleep(1)
            now = time.Time.now().mjd

    metadata = candcollection.metadata
    segment = candcollection.segment
    if not isinstance(segment, int):
        logger.warning("Cannot get unique segment from candcollection")

    st = candcollection.state

    candranges = gencandranges(candcollection)  # finds time windows to save
    calScanTime = candcollection.soltime  # solution saved during search
    logger.info('Getting data for candidate time ranges {0} in segment {1}.'
                .format(candranges, segment))

    ninttot, nbl, nchantot, npol = data.shape
    nchan = metadata.nchan_orig//metadata.nspw_orig
    nspw = metadata.nspw_orig

    sdmlocs = []
    # make sdm for each unique time range (e.g., segment)
    for (startTime, endTime) in set(candranges):
        i = (86400*(startTime-st.segmenttimes[segment][0])/metadata.inttime).astype(int)
        nint = floor(86400*(endTime-startTime)/metadata.inttime)
        logger.info("Cutting {0} ints from int {1} for candidate at {2} in segment {3}"
                    .format(nint, i, startTime, segment))
        data_cut = data[i:i+nint].reshape(nint, nbl, nspw, 1, nchan, npol)

        annotation = cc_to_annotation(candcollection, mode='dict')

# now retrieved from candcollection
#        calScanTime = np.unique(calibration.getsols(st)['mjd'])
#        if len(calScanTime) > 1:
#            logger.warn("Using first of multiple cal times: {0}."
#                        .format(calScanTime))
#        calScanTime = calScanTime[0]

        sdmloc = mcaf_servers.makesdm(startTime, endTime, metadata.datasetId,
                                      data_cut, calScanTime,
                                      annotation=annotation)
        if sdmloc is not None:
            sdmpath = os.path.dirname(sdmloc)
            sdmloc = os.path.basename(sdmloc)  # ignore internal mcaf path from here on
            logger.info("Created new SDM in {0} called {1}".format(sdmpath, sdmloc))
            sdmlocs.append(sdmloc)

            # update index to link to new sdm
            if indexprefix is not None:
                candIds = elastic.candid(cc=candcollection)
                for Id in candIds:
                    try:
                        logger.info("Updating index {0}, Id {1}, with sdmname {2}".format(indexprefix+'cands', Id, sdmloc))
                        elastic.update_field(indexprefix+'cands', 'sdmname',
                                             sdmloc, Id=Id)
                    except NotFoundError as exc:
                        logger.warn("elasticsearch cannot find Id {0} in index {1}. Exception: {2}".format(Id, indexprefix+'cands', exc))
                        
            # TODO: migrate bdfdir to newsdmloc once ingest tool is ready
            logger.info("Making bdf for time {0}-{1}".format(startTime, endTime))
            mcaf_servers.makebdf(startTime, endTime, metadata, data_cut,
                                 bdfdir=savebdfdir)
        else:
            logger.warn("No sdm/bdf made for {0} with start/end time {1}-{2}"
                        .format(metadata.datasetId, startTime, endTime))

    return sdmlocs


def cc_to_annotation(cc, mode='dict'):
    """ Takes candcollection and returns dict to be passed to sdmbuilder.
    Dict has standard fields to fill annotations table for archiving queries.
    mode can be 'dict' to return single dict at max snrtot or 'list' to return list of dicts.
    """

    from rfpipe import candidates

    assert mode in ['dict', 'list']

    # fixed in cc
    uvres = cc.state.uvres
    npix = min(cc.state.npixx, cc.state.npixy)  # estimate worst loc
    pixel_sec = np.degrees(1/(uvres*npix))*3600
    dmarr = cc.state.dmarr
    ra_ctr, dec_ctr = cc.metadata.radec
    scanid = cc.metadata.scanId
    maxsnr = cc.snrtot.max()

    if mode == 'list':
        annotation = []
    elif mode == 'dict':
        maxsnr = cc.snrtot.max()

    for i in range(len(cc)):
        snr = cc.snrtot[i]
        if snr != maxsnr and mode == 'dict':
            continue
        l1 = cc.candl[i]
        m1 = cc.candm[i]
        ra, dec = candidates.source_location(ra_ctr, dec_ctr, l1, m1)
        segment, integration, dmind, dtind, beamnum = cc.locs[i]
        candid = '{0}_seg{1}-i{2}-dm{3}-dt{4}'.format(scanid, segment, integration, dmind, dtind)
        dd = {'primary_filesetId': cc.metadata.datasetId,
              'cand_Id': candid,
#              'transient_mjd': None,  # TODO
              'transient_RA': ra,
              'transient_RA_error': float(pixel_sec),
              'transient_Dec': dec,
              'transient_Dec_error': float(pixel_sec),
              'transient_SNR': float(snr),
              'transient_DM': float(cc.canddm[i]),
              'transient_DM_error': float(dmarr[1]-dmarr[0]),
              'preaverage_time': float(cc.canddt[i]),
              'rfpipe_version': cc.prefs.rfpipe_version,
              'prefs_Id': cc.prefs.name}
# TODO: get noises and classifications in
#                  'rf_QA_label': None,  
#                  'rf_QA_zero_fraction': None,
#                  'rf_QA_visibility_noise': None,
#                  'rf_QA_image_noise': None}

        if mode == 'list':
            annotation.append(dd)
        elif mode == 'dict':
            annotation = dd

    return annotation


def classify_candidates(cc, indexprefix='new', devicenum=None):
    """ Submit canddata object to node with fetch model ready
    """

    from rfpipe import candidates

    index = indexprefix + 'cands'

    try:
        if len(cc.canddata):
            logger.info("Running fetch classifier on {0} candidates for scanId {1}, "
                        "segment {2}"
                        .format(len(cc.canddata), cc.metadata.scanId, cc.segment))

            for cd in cc.canddata:
                frbprob = candidates.cd_to_fetch(cd, classify=True, devicenum=devicenum)
                elastic.update_field(index, 'frbprob', frbprob, Id=cd.candid)
        else:
            logger.info("No canddata available for scanId {0}, segment {1}."
                        .format(cc.metadata.scanId, cc.segment))
    except AttributeError:
        logger.info("CandCollection has no canddata attached. Not classifying.")


def get_sdmname(candcollection):
    """ Use candcollection to get name of SDM output by the sdmbuilder
    """

    datasetId = candcollection.metadata.datasetId
    uid = get_uid(candcollection)
    outputDatasetId = 'realfast_{0}_{1}'.format(datasetId, uid.rsplit('/')[-1])

    return outputDatasetId


def get_uid(candcollection):
    """ Get unix time appropriate for bdf naming
    Expects one segment per candcollection and one range per segment.
    """

    candranges = gencandranges(candcollection)
    assert len(candranges) == 1, "Assuming 1 candrange per candcollection and/or segment"

    startTime, endTime = candranges[0]

    uid = ('uid:///evla/realfastbdf/{0}'
           .format(int(time.Time(startTime, format='mjd').unix*1e3)))

    return uid


def assemble_sdmbdf(candcollection, sdmloc='/home/mctest/evla/mcaf/workspace/',
                    bdfdir='/lustre/evla/wcbe/data/realfast/'):
    """ Use candcollection to assemble cutout SDM and BDF from file system at VLA
    into a viable SDM.
    """

    destination = '.'
    sdm0 = get_sdmname(candcollection)
    bdf0 = get_uid(candcollection).replace(':', '_').replace('/', '_')
    newsdm = os.path.join(destination, sdm0)
    shutil.copytree(os.path.join(sdmloc, sdm0), newsdm)

    bdfdestination = os.path.join(newsdm, 'ASDMBinary')
    os.mkdir(bdfdestination)
    shutil.copy(os.path.join(bdfdir, bdf0), os.path.join(bdfdestination, bdf0))

    logger.info("Assembled SDM/BDF at {0}".format(newsdm))

    return newsdm


def update_slack(channel, message):
    """ Use slack web API to send message to realfastvla slack
    channel should start with '#' and be existing channel.
    API token set for realfast user on cluster.
    """

    import os
    import slack

    client = slack.WebClient(token=os.environ['SLACK_API_TOKEN'])

    response = client.chat_postMessage(
        channel=channel,
        text=message, icon_emoji=":robot_face:")
    assert response["ok"]
    if response["message"]["text"] != message:
        logger.warn("Response from Slack API differs from message sent. "
                    "Maybe just broken into multiple updates: {0}".format(response))


def data_logger(st, segment, data):
    """ Function that inspects read data and writes results to file.
    """

    from rfpipe import fileLock

    filename = os.path.join(st.prefs.workdir,
                            "data_" + st.fileroot + ".txt")

    t0 = st.segmenttimes[segment][0]
    timearr = ','.join((t0+st.metadata.inttime*np.arange(st.readints))
                       .astype(str))

    if data.ndim == 4:
        results = ','.join(data.mean(axis=3).mean(axis=2).any(axis=1)
                           .astype(str))
    else:
        results = 'None'

    try:
        with fileLock.FileLock(filename, timeout=60):
            with open(filename, "a") as fp:
                fp.write("{0}: {1} {2} {3}\n".format(segment, t0, timearr,
                                                     results))
    except fileLock.FileLock.FileLockException:
        logger.warn("data_logger on segment {0} failed to write due to file timeout"
                    .format(segment))


def data_logging_report(filename):
    """ Read and summarize data logging file
    """

    with open(filename, 'r') as fp:
        for line in fp.readlines():
            segment, starttime, dts, filled = line.split(' ')
            segment = int(segment.rstrip(':'))
            starttime = float(starttime)
            dts = [float(dt) for dt in dts.split(',')]
            filled = [fill.rstrip() == "True" for fill in filled.split(',')]
            if len(filled) > 1:
                filled = np.array(filled, dtype=int)
                logger.info("Segment {0}: {1}/{2} missing"
                            .format(segment, len(filled)-filled.sum(),
                                    len(filled)))
                logger.info("{0}".format(filled))
            else:
                logger.info("Segment {0}: no data".format(segment))


def gencandranges(candcollection):
    """ Given a candcollection, define a list of candidate time ranges.
    Currently saves the whole segment.
    """

    segment = candcollection.segment
    st = candcollection.state

    # save whole segment
    return [(st.segmenttimes[segment][0], st.segmenttimes[segment][1])]


def initialize_worker():
    """ Function called to initialize python on workers
    """

    from rfpipe import search, state, metadata, candidates, reproduce
    t0 = time.Time.now().mjd
    search.set_wisdom(512)
    st = state.State(inmeta=metadata.mock_metadata(t0, t0+0.001, 27, 16, 16*32, 4, 5e4, datasource='sim'))
    return st


def rsync(original, new):
    """ Uses subprocess.call to rsync from 'filename' to 'new'
    If new is directory, copies original in.
    If new is new file, copies original to that name.
    """

    assert os.path.exists(original), 'Need original file!'
    res = subprocess.call(["rsync", "--timeout", "30", "-a", original.rstrip('/'), new.rstrip('/')])

    return int(res == 0)

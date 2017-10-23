from __future__ import print_function, division, absolute_import  #, unicode_literals # not casa compatible
from builtins import bytes, dict, object, range, map, input  #, str # not casa compatible
from future.utils import itervalues, viewitems, iteritems, listvalues, listitems
from io import open

import pickle
import os.path
import glob
import shutil
import random
from astropy import time
from evla_mcast.controller import Controller
from rfpipe import state, preferences
from sdmpy import bdf
from realfast import pipeline
from realfast import elastic, sdm_builder

import logging
ch = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s %(levelname)8s %(name)s | %(message)s')
ch.setFormatter(formatter)
logger = logging.getLogger('realfast_controller')

vys_cfile = '/home/cbe-master/realfast/soft/vysmaw_apps/vys.conf'
default_preffile = '/lustre/evla/test/realfast/realfast.yml'
default_vys_timeout = 10  # seconds more than segment length
distributed_host = 'cbe-node-01'

mock_standards = [(0.1, 30, 20, 0.05, 1e-3, 1e-3),
                  (0.1, 30, 20, 0.05, -1e-3, 1e-3),
                  (0.1, 30, 20, 0.05, -1e-3, -1e-3),
                  (0.1, 30, 20, 0.05, 1e-3, -1e-3)]  # (amp, i0, dm, dt, l, m)


class realfast_controller(Controller):

    def __init__(self, preffile=default_preffile, inprefs={},
                 vys_timeout=default_vys_timeout, datasource=None,
                 tags=None, mockprob=0.5):
        """ Creates controller object that can act on a scan configuration.
        Inherits a "run" method that starts asynchronous operation.
        datasource of None defaults to "vys" or "sdm", by sim" is an option.
        tags is a default string for candidates put into index (None -> "new").
        mockprob is a prob (range 0-1) that a mock is added to each segment.
        """

        super(realfast_controller, self).__init__()
        self.preffile = preffile
        self.inprefs = inprefs
        self.vys_timeout = vys_timeout
        self.jobs = {}
        self.datasource = datasource
        self.tags = tags
        self.mockprob = mockprob
        self.client = None

    def __repr__(self):
        return ('realfast controller for preffile {0} with {1} jobs'
                .format(self.datasource, len(self.jobs)))

    def handle_config(self, config):
        """ Triggered when obs comes in.
        Downstream logic starts here.
        """

        summarize(config)

        if self.datasource is None:
            self.datasource = 'vys'

        self.inject_transient(config.scanId)  # randomly inject mock transient

        if runsearch(config):
            logger.info('Config looks good. Generating rfpipe state...')
            st = state.State(config=config, preffile=self.preffile,
                             inprefs=self.inprefs,
                             inmeta={'datasource': self.datasource})
            elastic.indexscan_config(config, preferences=st.prefs,
                                     datasource=self.datasource)  # index prefs

            logger.info('Starting pipeline...')
            # pipeline returns dict of futures
            # TODO: update for dict structure
            jobs = pipeline.pipeline_scan(st, segments=None,
                                          host=distributed_host,
                                          cfile=vys_cfile,
                                          vys_timeout=self.vys_timeout)
            self.jobs[config.scanId] = jobs
            self.client = self.jobs[0].client  # TODO test this
        else:
            logger.info("Config not suitable for realfast. Skipping.")

        # end of job clean up (indexing and removing from job list)
        self.cleanup()
        # TODO: this only runs when new data arrives. how to run at end/ctrl-c?

    def handle_sdm(self, sdmfile, sdmscan):
        """ Parallel to handle_config, but allows sdm to be passed in.
        Gets called explicitly. No cleanup done.
        """

        if self.datasource is None:
            self.datasource = 'sdm'

        # TODO: subscan assumed = 1
        subscan = 1
        scanId = '.'.join([os.path.basename(sdmfile), str(sdmscan),
                           str(subscan)])
        self.inject_transient(scanId)  # randomly inject mock transient

        st = state.State(sdmfile=sdmfile, sdmscan=sdmscan,
                         preffile=self.preffile, inprefs=self.inprefs,
                         inmeta={'datasource': self.datasource})

        elastic.indexscan_sdm(scanId, preferences=st.prefs,
                              datasource=self.datasource)  # index prefs

        logger.info('Starting pipeline...')
        # pipeline returns state object per DM/dt
        jobs = pipeline.pipeline_scan_distributed(st, segments=None,
                                                  host=distributed_host,
                                                  cfile=vys_cfile,
                                                  vys_timeout=self.vys_timeout)

        self.jobs[scanId] = jobs
        self.client = jobs[0].client  # TODO: test this

    def handle_finish(self, dataset):
        """ Triggered when obs doc defines end of a script.
        """

        logger.info('End of scheduling block message received.')

    def cleanup(self):
        """ Scan job dict, remove finished jobs,
        and push results to relevant indices.
        """

        removed = 0
        cindexed = 0
        sdms = 0

        for scanId in self.jobs:
            logger.info("Checking on jobs from scanId {0}".format(scanId))
            removelist = [job for job in self.jobs[scanId]
                          if job.status in ['finished', 'cancelled']]

            # one canddf per segment
            for job in removelist:
                candcollection, data = job.result()  # TODO: test for performance

                if len(candcollection.array):
#                    res = self.client.submit(elastic.indexcands, job, scanId, tags=self.tags)
                    res = elastic.indexcands(candcollection.array, scanId,
                                             prefsname=candcollection.prefs.name,
                                             tags=self.tags)
                    cindexed += res
                else:
                    logger.info('No candidates for scanId {0}, scan {1} '
                                'and segment {2}.'.format(scanId,
                                                          candcollection.scan,
                                                          candcollection.segment))

# TODO: index noises
#                if os.path.exists(st.noisefile):
#                    res = elastic.indexnoises(st.noisefile, scanId)
#                    nindexed += res
#                else:
#                    logger.info('No noisefile found, no noises indexed.')


                # remove job from list
                self.jobs[scanId].remove(job)
                removed += 1

                # for last job of scanId trigger further cleanup
                if len(self.jobs[scanId]) == 0:
                    _ = self.jobs.pop(scanId)
                    moveplots(candcollection.prefs.workdir, scanId)
#                    res = self.client.submit(createproducts, ...)  # make SDMs

        if removed:
            logger.info('Removed {0} jobs, indexed {1} cands, made {2} SDMs.'
                        .format(removed, cindexed, sdms))

    def inject_transient(self, scanId):
        """ Randomly sets preferences for scan to injects a transient
        into each segment.
        Also pushes mock properties to index.
        """

        if random.uniform(0, 1) < self.mockprob:
            mockparams = random.choice(mock_standards)
            self.inprefs['simulated_transient'] = [mockparams]
            mindexed = elastic.indexmocks(self.inprefs, scanId)
            logger.info("Indexed {0} mock transients.".format(mindexed))

            if self.tags is None:
                self.tags = 'mock'
            elif 'mock' not in self.tags:
                self.tags = ','.join(self.tags.split(',') + ['mock'])
        elif self.tags is not None:
            if 'mock' in self.tags:
                self.tags = ','.join(self.tags.split(',').remove('mock'))

    @property
    def statuses(self):
        return [self.jobs[scanId][i].status for scanId in self.jobs
                for i in range(len(self.jobs[scanId]))]

    @property
    def errors(self):
        return [self.jobs[scanId][i].exception() for scanId in self.jobs
                for i in range(len(self.jobs[scanId]))
                if self.jobs[scanId][i].status == 'error']


def runsearch(config):
    """ Test whether configuration specifies a config that realfast should search
    """

    # find config properties of interest
    intent = config.scan_intent
    antennas = config.get_antennas()
    antnames = [str(ant.name) for ant in antennas]
    subbands = config.get_subbands()
    inttimes = [subband.hw_time_res for subband in subbands]
    pols = [subband.pp for subband in subbands]
    nchans = [subband.spectralChannels for subband in subbands]
    chansizes = [subband.bw/subband.spectralChannels for subband in subbands]
    reffreqs = [subband.sky_center_freq*1e6 for subband in subbands]

    # Do not process if...
    # 1) chansize changes between subbands
    if not all([chansizes[0] == chansize for chansize in chansizes]):
        logger.warn("Channel size changes between subbands: {0}"
                    .format(chansizes))
        return False

    # 2) start and stop time is after current time
    now = time.Time.now().unix
    startTime = time.Time(config.startTime, format='mjd').unix
    stopTime = time.Time(config.stopTime, format='mjd').unix
    if (startTime > now) and (stopTime > now):
        logger.warn("Scan startTime and stopTime are in the past ({0}, {1} < {2})"
                    .format(startTime, stopTime, now))
        return False

    return True

def summarize(self, config):
    """ Print summary info for config
    """

    try:
        logger.info(':: ConfigID {0} ::'.format(config.configId))
        logger.info('\tScan {0}, source {1}, intent {2}'
                    .format(config.scanNo, config.source,
                            config.scan_intent))

        logger.info('\t(RA, Dec) = ({0}, {1})'
                    .format(config.ra_deg, config.dec_deg))
        subbands = config.get_subbands()
        reffreqs = [subband.sky_center_freq for subband in subbands]
        logger.info('\tFreq: {0} - {1}'
                    .format(min(reffreqs), max(reffreqs)))

        nchans = [subband.spectralChannels for subband in subbands]
        chansizes = [subband.bw/subband.spectralChannels
                     for subband in subbands]
        sb0 = subbands[0]
        logger.info('\t(nspw, chan/spw, nchan) = ({0}, {1}, {2})'
                    .format(len(nchans), nchans[0], sum(nchans)))
        logger.info('\t(BW, chansize) = ({0}, {1}) MHz'
                    .format(sb0.bw, chansizes[0]))
        if not all([chansizes[0] == chansize for chansize in chansizes]):
            logger.info('\tNot all spw have same configuration.')

        logger.info('\t(nant, npol) = ({0}, {1})'
                    .format(config.numAntenna, sb0.npp))
        dt = 24*3600*(config.stopTime-config.startTime)
        logger.info('\t(StartMJD, duration) = ({0}, {1}s).'
                    .format(config.startTime, round(dt, 1)))
        logger.info('\t({0}/{1}) ints at (HW/Final) integration time of ({2}/{3}) s'
                    .format(int(round(dt/sb0.hw_time_res)),
                            int(round(dt/sb0.final_time_res)),
                            sb0.hw_time_res, sb0.final_time_res))
    except:
        logger.warn("Failed to fully parse config to print summary."
                    "Proceeding.")


def selectcands(candcollection):
    """ Given a candcollection, define a list of bdf
    """

#    [(datasetId, dataSize, nint, startTime, endTime)}
    NotImplementedError


def createproducts(candcollection, data):
    """ Create SDM and manage sdm and plots.
    """

    sdmlocs = []
    cands = selectcands(candcollection)
    for cand in cands:
        sdmlocs.append(savesdm(cand))
        savebdf(cand, candcollection.metadata, data)


def savesdm(cand):
    """ Generate unique id for bdf and call sdm builder for each cand.
    Returns location of newly created SDM.
    """

    datasetId, dataSize, nint, startTime, endTime = cand
    uid = int(time.Time(startTime, format='mjd').unix*1e3)
    sdmb = sdm_builder.SDMBuilder(datasetId, uid, dataSize,
                                  nint, startTime, endTime)
    sdmb.send()

    return sdmb.location()


def savebdf(cand, metadata, data):
    """ Create bdf for candidate that contains data array.
    Data is numpy array of complex64 type  spanning times in cand.
    Should have shape (nint, nbl, 1, nspw, nchan, npol).
    Assumes one bdf per sdm and one sdm per candidate.
    Only supports 8bit samplers and IFid of AC/BD.
    """

    datasetId, dataSize, nint, startTime, endTime = cand
    nint, nbl, numBin, nspw, nchan, npol = data.shape

    IFidspwnum = [spw.split('-') for (spw, freq) in metadata.spworder]
    spws = [bdf.BDFSpectralWindow(None, numBin=numBin, numSpectralPoint=nchan,
                                  sw=int(swnum)+1,
                                  swbb='{0}_8BIT'.format(IFid),
                                  npol=npol) for (IFid, swnum) in IFidspwnum]
    # TODO: confirm that sw is 1 based for a proper SDM
    # TODO: confirm that metadata spworder is 0 based

    assert nspw == len(spws), ('Expected one spw in metadata.spworder per spw '
                               'in data array.')

    uid = int(time.Time(startTime, format='mjd').unix*1e3)
    w = bdf.BDFWriter('{0}.bdf'.format(uid), start_mjd=startTime,
                      uid=uid, num_antenna=metadata.nants_orig,
                      spws=spws, scan_idx=1, corr_mode='c')

    dat = {}
    w.write_header()
    for i in range(nint):
        dat['crossData'] = data[i]
        ts = startTime+metadata.inttime/2/86400.
        w.write_integration(mjd=ts, interval=metadata.inttime, data=dat)
    w.close()


def moveplots(workdir, scanId,
              destination='/users/claw/public_html/realfast/plots'):
    """ For given fileroot, move candidate plots to public location
    """

    datasetId, scan, subscan = scanId.rsplit('.', 2)

    candfiles = glob.glob('{0}/cands_{1}*.png'.format(workdir, datasetId))
    for candfile in candfiles:
        shutil.copy(candfile, destination)
    if candfiles:
        logger.info('Candidate plots copied to {0}'.format(destination))
    else:
        logger.warn('No candidate plots found to copy.')


class config_controller(Controller):

    def __init__(self, pklfile=None, preffile=None):
        """ Creates controller object that saves scan configs.
        If pklfile is defined, it will save pickle there.
        If preffile is defined, it will attach a preferences to indexed scan.
        Inherits a "run" method that starts asynchronous operation.
        """

        super(config_controller, self).__init__()
        self.pklfile = pklfile
        self.preffile = preffile

    def handle_config(self, config):
        """ Triggered when obs comes in.
        Downstream logic starts here.
        """

        logger.info('Received complete configuration for {0}, '
                    'scan {1}, source {2}, intent {3}'
                    .format(config.scanId, config.scanNo, config.source,
                            config.scan_intent))

        if self.pklfile:
            with open(self.pklfile, 'ab') as pkl:
                pickle.dump(config, pkl)

        if self.preffile:
            prefs = preferences.Preferences(**preferences.parsepreffile(self.preffile))
            elastic.indexscan_config(config, preferences=prefs)

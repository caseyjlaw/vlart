import rfpipe
import pytest
import evla_mcast
import os.path
import os

_install_dir = os.path.abspath(os.path.dirname(__file__))


@pytest.fixture(scope="module")
def config():
    config = evla_mcast.scan_config.ScanConfig(vci=os.path.join(_install_dir, 'data/vci.xml'),
                                               obs=os.path.join(_install_dir, 'data/obs.xml'),
                                               ant=os.path.join(_install_dir, 'data/antprop.xml'),
                                               requires=['ant', 'vci', 'obs'])
    config.stopTime = config.startTime+100/(24*3600.)

    return config


@pytest.fixture(scope="module")
def config2():
    config = evla_mcast.scan_config.ScanConfig(vci=os.path.join(_install_dir, 'data/vci2.xml'),
                                               obs=os.path.join(_install_dir, 'data/obs.xml'),
                                               ant=os.path.join(_install_dir, 'data/antprop.xml'),
                                               requires=['ant', 'vci', 'obs'])
    config.stopTime = config.startTime+100/(24*3600.)

    return config


@pytest.fixture(scope="module", params=[{'npix_max': 128},
                                        {'memory_limit': 1., 'maxdm': 100},
                                        {'maxdm': 100}])
def inprefs(request):
    return request.param


def test_configstate(config, inprefs):
    st = rfpipe.state.State(config=config, inprefs=inprefs, preffile=None)

    assert st.nints
    assert st.metadata.nints
    assert st.metadata.endtime_mjd
    assert len(st.segmenttimes)


def test_configstate2(config2, inprefs):
    st = rfpipe.state.State(config=config2, inprefs=inprefs, preffile=None)

    assert st.nints
    assert st.metadata.nints
    assert st.metadata.endtime_mjd
    assert len(st.segmenttimes)


def test_metastate(config, inprefs):
    meta = rfpipe.metadata.config_metadata(config, datasource='sim')

    st = rfpipe.state.State(inmeta=meta, inprefs=inprefs, preffile=None)

    assert st.nints
    assert st.metadata.nints
    assert st.metadata.endtime_mjd
    assert len(st.segmenttimes)


def test_sim(config, inprefs):
    meta = rfpipe.metadata.config_metadata(config, datasource='sim')

    st = rfpipe.state.State(inmeta=meta, inprefs=inprefs,
                            preffile=os.path.join(_install_dir,
                                                  'data/realfast.yml'))

    segment = 0
    data = rfpipe.source.read_segment(st, segment)
    assert data.shape == st.datashape_orig

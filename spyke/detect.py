"""Spike detection algorithms

TODO: use median based noise estimation instead of std based
      - estimate noise level dynamically with sliding window
        and independently for each channel
TODO: spatiotemporal lockout:
      - do spatial lock out only during first 1/2 phase of trigger spike
      - phases are part of same spike if less than 250us between each other

TODO: for speed, consider converting all uV data from 64bit float to 16 bit integer

DONE: Might need to use scipy.weave or some other low-level code
     (cython?) to make these algorithms fast enough, while allowing you to
     step through one timepoint at a time, which numpy might not let you do
     easily...
DONE: add a method to search forward until you find the next spike on any or on a
      specific set of channels, as opposed to searching forward over a known fixed
      trange. This would make testing easier too
"""

from __future__ import division

__authors__ = ['Reza Lotun, Martin Spacek']

import itertools
import sys

import numpy as np
from numpy import where
from scipy import weave

import spyke.surf
from spyke.core import WaveForm, toiter
#from spyke import Spike, Template, Collection


MAXFIRINGRATE = 1000 # Hz, assume no chan will continuously trigger more than this rate of events
CHUNKSIZE = 1000000 # waveform data chunk size, us
MAXNSPIKETIS = CHUNKSIZE/1000000 * MAXFIRINGRATE # length of array to preallocate before searching a chunk's channel


class Detector(object):
    """Spike detector base class"""
    DEFAULTTHRESHMETHOD = 'median'
    DEFTLOCK = 250 # us
    DEFSLOCK = 175 # um

    def __init__(self, stream, chans=None, trange=None, maxnspikes=None,
                 tlock=None, slock=None):
        """Takes a data stream and sets various parameters"""
        self.stream = stream
        if chans == None:
            chans = range(self.stream.nchans) # search all channels
        self.chans = toiter(chans)
        if trange == None:
            trange = (stream.t0, stream.tend)
        self.trange = trange
        if maxnspikes == None:
            maxnspikes = sys.maxint
        self.maxnspikes = maxnspikes # return at most this many spikes, applies across chans
        if tlock == None:
            tlock = self.DEFTLOCK
        self.tlock = tlock
        if slock == None:
            slock = self.DEFSLOCK
        self.slock = slock

    '''
    def __iter__(self):
        """Returns an iterator object. Called in for loops and in 'in' statements, and by the iter() f'n?.
        This is here to allow you to treat any of the detection classes directly as iterators"""
        spikes = iter(self.find())
        # why not just return the iterator, and let the outside world iterate over it, like this:?
        return spikes
        #while True:
        #    try:
        #        yield spikes.next()
        #    except StopIteration:
        #        break
    '''
    def get_threshold(self, chan, kind=DEFAULTTHRESHMETHOD):
        """Calculate either median or stdev based threshold for a given chan"""
        if kind == 'median':
            self.get_median_threshold(chan)
        elif  kind == 'stdev':
            self.get_stdev_threshold(chan)

    def get_median_threshold(self, chan):
        return self.get_median_noise(chan) * self.MEDIAN_MULT

    def get_stdev_threshold(self, chan):
        return self.get_stdev_noise(chan) * self.STDEV_MULT

    def get_median_noise(self, chan):
        """Overriden by FixedThresh and DynamicThresh classes"""
        pass

    def get_stdev_noise(self, chan):
        """Overriden by FixedThresh and DynamicThresh classes"""
        pass


class FixedThresh(Detector):
    """Base class for fixed threshold spike detection,
    Uses the same single static threshold throughout the entire file,
    with an independent threshold for every channel"""

    STDEV_WINDOW = 10000000 # 10 sec
    STDEV_MULT = 4
    SPIKE_PRE = 250
    SPIKE_POST = 750
    #SEARCH_SPAN = 1000

    def __init__(self, *args, **kwargs):
        Detector.__init__(self, *args, **kwargs)
        self.thresh = 50 # uV, TODO: calculate this from noise level

    '''
    def setup(self):
        """Used to determine threshold and set initial state"""
        # get stdev for each channel along a STDEV_WINDOW window
        wave = self.stream[self.t0:self.t0 + STDEV_WINDOW]
        self.std = {}
        for chan, d in enumerate(wave.data):
            self.std[chan] = wave.data[chan].std()

        # set the threshold to be STDEV_MULT * standard deviation
        # each chan has a separate thresh
        self.thresholds = {}
        for chan, stdev in self.std.iteritems():
            self.thresholds[chan] = stdev * self.STDEV_MULT

        # spike window: -SPIKE_PRE and +SPIKE_POST around spike, search window will be 1ms
        self.search_span = self.SEARCH_SPAN
        self.curr = self.t0 + self.SEARCH_SPAN # XXX: add an initial jump: TODO: why?
        self.window = self.stream[self.t0:self.t0 + self.search_span]

        self.lockout = self.LOCKOUT

    def yield_events(self, chan_events):
        """TODO: what does this do? need description here"""
        # sort event indices
        chan_events.sort()
        for event_index, chan in chan_events:
            # if the event is firing before our current location
            # then we're in lockout mode and should just continue
            if self.window.ts[event_index] < self.curr:
                continue
            # reposition window for each event
            self.curr = self.window.ts[event_index] - self.SPIKE_PRE
            spike = self.stream[self.curr:self.curr + \
                                self.SPIKE_PRE + self.SPIKE_POST]
            self.curr = self.curr + self.SPIKE_PRE + \
                            self.SPIKE_POST + self.lockout
            #self.window = self.stream[self.curr:self.curr + self.search_span]
            yield Spike(spike, chan, self.window.ts[event_index])
    '''

class DynamicThresh(Detector):
    """Base class for dynamic threshold spike detection,
    Uses varying thresholds throughout the entire file,
    depending on the local noise level

    Calculate noise level using, say, a 50ms sliding window centered on the
    timepoint you're currently testing for a spike. Or, use fixed pos
    windows, pre calc noise for each of them, and take noise level from whichever
    window you happen to be in while checking a timepoint for thresh xing.
    """

    def get_median_noise(self, chan):
        """Overriden by FixedThresh and DynamicThresh classes"""
        pass

    def get_stdev_noise(self, chan):
        """Overriden by FixedThresh and DynamicThresh classes"""
        pass


class BipolarAmplitudeFixedThresh(FixedThresh):
    """Bipolar amplitude fixed threshold detector,
    with fixed temporal lockout on all channels, plus a spatial lockout"""

    def search(self):
        """Searches for spikes. Divides large searches into more managable
        chunks of (slightly overlapping) multichannel waveform data, and
        then combines the results"""

        # holds a channel's spike times, passed by assignment to C code
        # no need for more than one max every other timepoint, can get away with less to save memory if needed
        self.spiketis = np.zeros(MAXNSPIKETIS, dtype=np.int32)

        # TODO: slightly overlapping chunks of data, use CHUNKSIZE
        wavetranges = [(self.trange[0], self.trange[0]+5000000)] # hard coding for testing
        #spikes = {} # dict of arrays of spike times, one entry per chan
        for (tlo, thi) in wavetranges:
            wave = self.stream[tlo:thi] # a chunk of multichan data
            ts = self.searchwave(wave)
            # TODO: compare to previous chunk in overlap area on a chan by chan basis,
            #       make sure not to count spikes twice in overlap
            spikes = ts
        return spikes

    def searchwave(self, wave):
        """Search across all chans in a manageable chunk of waveform
        data and return a dict of arrays of spike times, one entry per chan"""
        spikes = {}
        nspikes = 0
        for chan in self.chans:
            abschan = np.abs(wave[chan])
            tis = self.searchchan(abschan)
            spikes[chan] = wave.ts[tis] # spike times in us
            nspikes += len(tis)
            if nspikes >= self.maxnspikes: # enforce maxnspikes across all chans
                break # this doesn't seem to speed things up for some reason
        # TODO: apply spatial lockout here

        return spikes

    def searchchan(self, abschan):
        """Search a single chan of absval data for thresh xings, apply temporal lockout.
        If this crashes, it might be possible that self.spiketis was init'd too small"""
        nt = len(abschan)
        maxnspikes = self.maxnspikes
        thresh = self.thresh
        #thresh = float(thresh)
        tilock = 6 # TODO: = t2ti(self.tlock)
        assert tilock.__class__ == int
        assert tilock >= 0 # num of timepoint indices to lock out after a spike
        spiketis = self.spiketis # init'd in self.search()

        code = r"""
        #line 230 "detect.py" // (this is only useful for debugging)
        double last=0.0; // last signal value, uV
        int nspikes=0;
        int ti=0; // current time index
        while ( ti<nt && nspikes < maxnspikes ) { // enforce maxnspikes across single chan
            if (abschan(ti) >= thresh) {
                while (abschan(ti) > last) { // signal is still increasing
                    last = abschan(ti);
                    ti++; // go to next timepoint
                }
                // signal is decreasing, save last timepoint as spike
                nspikes++;
                spiketis(nspikes-1) = ti-1;
                last = 0.0; // reset for search for next spike
                ti += tilock; // skip forward one temporal lockout
            }
            else
                ti++; // no thresh xing, go to next timepoint
        }
        return_val = nspikes;
        """
        nspikes = weave.inline(code, ['abschan', 'nt', 'maxnspikes', 'thresh', 'tilock', 'spiketis'],
                               type_converters=weave.converters.blitz,
                               compiler='gcc')
        return spiketis[:nspikes]


class MultiPhasic(FixedThresh):
    """Multiphasic filter - spikes triggered only when consecutive
    thresholds of opposite polarity occur on a given channel within
    a specified time window delta_t

    That is, either:

        1) s_i(t) > f and s_i(t + t') < -f
        2) s_i(t) < -f and s_it(t + t') > f

    for 0 < t' <= delta_t
    """

    STDEV_MULT = 4
    SPIKE_PRE = 250
    SPIKE_POST = 750
    SEARCH_SPAN = 1000
    LOCKOUT = 1000
    delta_t = 300

    def find(self):
        """Maintain state and search forward for a spike"""

        # keep on sliding our search window forward to find spikes
        while True:

            # check if we have a channel firing above threshold
            chan_events = []
            for chan, thresh in self.thresholds.iteritems():
                # this will only be along one dimension
                _ev = where(numpy.abs(self.window.data[chan]) > thresh)[0]

                if len(_ev) <= 0:
                    continue

                thresh_vals = [(self.window.data[chan][ind], ind) \
                                    for ind in _ev.tolist()]
                # for each threshold value, scan forwrd in time delta_t
                # to see if an opposite threshold crossing occurred
                for i, tup in enumerate(thresh_vals):
                    val, ind = tup
                    sgn = numpy.sign(val)
                    t = self.window.ts[ind]
                    for cand_val, t_ind in thresh_vals[i + 1:]:
                        # check ahead only with delt_t
                        if self.window.ts[t_ind] - t > self.delta_t:
                            break
                        cand_sgn = numpy.sign(cand_val)
                        # check if threshold crossings are opposite
                        # polarity
                        if cand_sgn != sgn:
                            chan_events.append((ind, chan))
                            break

                for evt in self.yield_events(chan_events):
                    yield evt

            self.curr += self.search_span
            self.window = self.stream[self.curr:self.curr + self.search_span]


class DynamicMultiPhasic(FixedThresh):
    """Dynamic Multiphasic filter - spikes triggered only when consecutive
    thresholds of opposite polarity occured on a given channel within
    a specified time window delta_t, where the second threshold level is
    determined relative to the amplitude of the waveform peak/valley
    following initial phase trigger

    That is, either:

        1) s_i(t) > f and s_i(t + t') < f_pk - f'
    or  2) s_i(t) < -f and s_it(t + t') > f_val + f'

    for -delta_t < t' <= delta_t
    and where f' is the minimum amplitdude inflection in delta_t
    """

    STDEV_MULT = 4
    SPIKE_PRE = 250
    SPIKE_POST = 750
    SEARCH_SPAN = 1000
    LOCKOUT = 1000
    delta_t = 300

    def setup(self):
        FixedThreshold.setup(self)
        self.f_inflect = {}
        # set f' to be 3.5 * standard deviation (see paper)
        for chan, val in self.std.iteritems():
            self.f_inflect[chan] = 3.5 * val

    def find(self):
        """Maintain state and search forward for a spike"""

        # keep on sliding our search window forward to find spikes
        while True:

            # check if we have a channel firing above threshold
            chan_events = []
            for chan, thresh in self.thresholds.iteritems():
                # this will only be along one dimension
                _ev = where(numpy.abs(self.window.data[chan]) > thresh)[0]

                if len(_ev) <= 0:
                    continue

                thresh_vals = [(self.window.data[chan][ind], ind) \
                                    for ind in _ev.tolist()]

                # for each threshold value, scan forwrd in time delta_t
                # to see if an opposite threshold crossing occurred
                for val, ind in thresh_vals:

                    # scan forward to find local max or local min
                    extremal_ind = ind
                    extremal_val = val
                    #while True:
                    #    next_ind = extremal_ind + 1
                    #    next_val = self.window.data[chan][next_ind]
                    #    if abs(next_val) < abs(extremal_val):
                    #        break
                    #    extremal_val, extremal_ind = next_val, next_ind

                    # calculate our dynamic threshold
                    # TODO: make this more compact
                    if extremal_val < 0:
                        # a valley
                        dyn_thresh = extremal_val + self.f_inflect[chan]
                        dyn_events = where(self.window.data[chan] \
                                                        > dyn_thresh)[0]
                    else:
                        # a peak
                        dyn_thresh = extremal_val - self.f_inflect[chan]
                        dyn_events = where(self.window.data[chan] \
                                                        < dyn_thresh)[0]

                    dyn_vals = [(self.window.data[chan][_ind], _ind) \
                                    for _ind in dyn_events.tolist()]
                    t = self.window.ts[extremal_ind]
                    # check for next inflection
                    for dyn_val, t_ind in dyn_vals:
                        # check ahead only within +/- delta_t
                        t_prime = self.window.ts[t_ind]
                        if (t_prime > t - self.delta_t) and \
                                (t_prime <= t + self.delta_t):
                            break

                        event_val = extremal_val
                        event_ind = extremal_ind
                        if abs(dyn_val) > abs(extremal_val):
                            event_val = dyn_val
                            event_ind = t_ind
                        chan_events.append((event_ind, chan))
                        break

                # yield all the events we've found
                for evt in self.yield_events(chan_events):
                    yield evt

            self.curr += self.search_span
            self.window = self.stream[self.curr:self.curr + self.search_span]

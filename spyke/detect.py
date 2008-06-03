"""Spike detection algorithms

TODO: use median based noise estimation instead of std based
      - estimate noise level dynamically with sliding window
        and independently for each channel

TODO: spatiotemporal lockout:
      - do spatial lock out only during first 1/2 phase of trigger spike
      - phases are part of same spike if less than 250us between each other

TODO: for speed (esp comparing signal to thresh), consider converting all uV data
      from 64bit float to 16 bit integer (actually, just keep it as 16 bit to begin
      with) - ack, not being in uV would complicate things all over the place

TODO: check if python's cygwincompiler.py module has been updated to deal with
      extra version fields in latest mingw
"""

from __future__ import division

__authors__ = ['Reza Lotun, Martin Spacek']

import itertools
import sys
import time

import wx

import numpy as np

import spyke.surf
from spyke.core import WaveForm, toiter, argcut, intround, eucd
#from spyke import Spike, Template, Collection


class Detector(object):
    """Spike detector base class"""
    DEFFIXEDTHRESH = 50 # uV
    DEFNOISEMETHOD = 'median'
    DEFNOISEMULT = 4
    DEFNOISEWINDOW = 10000000 # 10 sec
    DEFMAXNSPIKES = sys.maxint
    DEFBLOCKSIZE = 1000000 # waveform data block size, us
    DEFSLOCK = 175 # um
    DEFTLOCK = 250 # us, something wider like 440 might be better

    MAXAVGFIRINGRATE = 1000 # Hz, assume no chan will trigger more than this rate of events on average within a block
    BLOCKEXCESS = 1000 # us, extra data as buffer at start and end of a block while searching for spikes. Only useful for ensuring spike times within the actual block time range are accurate. Spikes detected in the excess are discarded

    def __init__(self, stream, chans=None,
                 fixedthresh=None, noisemethod=None, noisemult=None, noisewindow=None,
                 trange=None, maxnspikes=None, blocksize=None,
                 slock=None, tlock=None):
        """Takes a data stream and sets various parameters"""
        self.stream = stream
        self.chans = chans # is a property
        # for simplicity, assign all thresh and noise attribs to all subclasses, even if some won't apply
        self.fixedthresh = fixedthresh or self.DEFFIXEDTHRESH
        self.noisemethod = noisemethod or self.DEFNOISEMETHOD
        self.noisemult = noisemult or self.DEFNOISEMULT
        self.noisewindow = noisewindow or self.DEFNOISEWINDOW
        self.trange = trange or (stream.t0, stream.tend)
        self.maxnspikes = maxnspikes or self.DEFMAXNSPIKES # return at most this many spikes, applies across chans
        self.blocksize = blocksize or self.DEFBLOCKSIZE
        self.slock = slock or self.DEFSLOCK
        self.tlock = tlock or self.DEFTLOCK

    def get_chans(self):
        return self._chans

    def set_chans(self, chans):
        if chans == None:
            chans = range(self.stream.nchans) # search all channels
        self._chans = toiter(chans) # need not be contiguous
        self._chans.sort() # make sure they're in order
        self.dm = self.get_distance_matrix() # Euclidean channel distance matrix, in self.chans order

    chans = property(get_chans, set_chans)

    def get_distance_matrix(self):
        """Get channel distance matrix, in um"""
        sl = self.stream.probe.SiteLoc
        coords = []
        for chan in self.chans:
            coords.append(sl[chan])
        return eucd(coords)
    '''
    def __iter__(self):
        """Returns an iterator object. Called in for loops and in 'in' statements, and by the iter() f'n?.
        This is here to allow you to treat any of the detection classes directly as iterators"""
        spikes = iter(self.find())
        # why not just return the iterator, and let the outside world iterate over it, like this:?
        return (chan, spiketime), or maybe a Spike object with .chan and .ts attribs
        #while True:
        #    try:
        #        yield spikes.next()
        #    except StopIteration:
        #        break
    '''
    def get_thresh(self, chan):
        """Calculate either median or stdev based threshold for a given chan"""
        if self.noisemethod == 'median':
            self.get_median_thresh(chan)
        elif  self.noisemethod == 'stdev':
            self.get_stdev_thresh(chan)

    def get_median_thresh(self, chan):
        return self.get_median_noise(chan) * self.MEDIAN_MULT

    def get_stdev_thresh(self, chan):
        return self.get_stdev_noise(chan) * self.STDEV_MULT

    def get_median_noise(self, chan):
        """Overriden by FixedThresh and DynamicThresh classes"""
        pass

    def get_stdev_noise(self, chan):
        """Overriden by FixedThresh and DynamicThresh classes"""
        pass

    def us2nt(self, us):
        """Convert time in us to nearest number of eq'v timepoints in stream"""
        nt = intround(self.stream.layout.sampfreqperchan * us / 1000000)
        # prevent rounding nt down to 0. This way, even the smallest
        # non-zero us will get you at least 1 timepoint
        if nt == 0 and us != 0:
            nt = 1
        return nt


class FixedThresh(Detector):
    """Base class for fixed threshold spike detection,
    Uses the same single static threshold throughout the entire file,
    with an independent threshold for every channel"""

    def __init__(self, *args, **kwargs):
        Detector.__init__(self, *args, **kwargs)

    def get_median_noise(self, chan):
        pass

    def get_median_thresh(self, chan):
        pass
    '''
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
        pass

    def get_stdev_noise(self, chan):
        pass


#from detect_weave import BipolarAmplitudeFixedThresh_Weave
from detect_cy import BipolarAmplitudeFixedThresh_Cy


class BipolarAmplitudeFixedThresh(FixedThresh,
                                  #BipolarAmplitudeFixedThresh_Weave,
                                  BipolarAmplitudeFixedThresh_Cy):
    """Bipolar amplitude fixed threshold detector,
    with fixed temporal lockout on all channels, plus a spatial lockout"""

    def search(self):
        """Search for spikes. Divides large searches into more manageable
        blocks of (slightly overlapping) multichannel waveform data, and
        then combines the results. method = 'all' or 'indep' treats chans
        together or independently"""
        t0 = time.clock()

        bs = self.blocksize
        bx = self.BLOCKEXCESS
        maxnspikesperchanperblock = bs/1000000 * self.MAXAVGFIRINGRATE # num elements per chan to preallocate before searching a block
        # reset this at the start of every search
        self.totalnspikes = 0 # total num spikes found across all chans so far by this Detector
        # hold temp spiketimes and maxchans for .searchblock, reused on every call
        self._spiketimes = np.empty(len(self.chans)*maxnspikesperchanperblock, dtype=np.int64)
        self._maxchans = np.empty(len(self.chans)*maxnspikesperchanperblock, dtype=int)
        #self.spiketis = np.zeros((2, len(self.chans)*maxnspikesperchanperblock), dtype=int) # row0: ti, row1: maxchanii
        self.tilock = self.us2nt(self.tlock)

        # generate time ranges for slightly overlapping blocks of data
        wavetranges = []
        cutranges = []
        tstep = 1
        if self.trange[1] < self.trange[0]: # search backward
            bs = -bs
            bx = -bx
            tstep = -1
        es = range(self.trange[0], self.trange[1], bs) # left (or right) edges of data blocks
        for e in es:
            wavetranges.append((e-bx, e+bs+bx)) # time range of waveform to give to .searchblock
            cutranges.append((e, e+bs)) # time range of spikes to keep from those returned by .searchblock
        # last wavetrange and cutrange surpass self.trange[1], fix that here:
        wavetranges[-1] = (wavetranges[-1][0], self.trange[1]+bx) # replace with a new tuple
        cutranges[-1] = (cutranges[-1][0], self.trange[1])

        spikes = [] # list of 2D spike arrays returned by searchblock, one array per block
        for wavetrange, cutrange in zip(wavetranges, cutranges): # iterate over blocks
            #tblock = time.clock()
            if self.totalnspikes < self.maxnspikes:
                #tslice = time.clock()
                print 'wavetrange: %r, cutrange: %r' % (wavetrange, cutrange)
                tlo, thi = wavetrange
                wave = self.stream[tlo:thi:tstep] # a block (Waveform) of multichan data.
                #print 'whole stream slice took %.3f sec' % (time.clock()-tslice)
                #tsearchblock = time.clock()
                spiketimesmaxchans = self.searchblock(wave, cutrange)
                #print '.searchblock() took %.3f sec' % (time.clock()-tsearchblock)
                wx.Yield() # allow wx GUI event processing during search
                lo, hi = argcut(spiketimesmaxchans[0], cutrange) # get slice timepoint indices for removing excess
                # TODO: remove any spikes that happen right at the first or last timepoint in the file,
                # since we can't say when an interrupted rising edge would've reached peak
                spiketimesmaxchans = spiketimesmaxchans[:, lo:hi:tstep] # slice it to remove excess
                #print 'found %d spikes' % spiketimesmaxchans.shape[1]
                spikes.append(spiketimesmaxchans)
            else:
                break # out of for loop
            #print 'block loop took %.3f sec' % (time.clock()-tblock)
        spikes = np.concatenate(spikes, axis=1)
        print 'found %d spikes in total' % spikes.shape[1]
        print 'inside .search() took %.3f sec' % (time.clock()-t0)
        return spikes


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
                _ev = np.where(numpy.abs(self.window.data[chan]) > thresh)[0]

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
        FixedThresh.setup(self)
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
                _ev = np.where(numpy.abs(self.window.data[chan]) > thresh)[0]

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
                        dyn_events = np.where(self.window.data[chan] \
                                                        > dyn_thresh)[0]
                    else:
                        # a peak
                        dyn_thresh = extremal_val - self.f_inflect[chan]
                        dyn_events = np.where(self.window.data[chan] \
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
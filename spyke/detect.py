"""Spike detection and modelling"""

from __future__ import division

__authors__ = ['Martin Spacek', 'Reza Lotun']

import itertools
import sys
import time
import string
import logging

import wx
import pylab
import matplotlib as mpl

import numpy as np
from scipy.weave import inline
#from scipy.optimize import leastsq, fmin_slsqp
#import openopt
#import nmpfit

import spyke.surf
from spyke.core import WaveForm, toiter, argcut, intround, eucd, g, g2, RM
from text import SimpleTable


#DMURANGE = 0, 500 # allowed time difference between peaks of modelled spike
TW = -250, 750 # spike time window range, us, centered on thresh xing or 1st phase of spike

#MAXNSAVEDWAVEFORMS = 250000 # prevents MemoryErrors

SPIKEDTYPE = [('id', np.uint32), ('ri', np.uint32), # ri: row index into spikes recarray
              ('chani', np.uint8), ('chanis', np.ndarray),
              ('t', np.int64), ('t0', np.int64), ('tend', np.int64),
              ('phase1ti', np.uint8), ('phase2ti', np.uint8),
              ('detection', object), ('neuron', object),
              ('wave', object), ('plt', object), ('itemID', object),
              ('Vpp', np.float32), ('x0', np.float32), ('y0', np.float32), ('dphase', np.int16)]

'''
# spikes recarray fieldnames to convert to Spike attribs when filtering
SPIKEATTRS = [ fieldname for (fieldname, dtype) in SPIKEDTYPE ]
SPIKEATTRS.remove('wavedata')

def filterobjs(in_objs):
    in_objs = toiter(in_objs)
    out_objs = []
    for obj in in_objs:
        if type(obj) != np.rec.record:
            out_objs.append(obj)
            continue
        # it's a simple spike record, turn it into a full-fledged Spike
        s = Spike()
        for attr in SPIKEATTRS:
            s.__dict__[attr] = obj[attr]
        if obj.wavedata != None:
            tres = s.detection.sort.tres
            wave = WaveForm(data=obj.wavedata,
                            ts=np.arange(obj.t0, obj.tend, tres),
                            chans=s.chans)
            s.wave = wave
        out_objs.append(s) # replace spike record with Spike
    return out_objs
'''

def get_wave(obj, stream=None):
    """Return object's waveform, taken from the given stream"""
    if type(obj) != np.rec.record: # it's a Neuron
        if not hasattr(obj, 'wave') or obj.wave == None:
            wave = obj.update_wave(stream) # call Neuron method
            return wave
        else:
            return obj.wave
    # it's a spike record
    s = obj
    if s.wave != None:
        return s.wave
    if stream == None:
        raise RuntimeError("No stream open, can't get wave for %s %d" % (s, s.id))
    if s.detection.detector.srffname != stream.srffname:
        msg = ("Spike %d was extracted from .srf file %s.\n"
               "The currently opened .srf file is %s.\n"
               "Can't get spike %d's wave" %
               (s.id, s.detection.detector.srffname, stream.srffname, s.id))
        wx.MessageBox(msg, caption="Error", style=wx.OK|wx.ICON_EXCLAMATION)
        raise RuntimeError(msg)
    #tres = s.detection.sort.tres
    #ts = np.arange(s.t0, s.tend, tres) # build them up
    chans = s.detection.detector.chans[s.chanis] # dereference
    wave = stream[s.t0 : s.tend]
    # can't do this cuz chanis indexes only into enabled chans,
    return wave[chans]


logger = logging.Logger('detection')
shandler = logging.StreamHandler(strm=sys.stdout) # prints to screen
formatter = logging.Formatter('%(message)s')
shandler.setFormatter(formatter)
shandler.setLevel(logging.INFO) # log info level and higher to screen
logger.addHandler(shandler)
info = logger.info

DEBUG = False # print detection debug messages to log file? slows down detection

if DEBUG:
    # print detection info and debug msgs to file, and info msgs to screen
    logfname = r'C:\data\ptc15\detection.log' # TODO: stop hard coding the folder
    logf = open(logfname, 'w')
    fhandler = logging.StreamHandler(strm=logf) # prints to file
    fhandler.setFormatter(formatter)
    fhandler.setLevel(logging.DEBUG) # log debug level and higher to file
    logger.addHandler(fhandler)
    debug = logger.debug


def arglocalextrema(signal):
    """Return indices of all local extrema in 1D signal"""
    nt = len(signal) # it's possible to have a local extremum at every point
    stride = signal.strides[0] // signal.dtype.itemsize # signal might not be contiguous
    exti = np.zeros(nt, dtype=int)
    code = ("""
    #line 60 "detect.py"
    int n_ext = 0;
    int now, last, last2;
    // let's start from timepoint 2 (0-based)
    last = signal[stride]; // signal[(2-1)*stride] // signal 1 timepoint ago
    last2 = signal[0]; // signal[(2-2)*stride] // signal 2 timepoints ago
    for (int ti=2; ti<nt; ti++) {
        now = signal[ti*stride]; // signal at current timepoint
        // Two methods, equally fast. First one isn't quite correct, 2nd one is.
        // Test with signal = np.array([0, -5, -5, -5, -2]) and signal = -signal
        // should get 3 as an answer in both cases
        // Method 1: not quite right
        // if ((last2 < last) == (last > now)) {
        // Method 2: gives correct answer for consecutive identical points, both +ve and -ve:
        if ((last2 <= last && last > now) || (last2 >= last && last < now)) {
            exti[n_ext] = ti-1; // save previous time index
            n_ext++;
        }
        // update for next loop, reuse 'now' value instead of constantly indexing into signal
        last2 = last;
        last = now;
    }
    return_val = n_ext;""")
    n_ext = inline(code, ['signal', 'nt', 'stride', 'exti'], compiler='gcc')
    return exti[:n_ext]

def arg2ndpeak(signal, exti, peak1i, dir2, dti, ppthresh):
    """Return signal's biggest local extremum of opposite sign,
    in direction dir2, and within dti indices of signal at peak1i"""
    #assert type(peak1i) == int
    #assert type(dti) == int
    stride = signal.strides[0] // signal.dtype.itemsize # signal might not be contiguous
    if dir2 == 'left':
        exti = exti[exti < peak1i] # keep only left half of exti
    elif dir2 == 'right':
        exti = exti[exti > peak1i] # keep only right half of exti
    elif dir2 == 'both':
        pass # keep all of exti
    else:
        raise ValueError('unknown dir2 %r' % dir2)
    # abs(now - peak1) converts to Python int, so ppthresh has to be same type for >= comparison
    ppthresh = int(ppthresh) # convert from np.int16 type to Python int
    n_ext = len(exti)
    code = ("""
    #line 103 "detect.py"
    // index into signal to get voltages
    int peak1 = signal[peak1i*stride]; // int or short, doesn't matter
    int peak2i = -1; // 2nd peak index, -1 indicates suitable 2nd peak not yet found
    int peak2 = 0; // 2nd peak value
    int i, now;
    // test all extrema in exti
    for (int ei=0; ei<n_ext; ei++) {
        i = exti[ei]; // ei'th extremum's index into signal
        now = signal[i*stride]; // signal value at i
        if ((abs(i-peak1i) <= dti) && // if extremum's index is within dti of peak1i
            (now * peak1 < 0) && // and is of opposite sign
            (abs(now) > abs(peak2)) && // and is bigger than last one found
            (abs(now - peak1) >= ppthresh)) { // and resulting Vpp exceeds ppthresh
                peak2i = i; // save it
                peak2 = now;
        }
    }
    return_val = peak2i;""")
    peak2i = inline(code, ['signal', 'stride', 'exti', 'n_ext', 'peak1i', 'dti', 'ppthresh'],
                    compiler='gcc')
    if peak2i == -1:
        raise NoPeakError("can't find suitable 2nd peak")
    return peak2i

def get_edges(wave, thresh):
    """Return n x 2 array (ti, chani) of indices of all threshold crossings
    in wave.data. Total wave.data should have no more than 2**31 elements in it"""
    '''
    # using pure numpy this way is slow:
    edges = np.diff(np.int8( np.abs(wave.data) >= np.vstack(self.thresh) )) # indices where changing abs(signal) has crossed thresh
    edgeis = np.where(edges.T == 1) # indices of +ve edges, where increasing abs(signal) has crossed thresh
    edgeis = np.transpose(edgeis) # columns are [ti, chani], rows temporally sorted
    for i, edgei in enumerate(edgeis):
        print("edge %d, (%d, %d)" % (i+1, edgei[0], edgei[1]))
    return edgeis
    '''
    data = wave.data
    assert data.size < 2**31 # we're sticking with signed int32 indices for speed
    itemsize = data.dtype.itemsize
    stride0 = data.strides[0] // itemsize
    stride1 = data.strides[1] // itemsize
    #assert (thresh >= 0).all() # assume it's passed as +ve
    # NOTE: taking abs(data) in advance doesn't seem faster than constantly calling abs() in the loop
    nchans, nt = data.shape
    #assert nchans == len(thresh)
    # TODO: this could be sped up by declaring a pointer to data and calculating byte
    # offsets directly in the C code, instead of relying on weave to do it for you
    code = (r"""
    #line 149 "detect.py"
    int nd = 2; // num dimensions of output edgeis array
    npy_intp dimsarr[nd];
    int leninc = 16384; // 2**14
    dimsarr[0] = 4*leninc; // nrows
    dimsarr[1] = 2;        // ncols
    PyArrayObject *edgeis = (PyArrayObject *) PyArray_SimpleNew(nd, dimsarr, NPY_INT);

    PyArray_Dims dims; // stores current dimension info of edgeis array
    dims.len = nd;
    dims.ptr = dimsarr;
    PyObject *OK;

    int nedges = 0;
    int i; // can get 12 hours of timestamps at 50 kHz with signed int32, but only 0.2 hours of sample
           // indices for 54 chans, but wave.data should be much shorter than that anyway
    for (int ti=1; ti<nt; ti++) { // start at 1'th timepoint so we can index back 1 timepoint into the past
        for (int ci=0; ci<nchans; ci++) {
            i = ci*stride0 + ti*stride1; // calculate only once for speed
            if (abs(data[i]) >= thresh[ci] && abs(data[i-stride1]) < thresh[ci]) {
                // abs(voltage) has crossed threshold
                if (nedges == PyArray_DIM(edgeis, 0)) { // allocate more rows to edgeis array
                    printf("allocating more memory!\n");
                    dims.ptr[0] += leninc; // add leninc more rows to edgeis
                    OK = PyArray_Resize(edgeis, &dims, 0, NPY_ANYORDER); // 0 arg means don't check refcount or edgeis
                    if (OK == NULL) {
                        PyErr_Format(PyExc_TypeError, "can't resize edgeis");
                        return NULL;
                    }
                    // don't need 'OK' anymore I guess, see
                    // http://www.mail-archive.com/numpy-discussion@scipy.org/msg13013.html
                    Py_DECREF(OK);
                    printf("edgeis is now %d long\n", dims.ptr[0]);
                }
                // get pointer to i,jth entry in data, typecast appropriately,
                // then dereference the whole thing so you can assign
                // a value to it. Using PyArray_GETPTR2 macro is easier than
                // manually doing pointer math using strides, but might be slower?
                *((int *) PyArray_GETPTR2(edgeis, nedges, 0)) = ti; // assign to nedges'th row, col 0
                *((int *) PyArray_GETPTR2(edgeis, nedges, 1)) = ci; // assign to nedges'th row, col 1
                nedges++;
                // multi arg doesn't print right, even with %ld formatter, need a %lld formatter
                //printf("edge %d: (%d, %d)\n", nedges, ti, ci);
                // use this hack instead:
                //printf("edge %d: ", nedges);
                //printf("(%d, ", ti);
                //printf("%d)\n", ci);
            }
        }
    }

    // resize edgeis once more to reduce edgeis down to
    // just those values that were added to it
    dims.ptr[0] = nedges;
    OK = PyArray_Resize(edgeis, &dims, 0, NPY_ANYORDER);
    if (OK == NULL) {
        PyErr_Format(PyExc_TypeError, "can't resize edgeis");
        return NULL;
    }
    Py_DECREF(OK);
    //printf("shrunk edgeis to be %d long\n", dims.ptr[0]);
    //return_val = (PyObject *) edgeis;  // these two both
    return_val = PyArray_Return(edgeis); // seem to work
    """)
    edgeis = inline(code, ['data', 'nchans', 'nt', 'stride0', 'stride1', 'thresh'],
                    compiler='gcc')
    print("found %d edges" % len(edgeis))
    return edgeis


class FoundEnoughSpikesError(ValueError):
    pass

class NoPeakError(ValueError):
    pass


class RandomWaveTranges(object):
    """Iterator that spits out time ranges of width bs with
    excess bx that begin randomly from within the given trange.
    Optionally spits out no more than maxntranges tranges"""
    def __init__(self, trange, bs, bx=0, maxntranges=None):
        self.trange = trange
        self.bs = bs
        self.bx = bx
        self.maxntranges = maxntranges
        self.ntranges = 0

    def next(self):
        if self.maxntranges != None and self.ntranges >= self.maxntranges:
            raise StopIteration
        # random int within trange
        t0 = np.random.randint(low=self.trange[0], high=self.trange[1])
        tend = t0 + self.bs
        self.ntranges += 1
        return (t0-self.bx, tend+self.bx)

    def __iter__(self):
        return self


class DistanceMatrix(object):
    """Channel distance matrix, with rows in .data corresponding to
    .chans and .coords"""
    def __init__(self, SiteLoc):
        """SiteLoc is a dictionary of (x, y) tuples, with chans as the keys. See probes.py"""
        chans_coords = SiteLoc.items() # list of (chan, coords) tuples
        chans_coords.sort() # sort by chan
        self.chans = [ chan_coord[0] for chan_coord in chans_coords ] # pull out the sorted chans
        self.coords = [ chan_coord[1] for chan_coord in chans_coords ] # pull out the coords, now in chan order
        self.data = eucd(self.coords)
    '''
    # unused, therefore best left commented out:
    def __getitem__(self, key):
        """Make distance matrix data directly indexable by chan or chan pairs
        (instead of chani pairs). Return the distance between the chans in key.
        The order of the two chans in key doesn't matter, since .data is a symmetric matrix"""
        key = toiter(key)
        i0 = np.where(np.asarray(self.chans) == key[0]) # row index into .data of chan in key[0]
        if len(key) == 1:
            return self.data[i0].squeeze() # return a whole row of distances
        elif len(key) == 2:
            i1 = np.where(np.asarray(self.chans) == key[1]) # column index into .data of chan in key[1]
            return self.data[i0, i1] # return single distance value between the two specified chans
        else:
            raise ValueError, 'key must specify 1 or 2 chans'
    '''
'''
# no longer necessary, now that spikes recarray is being used
class Spike(object):
    """A Spike"""
    def __eq__(self, other):
        """Compare Spikes according to their hashes"""
        if self.__class__ != other.__class__: # might be comparing a Spike with a Neuron
            return False
        return hash(self) == hash(other) # good enough for just simple detection

    def __hash__(self):
        """Unique hash value for self, based on spike time and location.
        Required for effectively using Spikes in a Set, and for testing equality"""
        return hash((self.t, self.chani)) # hash of their tuple, should guarantee uniqueness

    def __repr__(self):
        chan = self.detection.detector.chans[self.chani] # dereference
        return str((self.t, chan))

    def __getstate__(self):
        """Get object state for pickling"""
        d = self.__dict__.copy() # this doesn't seem to be a slow step
        # when deleting a dict entry, the strategy here is to use
        # d.pop(entry, None) to not raise an error if the entry doesn't exist
        if not self.detection.sort.SAVEWAVES:
            d.pop('wave', None) # clear wave (if any) to save space and time during pickling
        if 'neuron' in d and d['neuron'] == None:
            del d['neuron']
        # TODO: do spikes really need a .neuron attribute at all? How about just a .nid attribute? .nid would be invalid
        # after a renumbering of the neurons
        d.pop('plt', None) # clear plot (if any) self is assigned to, since that'll have changed anyway on unpickle
        d.pop('itemID', None) # clear tree item ID (if any) since that'll have changed anyway on unpickle
        return d

    # TODO: Could potentially define __setstate___ to reset .wave, .neuron,
    # .plt, and .itemID back to None if they don't exist in the d returned
    # from the pickle. This might make it easier to work with other
    # code that relies on all of these attribs exsting all the time"""

    def get_chan(self):
        return self.detection.detector.chans[self.chani] # dereference max chani

    def set_chan(self, chans):
        raise RuntimeError("spike.chan isn't settable, set spike.chani instead")

    chan = property(get_chan, set_chan)

    def get_chans(self):
        return self.detection.detector.chans[self.chanis] # dereference

    def set_chans(self, chans):
        raise RuntimeError("spike.chans isn't settable, set spike.chanis instead")

    chans = property(get_chans, set_chans)

    def update_wave(self, stream, tw=None):
        """Load/update self's waveform taken from the given stream.
        Optionally slice it according to tw around self's spike time"""
        if stream == None:
            raise RuntimeError("No stream open, can't update waveform for spike %d" % self.id)
        if self.detection.detector.srffname != stream.srffname:
            msg = ("Spike %d was extracted from .srf file %s.\n"
                   "The currently opened .srf file is %s.\n"
                   "Can't update spike %d's waveform." %
                   (self.id, self.detection.detector.srffname, stream.srffname, self.id))
            wx.MessageBox(msg, caption="Error", style=wx.OK|wx.ICON_EXCLAMATION)
            raise RuntimeError(msg)
        wave = stream[self.t0 : self.tend]
        ts = np.arange(self.t0, self.tend, stream.tres) # build them up
        # can't do this cuz chanis indexes only into enabled chans,
        # not into all stream chans represented in data array:
        #data = wave.data[self.chanis]
        chans = self.detection.detector.chans[self.chanis] # dereference
        data = wave[chans].data # maybe a bit slower, but correct
        #assert data.shape[1] == len(np.arange(s.t0, s.tend, stream.tres)) # make sure I know what I'm doing
        self.wave = WaveForm(data=data, ts=ts, chans=chans)
        if tw != None:
            self.wave = self[self.t+tw[0] : self.t+tw[1]]
        return self.wave
'''

'''
class SpikeModel(Spike):
    """A model for fitting two voltage Gaussians to spike phases,
    plus a 2D spatial gaussian to model decay across channels"""
    def __init__(self):
        self.errs = []
        self.valid = False # modelled event is assumed not to be a spike until proven spike-worthy
        self.sxsyfactor = 3 # sx and sy need to be within this factor of each other

    def __eq__(self, other):
        """Compare SpikeModels by their parameter arrays"""
        if self.__class__ != other.__class__: # might be comparing a Spike with a Neuron
            return False
        return np.all(self.p == other.p) # disable for now while not modelling

    def __getitem__(self, key):
        """Return WaveForm for this spike given slice key"""
        assert type(key) == slice
        if self.wave != None:
            return self.wave[key] # slice existing .wave
        else: # existing .wave unavailable
            return WaveForm() # return empty waveform

    def __getstate__(self):
        """Get object state for pickling"""
        d = Spike.__getstate__(self)
        d['errs'] = None
        return d

    def plot(self):
        """Plot modelled and raw data for all chans, plus the single spatially
        positioned source time series, along with its 1 sigma ellipse"""
        # TODO: also plot the initial estimate of the model, according to p0, to see how the algoritm has changed wrt it
        ts, p = self.ts, self.p
        V1, V2, mu1, mu2, s1, s2, x0, y0, sx, sy, theta = p
        uV2um = 45 / 100 # um/uV
        us2um = 75 / 1000 # um/us
        tw = ts[-1] - ts[0]
        f = pylab.figure()
        f.canvas.Parent.SetTitle('t=%d' % self.t)
        a = f.add_axes((0, 0, 1, 1), frameon=False, alpha=1.)
        self.f, self.a = f, a
        a.set_axis_off() # turn off the x and y axis
        f.set_facecolor('black')
        f.set_edgecolor('black')
        xmin, xmax = min(self.x), max(self.x)
        ymin, ymax = min(self.y), max(self.y)
        xrange = xmax - xmin
        yrange = ymax - ymin
        # this is set with an aspect ratio to mimic the effects of a.set_aspect('equal') without enforcing it
        f.canvas.Parent.SetSize((xrange*us2um*100, yrange*uV2um*8))
        thetadeg = theta*180/np.pi
        # plot stdev ellipse centered on middle timepoint, with bottom origin
        ellorig = x0, ymax-y0
        e = mpl.patches.Ellipse(ellorig, 2*sx, 2*sy, angle=thetadeg,
                                ec='#007700', fill=False, ls='dotted')
        a.add_patch(e)
        """
        c = mpl.patches.Circle((0, yrange-15), radius=15, # for calibrating aspect ratio of display
                                ec='#ffffff', fill=False, ls='dotted')
        a.add_patch(c)
        """
        # plot a radial arrow on the ellipse to make its vertical axis obvious. theta=0 should plot a vertical radial line
        arrow = mpl.patches.Arrow(ellorig[0], ellorig[1], -sy*np.sin(theta), sy*np.cos(theta),
                                  ec='#007700', fc='#007700', ls='solid')
        a.add_patch(arrow)
        for (V, x, y) in zip(self.V, self.x, self.y):
            t_ = (ts-ts[0]-tw/2)*us2um + x # in um, centered on the trace
            V_ = V*uV2um + (ymax-y) # in um, switch to bottom origin
            modelV_ = self.model(p, ts, x, y).ravel() * uV2um + (ymax-y) # in um, switch to bottom origin
            rawline = mpl.lines.Line2D(t_, V_, color='grey', ls='-', linewidth=1)
            modelline = mpl.lines.Line2D(t_, modelV_, color='red', ls='-', linewidth=1)
            a.add_line(rawline)
            a.add_line(modelline)
        t_ = (ts-ts[0]-tw/2)*us2um + x0 # in um
        modelsourceV_ = self.model(p, ts, x0, y0).ravel() * uV2um + (ymax-y0) # in um, switch to bottom origin
        modelsourceline = mpl.lines.Line2D(t_, modelsourceV_, color='lime', ls='-', linewidth=1)
        a.add_line(modelsourceline)
        a.autoscale_view(tight=True) # fit to enclosing figure
        a.set_aspect('equal') # this makes circles look like circles, and ellipses to tilt at the right apparent angle
        # plot vertical lines in all probe columns at self's modelled 1st and 2nd spike phase times
        colxs = list(set(self.x)) # x coords of probe columns
        ylims = a.get_ylim() # y coords of vertical line
        for colx in colxs: # plot one vertical line per spike phase per probe column
            t1_ = (self.phase1t-ts[0]-tw/2)*us2um + colx # in um
            t2_ = (self.phase2t-ts[0]-tw/2)*us2um + colx # in um
            vline1 = mpl.lines.Line2D([t1_, t1_], ylims, color='#004444', ls='dotted')
            vline2 = mpl.lines.Line2D([t2_, t2_], ylims, color='#440044', ls='dotted')
            a.add_line(vline1)
            a.add_line(vline2)

    def model(self, p, ts, x, y):
        """Sum of two Gaussians in time, modulated by a 2D spatial Gaussian.
        For each channel, return a vector of voltage values V of same length as ts.
        x and y are vectors of coordinates of each channel's spatial location.
        Output should be an (nchans, nt) matrix of modelled voltage values V"""
        V1, V2, mu1, mu2, s1, s2, x0, y0, sx, sy, theta = p
        x, y = np.inner(RM(theta), np.asarray([x-x0, y-y0]).T) # make x, y distance to origin at x0, y0, and rotate by theta
        tmodel = V1*g(mu1, s1, ts) + V2*g(mu2, s2, ts)
        smodel = g2(0, 0, sx, sy, x, y)
        return np.outer(smodel, tmodel)

    def cost(self, p, ts, x, y, V):
        """Distance of each point to the 2D target function
        Returns a matrix of errors, channels in rows, timepoints in columns.
        Seems the resulting matrix has to be flattened into an array"""
        error = np.ravel(self.model(p, ts, x, y) - V)
        self.errs.append(np.abs(error).sum())
        #sys.stdout.write('%.1f, ' % np.abs(error).sum())
        return error

    def check_theta(self):
        """Ensure theta points along long axis of spatial model ellipse.
        Since theta always points along the sy axis, ensure sy is the long axis"""
        V1, V2, mu1, mu2, s1, s2, x0, y0, sx, sy, theta = self.p
        if sx > sy:
            sx, sy = sy, sx # swap them so sy is the bigger of the two
            if theta > 0: # keep theta in [-pi/2, pi/2]
                theta = theta - np.pi/2
            else: # theta <= 0
                theta = theta + np.pi/2
            self.p = np.array([V1, V2, mu1, mu2, s1, s2, x0, y0, sx, sy, theta])

    def get_paramstr(self, p=None):
        """Get formatted string of model parameter values"""
        p = p or self.p
        V1, V2, mu1, mu2, s1, s2, x0, y0, sx, sy, theta = p
        s = ''
        s += 'V1, V2 = %d, %d uV\n' % (V1, V2)
        s += 'mu1, mu2 = %d, %d us\n' % (mu1, mu2)
        s += 's1, s2 = %d, %d us\n' % (s1, s2)
        s += 'x0, y0 = %d, %d um\n' % (x0, y0)
        s += 'sx, sy = %d, %d um\n' % (sx, sy)
        s += 'theta = %d deg' % (theta*180/np.pi)
        return s

    def print_paramstr(self, p=None):
        """Print formatted string of model parameter values"""
        print self.get_paramstr(p)


class NLLSPSpikeModel(SpikeModel):
    """Nonlinear least squares problem solver from openopt, uses Shor's R-algorithm.
    This one can handle constraints"""
    FTOL = 1e-1 # function tolerance, openopt default is 1e-6
    XTOL = 1e-6 # variable tolerance
    GTOL = 1e-6 # gradient tolerance

    """Here come the constraints. For improved speed, might want to stop passing unnecessary args"""

    """constrain self.dmurange[0] <= dmu <= self.dmurange[1]
    TODO: maybe this contraint should be on the peak separation in the sum of Gaussians,
    instead of just on the mu params. Can probably remove the lower bound on the peak separation,
    especially if it's left at 0"""
    def c0(self, p, ts, x, y, V):
        """dmu lower bound constraint"""
        dmu = abs(p[3] - p[2])
        return self.dmurange[0] - dmu # <= 0, lower bound

    def c1(self, p, ts, x, y, V):
        """dmu upper bound constraint"""
        dmu = abs(p[3] - p[2])
        return dmu - self.dmurange[1] # <= 0, upper bound

    def c2(self, p, ts, x, y, V):
        """Constrain that sx and sy need to be within some factor of each other,
        ie constrain their ratio"""
        return max(p[8], p[9]) - self.sxsyfactor*min(p[8], p[9]) # <= 0

    # TODO: constrain V1 and V2 to have opposite sign, see ptc15.87.6920

    def calc(self, ts, x, y, V):
        self.ts = ts
        self.x = x
        self.y = y
        self.V = V
        pr = openopt.NLLSP(self.cost, self.p0, args=(ts, x, y, V),
                           ftol=self.FTOL, xtol=self.XTOL, gtol=self.GTOL)
        # set lower and upper bounds on parameters:
        # limit mu1 and mu2 to within min(ts) and max(ts) - sometimes they fall outside,
        # esp if there was a poor lockout and you're triggering off a previous spike
        pr.lb[2], pr.ub[2] = min(ts), max(ts) # mu1
        pr.lb[3], pr.ub[3] = min(ts), max(ts) # mu2
        pr.lb[4], pr.ub[4] = 40, 250 # s1
        pr.lb[5], pr.ub[5] = 40, 250 # s2
        # limit x0 to within reasonable distance of vertical midline of probe
        pr.lb[6], pr.ub[6] = -50, 50 # x0
        pr.lb[8], pr.ub[8] = 20, 200 # sx
        pr.lb[9], pr.ub[9] = 20, 200 # sy
        pr.lb[10], pr.ub[10] = -np.pi/2, np.pi/2 # theta (radians)
        pr.c = [self.c0, self.c1, self.c2] # constraints
        pr.solve('nlp:ralg')
        self.pr, self.p = pr, pr.xf
        print "%d NLLSP iterations, cost f'n eval'd %d times" % (pr.iter, len(self.errs))
        self.check_theta()

    def __getstate__(self):
        """Get object state for pickling"""
        d = SpikeModel.__getstate__(self)
        # TODO: would be really nice to be able to keep the .pr attrib, for later inspection after unpickling of, say, bounds
        d['pr'] = None # don't pickle the openopt.NLLSP problem object, cuz it has lambdas which aren't picklable apparently
        return d
'''

class Detector(object):
    """Spike detector base class"""
    DEFTHRESHMETHOD = 'GlobalFixed' # GlobalFixed, ChanFixed, or Dynamic
    DEFNOISEMETHOD = 'median' # median or stdev
    DEFNOISEMULT = 4.0
    DEFFIXEDTHRESH = 60 # uV, used by GlobalFixed
    DEFPPTHRESHMULT = 1.5 # peak-to-peak threshold is this times thresh
    DEFFIXEDNOISEWIN = 10000000 # 10s, used by ChanFixed - this should really be a % of self.trange
    DEFDYNAMICNOISEWIN = 10000 # 10ms, used by Dynamic
    DEFMAXNSPIKES = 0
    DEFBLOCKSIZE = 10000000 # 10s, waveform data block size
    DEFSLOCK = 150 # spatial lockout radius, um
    DEFDT = 350 # max time between spike phases, us
    DEFRANDOMSAMPLE = False
    #DEFKEEPSPIKEWAVESONDETECT = False # only reason to turn this off is to save memory during detection
    DEFEXTRACTPARAMSONDETECT = True

    # us, extra data as buffer at start and end of a block while detecting spikes.
    # Only useful for ensuring spike times within the actual block time range are
    # accurate. Spikes detected in the excess are discarded
    BLOCKEXCESS = 1000

    def __init__(self, sort, chans=None,
                 threshmethod=None, noisemethod=None, noisemult=None, fixedthresh=None,
                 ppthreshmult=None, fixednoisewin=None, dynamicnoisewin=None,
                 trange=None, maxnspikes=None, blocksize=None,
                 slock=None, dt=None, randomsample=None,
                 #keepspikewavesondetect=None,
                 extractparamsondetect=None):
        """Takes a parent Sort session and sets various parameters"""
        self.sort = sort
        self.srffname = sort.stream.srffname # for reference, store which .srf file this Detector is run on
        self.chans = np.asarray(chans) or np.arange(sort.stream.nchans) # None means search all channels
        self.threshmethod = threshmethod or self.DEFTHRESHMETHOD
        self.noisemethod = noisemethod or self.DEFNOISEMETHOD
        self.noisemult = noisemult or self.DEFNOISEMULT
        self.fixedthresh = fixedthresh or self.DEFFIXEDTHRESH
        self.ppthreshmult = ppthreshmult or self.DEFPPTHRESHMULT
        self.fixednoisewin = fixednoisewin or self.DEFFIXEDNOISEWIN # us
        self.dynamicnoisewin = dynamicnoisewin or self.DEFDYNAMICNOISEWIN # us
        self.trange = trange or (sort.stream.t0, sort.stream.tend)
        self.maxnspikes = maxnspikes or self.DEFMAXNSPIKES # return at most this many spikes, applies across chans
        self.blocksize = blocksize or self.DEFBLOCKSIZE
        self.slock = slock or self.DEFSLOCK
        self.dt = dt or self.DEFDT
        self.randomsample = randomsample or self.DEFRANDOMSAMPLE
        #self.keepspikewavesondetect = keepspikewavesondetect or self.DEFKEEPSPIKEWAVESONDETECT
        self.extractparamsondetect = extractparamsondetect or self.DEFEXTRACTPARAMSONDETECT

        #self.dmurange = DMURANGE # allowed time difference between peaks of modelled spike
        self.tw = TW # spike time window range, us, centered on 1st phase of spike

    def detect(self):
        """Search for spikes. Divides large searches into more manageable
        blocks of (slightly overlapping) multichannel waveform data, and
        then combines the results
        TODO: remove any spikes that happen right at the first or last timepoint in the file,
        since we can't say when an interrupted falling or rising edge would've reached peak
        """
        self.enabledSiteLoc = {}
        stream = self.sort.stream
        for chan in self.chans: # for all enabled chans
            self.enabledSiteLoc[chan] = stream.probe.SiteLoc[chan] # grab its (x, y) coordinate
        self.dm = DistanceMatrix(self.enabledSiteLoc) # distance matrix for the chans enabled for this search
        self.nbhd = [] # list of neighbourhood of chanis for each chani, as defined by self.slock, each in ascending order
        for distances in self.dm.data: # iterate over rows
            chanis, = np.uint8(np.where(distances <= self.slock)) # at what col indices does the returned row fall within slock?
            ds = distances[chanis] # subset of distances
            sortis = ds.argsort()
            chanis = chanis[sortis] # sort by distance from chani
            self.nbhd.append(chanis)

        self.dti = int(self.dt // stream.tres) # convert from numpy.int64 to normal int for inline C

        t0 = time.clock()
        self.thresh = self.get_thresh() # abs, in AD units, one per chan in self.chans
        self.ppthresh = np.int16(np.round(self.thresh * self.ppthreshmult)) # peak-to-peak threshold, abs, in AD units
        AD2uV = self.sort.converter.AD2uV
        info('thresh calcs took %.3f sec' % (time.clock()-t0))
        info('thresh   = %s' % AD2uV(self.thresh))
        info('ppthresh = %s' % AD2uV(self.ppthresh))

        bs = self.blocksize
        bx = self.BLOCKEXCESS
        wavetranges, (bs, bx, direction) = self.get_blockranges(bs, bx)

        nchans = len(self.chans) # number of enabled chans
        self.lockouts = np.zeros(nchans, dtype=np.int64) # holds time indices until which each enabled chani is locked out, updated on every found spike
        self.lockouts_us = np.zeros(nchans, dtype=np.int64) # holds times in us until which each enabled chani is locked out, updated only at end of each searchblock call
        self.nspikes = 0 # total num spikes found across all chans so far by this Detector, reset at start of every search
        spikes = np.recarray(0, SPIKEDTYPE) # init

        t0 = time.clock()
        for wavetrange in wavetranges:
            try:
                blockspikes = self.searchblock(wavetrange, direction)
            except FoundEnoughSpikesError:
                break
            nblockspikes = len(blockspikes)
            spikes.resize(self.nspikes + nblockspikes, refcheck=False)
            spikes[self.nspikes:self.nspikes+nblockspikes] = blockspikes
            self.nspikes += nblockspikes
        #spikets = [ s.t for s in spikes ]
        #spikeis = np.argsort(spikets, kind='mergesort') # indices into spikes, ordered by spike time
        #spikes = [ spikes[si] for si in spikeis ] # now guaranteed to be in temporal order
        info('\nfound %d spikes in total' % len(spikes))
        info('inside .detect() took %.3f sec' % (time.clock()-t0))
        return spikes

    def searchblock(self, wavetrange, direction):
        """Search a block of data, return a list of valid Spikes"""
        #info('searchblock():')
        stream = self.sort.stream
        info('self.nspikes=%d, self.maxnspikes=%d, wavetrange=%s, direction=%d' %
             (self.nspikes, self.maxnspikes, wavetrange, direction))
        if self.nspikes >= self.maxnspikes:
            raise FoundEnoughSpikesError # skip this iteration
        #if self.nspikes > MAXNSAVEDWAVEFORMS:
        #    self.keepspikewavesondetect = False
        tlo, thi = wavetrange # tlo could be > thi
        bx = self.BLOCKEXCESS
        cutrange = (tlo+bx, thi-bx) # range without the excess, ie time range of spikes to actually keep
        info('wavetrange: %s, cutrange: %s' % (wavetrange, cutrange))
        tslice = time.clock()
        wave = stream[tlo:thi:direction] # a block (WaveForm) of multichan data, possibly reversed, ignores out of range data requests, returns up to stream limits
        print('Stream slice took %.3f sec' % (time.clock()-tslice))
        # TODO: simplify the whole channel deselection and indexing approach, maybe
        # make all chanis always index into the full probe chan layout instead of the self.chans
        # that represent which chans are enabled for this detector. Also, maybe do away with
        # the whole slicing/indexing into a WaveForm object - just work directly on the .data
        # and .chans and .ts, convenience be damned
        tres = stream.tres
        self.lockouts = np.int64((self.lockouts_us - wave.ts[0]) / tres)
        self.lockouts[self.lockouts < 0] = 0 # don't allow -ve lockout indices
        #info('at start of searchblock:\n new wave.ts[0, end] = %s\n new lockouts = %s' %
        #     ((wave.ts[0], wave.ts[-1]), self.lockouts))

        if self.randomsample:
            maxnspikes = 1 # how many more we're looking for in the next block
        else:
            maxnspikes = self.maxnspikes - self.nspikes

        twts = np.arange(self.tw[0], self.tw[1], tres) # temporal window timepoints wrt thresh xing or phase1t
        twts += twts[0] % tres # get rid of mod, so twts go through zero
        self.twi = int(round(twts[0] / tres)), int(round(twts[-1] / tres)) # time window indices wrt thresh xing or 1st phase
        #info('twi = %s' % (self.twi,))

        # want an nchan*2 array of [chani, x/ycoord]
        # TODO: why is this done on every searchblock call? Doesn't take any noticeable amount of time
        xycoords = [ self.enabledSiteLoc[chan] for chan in self.chans ] # (x, y) coords in chan order
        xcoords = np.asarray([ xycoord[0] for xycoord in xycoords ])
        ycoords = np.asarray([ xycoord[1] for xycoord in xycoords ])
        self.siteloc = np.asarray([xcoords, ycoords]).T # index into with chani to get (x, y)
        tget_edges = time.clock()
        edgeis = get_edges(wave, self.thresh)
        info('get_edges() took %.3f sec' % (time.clock()-tget_edges))
        tcheck_edges = time.clock()
        spikes = self.check_edges(wave, edgeis, cutrange)
        info('checking edges took %.3f sec' % (time.clock()-tcheck_edges))
        print('found %d spikes' % len(spikes))
        #import cProfile
        #cProfile.runctx('spikes = self.check_edges(wave, edgeis, cutrange)', globals(), locals())
        #spikes = []
        self.lockouts_us = wave.ts[self.lockouts] # lockouts in us, use this to propagate lockouts to next searchblock call
        #info('at end of searchblock:\n lockouts = %s\n new lockouts_us = %s' %
        #     (self.lockouts, self.lockouts_us))
        return spikes

    def check_edges(self, wave, edgeis, cutrange):
        """Check which edges (threshold crossings) in wave data look like spikes
        and return only events that fall within cutrange. Search in window
        forward from thresh for a peak, then in appropriate direction from
        that peak (based on sign of signal) for up to self.dt for another
        one of opposite sign. If you don't find a 2nd one that meets these
        criteria, it ain't a spike.

        TODO: would be nice to use some multichannel thresholding, instead
        of just single independent channel
            - e.g. obvious but small multichan spike at ptc15.87.23340
            - hyperellipsoidal?
            - take mean of sets of chans (say one set per chan, slock of chans
            around it), check when they exceed thresh, find max chan within
            that set at that time and report it as a threshold event
            - or slide some filter across the data in space and time that not
            only checks for thresh, but ppthresh as well

        TODO: when searching for maxchan, new one should exceed current in Vpp, not just in Vp
        at phase1t. See ptc15.87.35040. Best to use Vpp instead of just size of a single phase
        when deciding on maxchan

        TODO: Another problem at ptc15.87.35040
        is that it did actually detect the original teal chan 5 as a spike, but
        then when it went to look for phase2 on the new purple maxchan 6 and couldn't find it,
        it gave up completely instead of reverting back to the previous maxchan. Maybe I should
        save things that are definitely spike-like before looking for new maxchan, and revert to those
        if a PeakError is raised... Maybe I should be doing recursive calls?

        TODO: make lockout in space and time proportional to the size (and slope?) of signal
        on each chan at the 2nd phase on the maxchan
            - on the maxchan, lockout for some determined time after 2nd phase (say one dphase),
            on others lock out a proportionally less amount in time (say V2/V2maxchan*dphase)
            - should help with nearly overlapping spikes, such as at ptc15.87.89740
        - or more crudely?: for chans within slock radius, lockout only those that
        exceeded thresh within the window

        TODO: search local window in space and time *simultaneously* for biggest signal,
        deal with biggest one first, maybe try to associate it with the nearest preceding thresh xing,
        then go back after applying lockout and deal with the
        smaller fry. ex. ptc15.87.125820 and 89760.
        Also, see error choosing the wrong maxchan due to sequential time-space-time-space
        search at ptc15.87.68420 (should detect grey maxchan 7, not slightly earlier magenta maxchan 46)
            - maybe partition the data into 2D tiles with some overlap

        TODO: keep an eye on broad spike at ptc15.87.1024880, about 340 us wide. Should be counted though
        """
        AD2uV = self.sort.converter.AD2uV
        extractXY = self.sort.extractor.extractXY
        lockouts = self.lockouts
        twi = self.twi
        nspikes = 0
        spikes = np.recarray(len(edgeis), SPIKEDTYPE) # nspikes will always be far less than nedgeis
        # check each edge for validity
        for ti, chani in edgeis: # ti begins life as the threshold xing time index
            chan = self.chans[chani]
            if DEBUG: debug('*** trying thresh event at t=%d chan=%d' % (wave.ts[ti], chan))
            if ti <= lockouts[chani]: # is this thresh crossing timepoint locked out?
                if DEBUG: debug('thresh event is locked out')
                continue # skip to next event

            # get data window wrt threshold crossing
            t0i = max(ti+twi[0], lockouts[chani]+1) # make sure any timepoints included prior to ti aren't locked out
            tendi = min(ti+twi[1]+1, len(wave.ts)-1) # +1 makes it end inclusive, don't go further than last wave timepoint
            window = wave.data[chani, t0i:tendi] # window of data, not necessarily contiguous

            # find spike phases
            tiw = ti - t0i # time index where ti falls wrt the window
            try:
                phase1ti, phase2ti = self.find_spike_phases(window, tiw, self.ppthresh[chani],
                                                            reftype='trigger')
            except NoPeakError, message: # doesn't qualify as a spike
                if DEBUG: debug(message)
                continue # skip to next event
            ti = t0i + phase1ti # overwrite ti, make it phase1ti wrt 0th time index
            V1, V2 = window[phase1ti], window[phase2ti]
            if DEBUG: debug('window params: t0=%d, tend=%d, phase1t=%d, phase2t=%d, V1=%d, V2=%d'
                            % (wave.ts[t0i], wave.ts[tendi],
                            wave.ts[ti], wave.ts[t0i+phase2ti],
                            V1, V2))

            # find all enabled chanis within nbhd of chani, exclude those locked-out at 1st phase
            chanis = np.asarray([ chi for chi in self.nbhd[chani] if lockouts[chi] < ti ])

            # find maxchan within chanis based on Vpp, preserve sign so nearby inverted chans are ignored
            # phase2 - phase1 on all chans, should be +ve
            # first convert from int16 to int32 to prevent overflow
            Vpps = np.int32(wave.data[chanis, t0i+phase2ti]) - np.int32(wave.data[chanis, ti])
            chanii = Vpps.argmax() # max chanii within chanis neighbourhood
            usenewchan = False
            newchani = chanis[chanii] # new max chani
            if newchani != chani: # new max chani is different from old one
                newchan = self.chans[newchani] # new max chan
                if DEBUG: debug('new maxchan %d @ (%d, %d)'
                                % (newchan, self.siteloc[newchani, 0], self.siteloc[newchani, 1]))

                # get new data window using new maxchan and wrt 1st phase this time, instead of wrt the original thresh xing
                newt0i = max(ti+twi[0], lockouts[newchani]+1) # make sure any timepoints included prior to ti aren't locked out
                newtendi = min(ti+twi[1]+1, len(wave.ts)-1) # +1 makes it end inclusive, don't go further than last wave timepoint
                newwindow = wave.data[newchani, newt0i:newtendi]
                if DEBUG: debug('new window params: t0=%d, tend=%d'
                                % (wave.ts[newt0i], wave.ts[newtendi]))

                # find spike phases again, on new maxchan in refined window, starting from new ti
                newtiw = ti - newt0i # time index where ti (current phase1ti wrt 0th time index) falls wrt the window
                try:
                    phase1ti, phase2ti = self.find_spike_phases(newwindow, newtiw, self.ppthresh[newchani],
                                                                reftype='phase')
                    usenewchan = True
                except NoPeakError, message: # doesn't qualify as a spike
                    if DEBUG:
                        debug(message)
                        debug('resorting to original maxchan %d with its successful spike' % chan)

            if usenewchan: # update vars to reflect new maxchan
                chani, chan = newchani, newchan # update
                t0i, tendi, window, tiw = newt0i, newtendi, newwindow, newtiw
                ti = t0i + phase1ti # overwrite ti, make it the new phase1ti wrt 0th time index
                V1, V2 = window[phase1ti], window[phase2ti]
                if DEBUG: debug('window params: t0=%d, tend=%d, phase1t=%d, phase2t=%d, V1=%d, V2=%d'
                                % (wave.ts[t0i], wave.ts[tendi],
                                wave.ts[ti], wave.ts[t0i+phase2ti],
                                V1, V2))
                # find all enabled chanis within nbhd of chani, exclude those locked-out at 1st phase
                chanis = np.asarray([ chi for chi in self.nbhd[chani] if lockouts[chi] < ti ])
            else:
                # get new data window using old maxchan, wrt 1st phase this time, update everything that's wrt t0i
                newt0i = max(ti+twi[0], lockouts[chani]+1) # make sure any timepoints included prior to ti aren't locked out
                dt0i = t0i - newt0i
                phase1ti += dt0i # update
                phase2ti += dt0i
                t0i = newt0i # overwrite
                tendi = min(ti+twi[1]+1, len(wave.ts)-1) # +1 makes it end inclusive, don't go further than last wave timepoint
                window = wave.data[chani, t0i:tendi]
                # TODO: This window trange might include datapoints on neighbouring chans
                # that are locked out. Not a big deal. Having different numbers of datapoints
                # per chan per spike would add complexity with little benefit
            '''
            # check if this (still roughly defined) event crosses ppthresh, and some other requirements,
            try:
                # Vpp check now happens in arg2ndpeak and raises an error immediately
                #assert abs(V2 - V1) >= self.ppthresh[chani], \
                #    "event doesn't cross ppthresh[chani=%d] = %.1f" % (chani, self.ppthresh[chani])
                #assert phase1ti-0 > 2, 'phase1t is very near window startpoint, probably a mistrigger'
                #assert len(window)-phase2ti > 2, 'phase2t is very near window endpoint, probably a mistrigger'
                #assert np.sign(V1) == -np.sign(V2), 'phases must be of opposite sign'
                #assert minV < 0, 'minV is %s V at t = %d' % (minV, wave.ts[t0i+minti])
                #assert maxV > 0, 'maxV is %s V at t = %d' % (maxV, wave.ts[t0i+maxti])
            except AssertionError, message: # doesn't qualify as a spike
                if DEBUG: debug(message)
                continue # skip to next event
            '''
            # looks like a spike. For pickling/unpickling efficiency, save as few
            # attribs as possible with the most compact representation possible.
            # Saving numpy scalars is less efficient than using basic Python types
            s = spikes[nspikes]
            s.t = wave.ts[ti]
            try:
                assert cutrange[0] <= s.t <= cutrange[1], 'spike time %r falls outside cutrange for this searchblock call, discarding' % s.t # use %r since s.t is np.int64 and %d gives TypeError if > 2**31
            except AssertionError, message: # doesn't qualify as a spike, don't change lockouts
                if DEBUG: debug(message)
                continue # skip to next event
            # keep each spike's chanis in sorted order, useful assumption used later on, like in Neuron.update_wave
            chanis.sort()
            #s.ts = wave.ts[t0i:tendi] # reconstruct this using np.arange(s.t0, s.tend, stream.tres)
            ts = wave.ts[t0i:tendi]
            s.t0, s.tend = wave.ts[t0i], wave.ts[tendi]
            s.phase1ti, s.phase2ti = phase1ti, phase2ti # wrt t0i
            s.dphase = ts[phase2ti] - ts[phase1ti] # in us
            #s.V1, s.V2 = V1, V2
            # maintain polarity, first convert from int16 to float to prevent overflow
            s.Vpp = AD2uV(np.float32(V2) - np.float32(V1))
            #chans = np.asarray(self.chans)[chanis] # dereference
            # chanis as a list is less efficient than as an array
            s.chani, s.chanis = chani, chanis
            #s.chan, s.chans = chan, chans # instead, use s.detection.detector.chans[s.chanis]
            #if self.keepspikewavesondetect: # keep spike waveform for later use
            #    s.wavedata = wave.data[chanis, t0i:tendi]
            if self.extractparamsondetect:
                wavedata = wave.data[chanis, t0i:tendi]
                # just x and y params for now
                x = self.siteloc[chanis, 0] # 1D array (row)
                y = self.siteloc[chanis, 1]
                s.x0, s.y0 = extractXY(wavedata, x, y, phase1ti, phase2ti)

            if DEBUG: debug('*** found new spike: %d @ (%d, %d)' % (s.t, self.siteloc[chani, 0], self.siteloc[chani, 1]))

            # update lockouts to 2nd phase of this spike
            #dphaseti = phase2ti - phase1ti
            lockout = t0i + phase2ti #+ dphaseti / 2
            lockouts[chanis] = lockout # same for all chans in this spike
            if DEBUG:
                lockoutt = wave.ts[lockout]
                #lockoutt = wave.ts[max(lockout, len(wave.ts)-1)] # stay inbounds
                debug('lockout = %d for chans = %s' % (lockoutt, chans))

            nspikes += 1

        spikes.resize(nspikes, refcheck=False)
        return spikes

    def find_spike_phases(self, window, tiw, ppthresh, reftype='trigger'):
        """Find spike phases within window of data: search from tiw in direction
        (which might be, say, a threshold xing point) for 1st peak, then within
        self.dti of that for a 2nd peak of opposite phase. Decide which peak
        comes first, return window indices of 1st and 2nd spike phases. reftype
        describes what tiw represents: a 'trigger' point or previously found
        spike 'phase'"""
        exti = arglocalextrema(window) # indices of local extrema, wrt window
        if len(exti) == 0:
            raise NoPeakError("can't find any extrema within window")
        if reftype == 'trigger':
            dir1 = 'right'
        elif reftype == 'phase':
            dir1 = 'nearest'
        else:
            raise ValueError('unknown reftype %r' % reftype)
        '''
        if dir1 == 'left':
            try:
                peak1i = exti[(exti <= tiw)][-1] # index of first extremum left of tiw, wrt window
            except IndexError:
                raise NoPeakError("can't find 1st peak within window")'''
        if dir1 == 'right':
            try:
                peak1i = exti[(exti >= tiw)][0] # index of first extremum right of tiw, wrt window
            except IndexError:
                raise NoPeakError("can't find 1st peak within window")
        else: # dir1 == 'nearest'
            peak1i = exti[abs(exti - tiw).argmin()]
        peak1i = int(peak1i)
        if window[peak1i] < 0: # peak1i is -ve, look right for corresponding +ve peak
            dir2 = 'right'
        else: # peak1i is +ve, look left for corresponding -ve peak
            dir2 = 'left'
        # find biggest 2nd extremum of opposite sign in dir2 within self.dti
        peak2i = arg2ndpeak(window, exti, peak1i, dir2, self.dti, ppthresh)
        # check which comes first
        if dir2 == 'right':
            #assert peak1i < peak2i
            return peak1i, peak2i
        else: # dir2 == 'left'
            #assert peak2i < peak1i
            return peak2i, peak1i
    '''
    def modelspikes(self, events):
        """Model spike events that roughly look like spikes
        TODO: needs updating to make use of given set of spikes.
        Really just need to run each Spike's .calc() method, and then
        check the output modelled params"""
        for ti, chani in events:
            print('trying to model thresh event at t=%d chan=%d'
                  % (wave.ts[ti], self.chans[chani]))
            # create a Spike model
            sm = Spike()
            chans = np.asarray(self.chans)[chanis] # dereference
            sm.chani, sm.chanis, sm.chan, sm.chans, sm.nchans = chani, chanis, chan, chans, nchans
            #sm.chanii, = np.where(sm.chanis == sm.chani) # index into chanis that returns max chani
            sm.dmurange = self.dmurange
            print 'chans  = %s' % (chans,)
            print 'chanis = %s' % (chanis,)
            ts = wave.ts[t0i:tendi]
            x = siteloc[chanis, 0] # 1D array (row)
            y = siteloc[chanis, 1]
            V = wave.data[chanis, t0i:tendi]

            x0, y0 = self.get_spike_spatial_mean(sm, wave)

            # take weighted spatial mean of chanis at phase1ti to estimate initial (x0, y0)
            multichanwindow = wave.data[chanis, t0i:tendi]
            chanweights = multichanwindow[:, phase1ti] # unnormalized, some of these may be -ve
            chanweights = chanweights / chanweights.sum() # normalized
            chanweights = np.where(chanweights >= 0, chanweights, 0) # replace -ve weights with 0
            chanweights = chanweights / chanweights.sum() # renormalized
            x0 = (chanweights * x).sum()
            y0 = (chanweights * y).sum()


            print 'maxchan @ (%d, %d), (x0, y0)=(%.1f, %.1f)' % (siteloc[chani, 0], siteloc[chani, 1], x0, y0)
            """
            TODO: more intelligent estimate of sx and sy by taking signal differences between maxchan and two nearest chans. Get all chans with x vals different from max, and make a similar list for y vals. Out of each of those lists, get the nearest (in 2D) chan(s) to maxchan (pick one), find the signal value ratio between it and the maxchan at phase1ti, plug maxchan's (x or y) coord into g(), set it equal to the ratio, and solve for sigma (sx or sy).
            """
            # initial params
            p0 = [V1, V2, # Gaussian amplitudes (uV)
                  wave.ts[t0i+phase1ti], wave.ts[t0i+phase2ti], # temporal means mu1, mu2 (us)
                  60, 60, # temporal sigmas s1, s2 (us)
                  x0, y0, # spatial origin, (um)
                  60, 60, 0] # sx, sy (um), theta (radians)
            sm.p0 = np.asarray(p0)
            sm.calc(ts, x, y, V) # calculate spatiotemporal fit

            table = SimpleTable(np.asarray([sm.p0, sm.p]),
                                headers=('V1', 'V2', 'mu1', 'mu2', 's1', 's2', 'x0', 'y0', 'sx', 'sy', 'theta'),
                                stubs=('p0', 'p'),
                                fmt={'data_fmt': ['%d', '%d', '%d', '%d', '%d', '%d', '%d', '%d', '%d', '%d', '%.3f'],
                                     'data_aligns':      'r',
                                     'table_dec_above':  '-',
                                     'table_dec_below':  '-',
                                     'header_dec_below': ''})
            print table
            """
            The peak times of the modelled f'n may not correspond to the peak times of the two phases.
            Their amplitudes certainly need not correspond. So, here I'm reading values off of the modelled
            waveform instead of just the parameters of the constituent Gaussians that make it up
            """
            V1, V2, mu1, mu2, s1, s2, x0, y0, sx, sy, theta = sm.p
            modelV = sm.model(sm.p, sm.ts, x0, y0).ravel()
            modelminti = np.argmin(modelV)
            modelmaxti = np.argmax(modelV)
            phase1ti = min(modelminti, modelmaxti) # 1st phase might be the min or the max
            phase2ti = max(modelminti, modelmaxti) # 2nd phase might be the min or the max
            phase1t = ts[phase1ti]
            phase2t = ts[phase2ti]
            dphase = phase2t - phase1t
            V1 = modelV[phase1ti] # now refers to waveform's actual modelled peak, instead of Gaussian amplitude
            V2 = modelV[phase2ti]
            Vpp = abs(V2 - V1)
            absV1V2 = abs(np.array([V1, V2]))
            bigphase = max(absV1V2)
            smallphase = min(absV1V2)

            # save calculated params back to SpikeModel, save the SpikeModel in a dict
            sm.t = phase1t # phase1t is synonym for spike time
            sm.phase1t, sm.phase2t, sm.dphase = phase1t, phase2t, dphase
            sm.V1, sm.V2, sm.Vpp, sm.x0, sm.y0 = V1, V2, Vpp, x0, y0
            key = phase1t
            while key in self.sms: # if model timepoint doesn't make for a uniqe dict key (rarely the case)
                key += 0.1 # inc it slightly until it becomes unique
            self.sms[key] = sm # save the SpikeModel object for later inspection

            # check to see if modelled spike qualifies as an actual spike
            try:
                # ensure modelled spike time doesn't violate any existing lockout on any of its modelled chans
                assert (lockout[chanis] < t0i+phase1ti).all(), 'model spike time is locked out'
                assert wave.ts[t0i] < phase1t < wave.ts[tendi], "model spike time doesn't fall within time window"
                assert bigphase >= self.thresh[chani], \
                    "model (bigphase=%.1f) doesn't cross thresh[chani=%d]=%.1f " % (bigphase, chani, self.thresh[chani])
                assert Vpp >= self.ppthresh[chani], \
                    "model (Vpp=%.1f) doesn't cross ppthresh[chani=%d]=%.1f " % (Vpp, chani, self.ppthresh[chani])
                assert self.dmurange[0] <= dphase <= self.dmurange[1], \
                    'model phases separated by %f us (outside of dmurange=%s)' % (dphase, self.dmurange)
                assert np.sign(V1) == -np.sign(V2), 'model phases must be of opposite sign'
            except AssertionError, message: # doesn't qualify as a spike
                print '%s, spiket=%d' % (message, phase1t)
                continue
            # it's a spike, record it
            sm.valid = True
            sms.append(sm) # add to list of valid SpikeModels to return
            print 'found new spike: %d @ (%d, %d)' % (phase1t, int(round(x0)), int(round(y0)))
            """
            update spatiotemporal lockout

            TODO: maybe apply the same 2D gaussian spatial filter to the lockout in time, so chans further away
            are locked out for a shorter time. Use slock as a circularly symmetric spatial sigma
            TODO: center lockout on model (x0, y0) coords, instead of max chani - this could be dangerous - if model
            got it wrong, could get a whole lotta false +ve spike detections due to spatial lockout being way off

            lock out til one (TODO: maybe it should be two?) stdev after peak of 2nd phase,
            in case there's a noisy mini spike that might cause a trigger on the way down
            """
            lockout[chanis] = t0i + phase2ti + int(round(s2 / self.sort.stream.tres))
            print 'lockout for chanis = %s' % wave.ts[lockout[chanis]]
    '''
    def get_blockranges(self, bs, bx):
        """Generate time ranges for slightly overlapping blocks of data,
        given blocksize and blockexcess"""
        wavetranges = []
        bs = abs(bs)
        bx = abs(bx)
        if self.trange[1] >= self.trange[0]: # search forward
            direction = 1
        else: # self.trange[1] < self.trange[0], # search backward
            bs = -bs
            bx = -bx
            direction = -1

        if self.randomsample:
            # wavetranges is an iterator that spits out random ranges starting from within
            # self.trange, and of width bs + 2bx
            if direction == -1:
                raise ValueError("Check trange - I'd rather not do a backwards random search")
            wavetranges = RandomWaveTranges(self.trange, bs, bx)
        else:
            es = range(self.trange[0], self.trange[1], bs) # left (or right) edges of data blocks
            for e in es:
                wavetranges.append((e-bx, e+bs+bx)) # time range of waveform to give to .searchblock
            # last wavetrange surpasses self.trange[1] by some unknown amount, fix that here:
            wavetranges[-1] = (wavetranges[-1][0], self.trange[1]+bx) # replace with a new tuple
        return wavetranges, (bs, bx, direction)

    def get_sorted_sm(self, onlyvalid=False):
        """Return (only valid) SpikeModels in a sorted list of key:val tuples"""
        l = self.sms.items()
        l.sort() # according to key (spike time)
        if onlyvalid:
            l = [ (key, sm) for (key, sm) in l if sm.valid ]
        return l

    def plot_sm(self, reversed=True, onlyvalid=True):
        """Plot all spike models in self in (reversed) sorted order"""
        sortedsm = self.get_sorted_sm(onlyvalid)
        if reversed:
            sortedsm.reverse()
        for st, sm in sortedsm:
            sm.plot()

    def get_thresh(self):
        """Return array of thresholds in AD units, one per chan in self.chans,
        according to threshmethod and noisemethod"""
        if self.threshmethod == 'GlobalFixed': # all chans have the same fixed thresh
            fixedthresh = self.sort.converter.uV2AD(self.fixedthresh) # convert to AD units
            thresh = np.tile(fixedthresh, len(self.chans))
        elif self.threshmethod == 'ChanFixed': # each chan has its own fixed thresh
            """randomly sample self.fixednoisewin's worth of data from self.trange in
            blocks of self.blocksize. NOTE: this samples with replacement, so it's
            possible, though unlikely, that some parts of the data will contribute
            more than once to the noise calculation
            """
            print('loading data to calculate noise')
            if self.fixednoisewin >= abs(self.trange[1] - self.trange[0]): # sample width exceeds search trange
                wavetranges = [self.trange] # use a single block of data, as defined by trange
            else:
                nblocks = int(round(self.fixednoisewin / self.blocksize))
                wavetranges = RandomWaveTranges(self.trange, bs=self.blocksize, bx=0, maxntranges=nblocks)
            data = []
            for wavetrange in wavetranges:
                wave = self.sort.stream[wavetrange[0]:wavetrange[1]]
                wave = wave[self.chans] # keep just the enabled chans
                data.append(wave.data)
            data = np.concatenate(data, axis=1) # int16 AD units
            noise = self.get_noise(data) # float AD units
            thresh = noise * self.noisemult # float AD units
            thresh = np.int16(np.round(thresh)) # int16 AD units
        elif self.threshmethod == 'Dynamic':
            # dynamic threshes are calculated on the fly during the search, so leave as zero for now
            # or at least they were, in the Cython code
            #thresh = np.zeros(len(self.chans), dtype=np.float32)
            raise NotImplementedError
        else:
            raise ValueError
        #assert len(thresh) == len(self.chans)
        #assert thresh.dtype == np.float32
        return thresh

    def get_noise(self, data):
        """Calculates noise over last dim in data (time), using .noisemethod"""
        print('calculating noise')
        if self.noisemethod == 'median':
            return np.median(np.abs(data), axis=-1) / 0.6745 # see Quiroga2004
        elif self.noisemethod == 'stdev':
            return np.stdev(data, axis=-1)
        else:
            raise ValueError


class Detection(object):
    """A spike detection run, which happens every time Detect is pressed.
    When you're merely searching for the previous/next spike with
    F2/F3, that's not considered a detection run"""
    def __init__(self, sort, detector, id=None, datetime=None):
        self.sort = sort
        self.detector = detector # Detector object used in this Detection run
        self.id = id
        self.datetime = datetime
        #self.spikeis = [] # list of spike IDs that came from this detection
    '''
    def __eq__(self, other):
        """Compare detection runs by their ._spikes lists"""
        return np.all(self._spikes == other._spikes)
    '''
    def set_spikeids(self, spikes):
        """Give each spike an ID, inc sort's _sid spike ID counter, and save
        array of spikeis to self"""
        # don't iterate over spikes recarray, since that generates
        # a bunch of record objects, which is slow
        nspikes = len(spikes)
        spikei0 = self.sort._sid
        spikes.id = np.arange(spikei0, spikei0+nspikes) # assign IDs in one shot
        spikes.detection = self
        self.sort._sid += nspikes # inc for next unique Detection
        self.spikeis = spikes.id

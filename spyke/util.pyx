"""Some functions written in Cython for max performance"""
cimport cython
import numpy as np
cimport numpy as np


cdef short select_short(short *a, int l, int r, int k):
    """Returns the k'th (0-based) ranked entry from float array a within left
    and right pointers l and r. This is quicksort partitioning based
    selection, taken from Sedgewick (Algorithms, 2ed 1988, p128).
    Note that this modifies a in-place"""
    cdef int i, j
    cdef short v, temp
    if r < l:
        raise ValueError, 'bad pointer range in select()'
    while r > l:
        v = a[r]
        i = l-1
        j = r
        while True:
            while True:
                i += 1
                if a[i] >= v: break
            while True:
                j -= 1
                if a[j] <= v: break
            temp = a[i] # swap a[i] and a[j]
            a[i] = a[j]
            a[j] = temp
            if j <= i: break
        a[j] = a[i]
        a[i] = a[r]
        a[r] = temp # temp was old a[j]
        if i >= k: r = i-1
        if i <= k: l = i+1
    return a[k] # return kth in 0-based

@cython.boundscheck(False)
@cython.wraparound(False)
def median_inplace_2Dshort(np.ndarray[np.int16_t, ndim=2, mode='c'] arr):
    """Assumes C-contig 2D input array. arr will probably be from a copy anyway,
    since it modifies in-place"""
    cdef Py_ssize_t nchans, nt, k, i
    cdef np.ndarray[np.int16_t, ndim=1] result
    cdef short *a
    nchans = arr.shape[0]
    nt = arr.shape[1]
    result = np.zeros(nchans, dtype=np.int16)
    k = (nt-1) // 2
    a = <short *>arr.data # short pointer to arr's .data field
    for i in range(nchans):
        result[i] = select_short(a, i*nt, i*nt+nt-1, i*nt+k) # this won't work for strided stuff
    return result


'''
cdef double mean(short *a, int N):
    cdef Py_ssize_t i # recommended type for looping
    cdef double s=0
    for i in range(N):
        s += a[i]
    s /= N
    return s


def mean2(np.ndarray[np.int16_t, ndim=1] a):
    """Uses new simpler numpy type notation for fast indexing, but is still a
    bit slower than the classical way, because you currently can't
    use the new notation with cdefs"""
    cdef Py_ssize_t i
    cdef double s=0
    for i in range(a.shape[0]):
        s += a[i]
    s /= a.shape[0]
    return s
'''

def mean_2Dshort(np.ndarray[np.int16_t, ndim=2] a):
    """Uses new simpler numpy type notation for fast indexing, but is still a
    bit slower than the classical way, because you currently can't
    use the new notation with cdefs"""
    cdef Py_ssize_t i, j, nchans, nt
    nchans = a.shape[0]
    nt = a.shape[1]
    cdef np.ndarray[np.float64_t, ndim=1] s = np.zeros(nchans, dtype=np.float64)
    for i in range(nchans):
        for j in range(nt):
            s[i] += a[i, j]
        s[i] /= nt # normalize
    return s

def gradient_ascent(np.ndarray[np.float32_t, ndim=2] data,
                    double sigma, double alpha):
    """Implement Nick's gradient ascent (mountain climbing) algorithm"""
    cdef int N = len(data) # total num data points
    cdef np.ndarray[np.float32_t, ndim=2] scouts = data # TODO: hopefully this is a copy
    cdef int M = N # current num scout points (clusters)
    cdef np.ndarray[np.int32_t, ndim=1] clusteris = np.arange(N) # cluster indices into data
    cdef int k = N # num points in vicinity of scout point
    cdef double sigma2 = sigma**2
    cdef double r = sigma # radius within which scout points are merged
    cdef double r2 = sigma2
    cdef np.ndarray[np.float32_t, ndim=2] diff
    cdef double v
    cdef Py_ssize_t i, j, iteri

    #while True:
    for iteri in range(10000):
        # move each scout point up its local gradient
        for i in range(M): # iterate over all scout points
            # measure gradient, include only points within 4*sigma
            #localdata = data[(data - scout)**2.sum() <= sigma2]
            diff = data - scouts[i]
            # mean vector of g-weighted distances between scout point and all data
            v = (diff * np.exp(-diff**2 / (2*sigma2))).sum(axis=0)
            # update scout position in direction of v
            scouts[i] += alpha / k * v

        # merge scout points sufficiently close to each other
        for i in range(M):
            # M may be decr in this loop, so this condition may
            # be reached before this loop completes
            if i >= M: break # out of for loop
            for j in range(i+1, M):
                if j >= M: break # out of for loop
                # for each pair of scouts, check if any pair is within r of each other
                diff = abs(scouts[i] - scouts[j])
                if (diff > r).any(): continue # to next iter
                if (diff**2).sum() <= r2:
                    # merge the scouts: keep scout i, ditch scout j
                    clusteris[clusteris == j] = i
                    clusteris[clusteris > j] -= 1 # decr all clust indices above j
                    M -= 1 # decr num of scouts (clusters)
                    print('merging clusters %d and %d' % (i, j))
        print '.',

    return clusteris


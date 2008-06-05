"""Main spyke window"""

from __future__ import division

__authors__ = 'Martin Spacek, Reza Lotun'

import wx
import wx.html
import cPickle
import os
import sys
import time
from copy import copy

import spyke
from spyke import core, surf, detect
from spyke.core import toiter, MU, intround
from spyke.gui.plot import ChartPanel, LFPPanel, SpikePanel
import wxglade_gui

DEFSPIKETW = 1000 # spike frame temporal window width (us)
DEFCHARTTW = 50000 # chart frame temporal window width (us)
DEFLFPTW = 1000000 # lfp frame temporal window width (us)

SPIKEFRAMEPIXPERCHAN = 80 # horizontally
SPIKEFRAMEHEIGHT = 700
CHARTFRAMESIZE = (900, SPIKEFRAMEHEIGHT)
LFPFRAMESIZE   = (250, SPIKEFRAMEHEIGHT)

FRAMEUPDATEORDER = ['spike', 'lfp', 'chart'] # chart goes last cuz it's slowest

class SpykeFrame(wxglade_gui.SpykeFrame):
    """spyke's main frame, inherits gui layout code auto-generated by wxGlade"""

    DEFAULTDIR = '/data/ptc15'
    FRAMETYPE2ID = {'spike': wx.ID_SPIKEWIN,
                    'chart': wx.ID_CHARTWIN,
                    'lfp': wx.ID_LFPWIN}
    REFTYPE2ID = {'tref': wx.ID_TREF,
                  'vref': wx.ID_VREF,
                  'caret': wx.ID_CARET}

    def __init__(self, *args, **kwargs):
        wxglade_gui.SpykeFrame.__init__(self, *args, **kwargs)
        self.SetPosition(wx.Point(x=0, y=0)) # upper left corner
        self.dpos = {} # positions of data frames relative to main spyke frame
        self.surffname = ""
        self.sortfname = ""
        self.frames = {} # holds spike, chart, and lfp frames
        self.spiketw = DEFSPIKETW # spike frame temporal window width (us)
        self.charttw = DEFCHARTTW # chart frame temporal window width (us)
        self.lfptw = DEFLFPTW # lfp frame temporal window width (us)
        self.t = None # current time position in recording (us)

        self.Bind(wx.EVT_CLOSE, self.OnExit)
        self.Bind(wx.EVT_MOVE, self.OnMove)

        self.slider.Bind(wx.EVT_SLIDER, self.OnSlider)

        #self.Bind(wx.EVT_KEY_DOWN, self.OnKeyDown)

        self.file_combo_box_units_label.SetLabel(MU+'s') # can't seem to set mu symbol from within wxGlade
        self.fixedthresh_units_label.SetLabel(MU+'V')
        self.range_units_label.SetLabel(MU+'s')
        self.blocksize_units_label.SetLabel(MU+'s')
        self.spatial_units_label.SetLabel(MU+'m')
        self.temporal_units_label.SetLabel(MU+'s')

        # disable most widgets until a .srf file is opened
        self.EnableWidgets(False)

        # TODO: load recent file history and add it to menu (see wxGlade code that uses wx.FileHistory)

        fname = self.DEFAULTDIR + '/87 - track 7c spontaneous craziness.srf'
        #fname = '/home/mspacek/Desktop/Work/spyke/data/large_data.srf'
        self.OpenSurfFile(fname) # have this here just to make testing faster

    def OnNew(self, event):
        # TODO: what should actually go here? just check if an existing collection exists,
        # check if it's saved (if not, prompt to save), and then del it and init a new one?
        wxglade_gui.SpykeFrame.OnNew(self, event)

    def OnOpen(self, event):
        dlg = wx.FileDialog(self, message="Open surf or sort file",
                            defaultDir=self.DEFAULTDIR, defaultFile='',
                            wildcard="All files (*.*)|*.*|Surf files (*.srf)|*.srf|Sort files (*.sort)|*.sort",
                            style=wx.OPEN)
        if dlg.ShowModal() == wx.ID_OK:
            fname = dlg.GetPath()
            self.OpenFile(fname)
        dlg.Destroy()

    def OnSave(self, event):
        if not self.sortfname:
            self.OnSaveAs(event)
        else:
            self.SaveFile(self.sortfname) # save to existing sort fname

    def OnSaveAs(self, event):
        """Save collection to new .sort file"""
        dlg = wx.FileDialog(self, message="Save collection as",
                            defaultDir=self.DEFAULTDIR, defaultFile='',
                            wildcard="Sort files (*.sort)|*.sort|All files (*.*)|*.*",
                            style=wx.SAVE | wx.OVERWRITE_PROMPT)
        if dlg.ShowModal() == wx.ID_OK:
            fname = dlg.GetPath()
            self.SaveFile(fname)
        dlg.Destroy()

    def OnClose(self, event):
        # TODO: add confirmation dialog if collection not saved
        self.CloseSurfFile()

    def OnExit(self, event):
        # TODO: add confirmation dialog if collection not saved
        self.CloseSurfFile()
        self.Destroy()

    def OnAbout(self, event):
        dlg = SpykeAbout(self)
        dlg.ShowModal()
        dlg.Destroy()

    def OnSpike(self, event):
        """Spike window toggle menu/button event"""
        self.ToggleFrame('spike')

    def OnChart(self, event):
        """Chart window toggle menu/button event"""
        self.ToggleFrame('chart')

    def OnLFP(self, event):
        """LFP window toggle menu/button event"""
        self.ToggleFrame('lfp')

    def OnTref(self, event):
        """Time reference toggle menu event"""
        self.ToggleRef('tref')

    def OnVref(self, event):
        """Voltage reference toggle menu event"""
        self.ToggleRef('vref')

    def OnCaret(self, event):
        """Caret toggle menu event"""
        self.ToggleRef('caret')

    def OnMove(self, event):
        """Move frame, and all dataframes as well, like docked windows"""
        for frametype, frame in self.frames.items():
            frame.Move(self.GetPosition() + self.dpos[frametype])
        #event.Skip() # apparently this isn't needed for a move event,
        # I guess the OS moves the frame no matter what you do with the event

    def OnFileComboBox(self, event):
        """Change file position using combo box control,
        convert start, now, and end to appropriate vals"""
        # TODO: I set a value manually, but the OS overrides the value
        # after this handler finishes handling the event, don't know how to
        # prevent its propagation to the OS. ComboBoxEvent is a COMMAND event
        t = self.file_combo_box.GetValue()
        try:
            t = self.str2t[t]
        except KeyError:
            # convert to float first so you can use exp notation as shorthand
            t = float(t)
        self.seek(t)

    def OnSlider(self, event):
        """Strange: keyboard press or page on mouse click when slider in focus generates
        two slider events, and hence two plot events - mouse drag only generates one slider event"""
        self.seek(self.slider.GetValue())
        #print time.time(), 'OnSlider()'
        #event.Skip() # doesn't seem to be necessary

    def OnSearch(self, event):
        """Detect pane Search button click"""
        self.get_detector()
        self.spikes = self.det.search()
        self.total_nspikes_label.SetLabel(str(self.spikes.shape[1]))
        print '%r' % self.spikes

    def OnKeyDown(self, event):
        """Handle key presses"""
        key = event.GetKeyCode()
        #print 'key: %r' % key
        in_widget = event.GetEventObject().ClassName in ['wxComboBox', 'wxSpinCtrl', 'wxSlider']
        in_file_combo_box = event.GetEventObject() == self.file_combo_box
        if not event.ControlDown():
            if key == wx.WXK_LEFT and not in_widget or key == wx.WXK_DOWN and in_file_combo_box:
                    self.seek(self.t - self.hpstream.tres)
            elif key == wx.WXK_RIGHT and not in_widget or key == wx.WXK_UP and in_file_combo_box:
                    self.seek(self.t + self.hpstream.tres)
            elif key == wx.WXK_PRIOR: # PGUP
                self.seek(self.t - self.spiketw)
            elif key == wx.WXK_NEXT: # PGDN
                self.seek(self.t + self.spiketw)
            elif key == wx.WXK_F2: # search for previous spike
                self.findspike(which='previous')
            elif key == wx.WXK_F3: # search for next spike
                self.findspike(which='next')
        else: # CTRL is down
            if key == wx.WXK_PRIOR: # PGUP
                self.seek(self.t - self.charttw)
            elif key == wx.WXK_NEXT: # PGDN
                self.seek(self.t + self.charttw)
        # when key event comes from file_combo_box, reserve down/up for seeking through file
        if in_widget and not in_file_combo_box or in_file_combo_box and key not in [wx.WXK_DOWN, wx.WXK_UP]:
            event.Skip() # pass event on to OS to handle cursor movement

    def OpenFile(self, fname):
        """Open either .srf or .sort file"""
        ext = os.path.splitext(fname)[1]
        if ext == '.srf':
            self.OpenSurfFile(fname)
        elif ext == '.sort':
            self.OpenSortFile(fname)
        else:
            wx.MessageBox("%s is not a .srf or .sort file" % fname,
                          caption="Error", style=wx.OK|wx.ICON_EXCLAMATION)
            return

    def OpenSurfFile(self, fname):
        """Open a .srf file, and update display accordingly"""
        self.CloseSurfFile() # in case a .srf file and frames are already open
        self.surff = surf.File(fname)
        # TODO: parsing progress dialog
        self.surff.parse()
        self.Refresh() # parsing takes long, can block repainting events
        self.surffname = fname # bind it now that it's been successfully opened and parsed
        self.SetTitle(self.Title + ' - ' + self.surffname) # update the caption

        self.hpstream = core.Stream(self.surff.highpassrecords) # highpass record (spike) stream
        self.lpstream = core.Stream(self.surff.lowpassmultichanrecords) # lowpassmultichan record (LFP) stream
        self.chans_enabled = copy(self.hpstream.layout.chanlist) # property
        self.t = intround(self.hpstream.t0 + self.spiketw/2) # set current time position in recording (us)

        self.OpenFrame('spike')
        self.OpenFrame('chart')
        self.OpenFrame('lfp')
        self.ShowRef('tref')
        self.ShowRef('vref')
        self.ShowRef('caret')

        # self has focus, but isn't in foreground after opening data frames
        #self.Raise() # doesn't seem to bring self to foreground
        #wx.GetApp().SetTopWindow(self) # neither does this

        self.str2t = {'start': self.hpstream.t0,
                      'now': self.t,
                      'end': self.hpstream.tend}

        self.range = (self.hpstream.t0, self.hpstream.tend) # us
        self.file_combo_box.SetValue(str(self.t))
        self.file_min_label.SetLabel(str(self.hpstream.t0))
        self.file_max_label.SetLabel(str(self.hpstream.tend))
        self.slider.SetRange(self.range[0], self.range[1])
        self.slider.SetValue(self.t)
        self.slider.SetLineSize(self.hpstream.tres) # us, TODO: this should be based on level of interpolation
        self.slider.SetPageSize(self.spiketw) # us

        self.fixedthresh_spin_ctrl.SetRange(-sys.maxint, sys.maxint)
        self.fixedthresh_spin_ctrl.SetValue(detect.Detector.DEFFIXEDTHRESH)
        self.noisemult_spin_ctrl.SetValue(detect.Detector.DEFNOISEMULT)
        #self.noise_method_choice.SetSelection(0)
        self.nspikes_spin_ctrl.SetRange(0, sys.maxint)
        self.blocksize_combo_box.SetValue(str(detect.Detector.DEFBLOCKSIZE))
        self.slock_spin_ctrl.SetRange(0, sys.maxint)
        self.tlock_spin_ctrl.SetRange(0, sys.maxint)
        self.slock_spin_ctrl.SetValue(detect.Detector.DEFSLOCK)
        self.tlock_spin_ctrl.SetValue(detect.Detector.DEFTLOCK)

        self.get_detector() # bind a Detector to self

        self.EnableWidgets(True)

    def get_chans_enabled(self):
        return [ chan for chan, enable in self._chans_enabled.items() if enable ]

    def set_chans_enabled(self, chans, enable=True):
        if chans == None: # None means all chans
            chans = copy(self.hpstream.layout.chanlist)
        chans = toiter(chans) # need not be contiguous
        try:
            self._chans_enabled
        except AttributeError:
            self._chans_enabled = {}
        for chan in chans:
            self._chans_enabled[chan] = enable
        try:
            self.frames['spike'].panel.enable_chans(chans, enable)
            self.frames['chart'].panel.enable_chans(chans, enable)
        except KeyError:
            pass

    chans_enabled = property(get_chans_enabled, set_chans_enabled)

    def CloseSurfFile(self):
        """Destroy data frames, close .srf file"""
        # need to specifically get a list of keys, not an iterator,
        # since self.frames dict changes size during iteration
        for frametype in self.frames.keys():
            self.CloseFrame(frametype) # deletes from dict
        try:
            self.surff.close()
        except AttributeError:
            pass
        self.t = None
        self.spiketw = DEFSPIKETW # reset
        self.charttw = DEFCHARTTW
        self.lfptw = DEFLFPTW
        self.SetTitle("spyke") # update caption
        self.EnableWidgets(False)

    def OpenSortFile(self, fname):
        """Open a collection from a .sort file"""
        # TODO: do something with data (data is the collection object????)
        try:
            f = file(fname, 'rb')
            data = cPickle.load(f)
            f.close()
            self.sortfname = fname # bind it now that it's been successfully loaded
            self.SetTitle(self.Title + ' - ' + self.sortfname)
        except cPickle.UnpicklingError:
            wx.MessageBox("Couldn't open %s as a sort file" % fname,
                          caption="Error", style=wx.OK|wx.ICON_EXCLAMATION)

    def SaveFile(self, fname):
        """Save collection to a .sort file"""
        if not os.path.splitext(fname)[1]:
            fname = fname + '.sort'
        f = file(fname, 'wb')
        cPickle.dump(self.collection, f)
        f.close()
        self.sortfname = fname # bind it now that it's been successfully saved
        self.SetTitle(self.Title + ' - ' + self.sortfname)

    def OpenFrame(self, frametype):
        """Create and bind a data frame, show it, plot its data"""
        if frametype not in self.frames: # check it doesn't already exist
            if frametype == 'spike':
                ncols = self.hpstream.probe.ncols
                x = self.GetPosition()[0]
                y = self.GetPosition()[1] + self.GetSize()[1]
                frame = SpikeFrame(parent=self, stream=self.hpstream,
                                   tw=self.spiketw,
                                   pos=wx.Point(x, y), size=(ncols*SPIKEFRAMEPIXPERCHAN, SPIKEFRAMEHEIGHT))
            elif frametype == 'chart':
                x = self.GetPosition()[0] + self.frames['spike'].GetSize()[0]
                y = self.GetPosition()[1] + self.GetSize()[1]
                frame = ChartFrame(parent=self, stream=self.hpstream,
                                   tw=self.charttw, cw=self.spiketw,
                                   pos=wx.Point(x, y), size=CHARTFRAMESIZE)
            elif frametype == 'lfp':
                x = self.GetPosition()[0] + self.frames['spike'].GetSize()[0] + self.frames['chart'].GetSize()[0]
                y = self.GetPosition()[1] + self.GetSize()[1]
                frame = LFPFrame(parent=self, stream=self.lpstream,
                                 tw=self.lfptw, cw=self.charttw,
                                 pos=wx.Point(x, y), size=LFPFRAMESIZE)
            self.frames[frametype] = frame
            self.dpos[frametype] = frame.GetPosition() - self.GetPosition()
        self.ShowFrame(frametype)

    def ShowFrame(self, frametype, enable=True):
        """Show/hide a data frame, force menu and toolbar states to correspond"""
        self.frames[frametype].Show(enable)
        self.menubar.Check(self.FRAMETYPE2ID[frametype], enable)
        self.toolbar.ToggleTool(self.FRAMETYPE2ID[frametype], enable)
        if enable:
            self.plot(frametype) # update only the newly shown frame's data, in case self.t changed since it was last visible
        #if enable:
        #    self.Raise() # children wx.MiniFrames are always on top of main spyke frame, self.Raise() doesn't seem to help. Must be an inherent property of wx.MiniFrames, which maybe isn't such a bad idea after all...

    def HideFrame(self, frametype):
        self.ShowFrame(frametype, False)

    def ToggleFrame(self, frametype):
        """Toggle visibility of a data frame"""
        frame = self.frames[frametype]
        self.ShowFrame(frametype, not frame.IsShown())

    def CloseFrame(self, frametype):
        """Hide frame, remove it from frames dict, destroy it"""
        self.HideFrame(frametype)
        frame = self.frames.pop(frametype)
        frame.Destroy()

    def ShowRef(self, ref, enable=True):
        """Show/hide a tref, vref, or the caret. Force menu states to correspond"""
        self.menubar.Check(self.REFTYPE2ID[ref], enable)
        for frame in self.frames.values():
            frame.panel.show_ref(ref, enable=enable)

    def ToggleRef(self, ref):
        """Toggle visibility of a tref, vref, or the caret"""
        enable = self.frames.items()[0] # pick a random frame
        self.ShowRef(ref, self.menubar.IsChecked(self.REFTYPE2ID[ref])) # maybe not safe, but seems to work

    def EnableWidgets(self, enable):
        """Enable/disable all widgets that require an open .srf file"""
        self.menubar.Enable(wx.ID_SPIKEWIN, enable)
        self.menubar.Enable(wx.ID_CHARTWIN, enable)
        self.menubar.Enable(wx.ID_LFPWIN, enable)
        self.menubar.Enable(wx.ID_TREF, enable)
        self.menubar.Enable(wx.ID_VREF, enable)
        self.menubar.Enable(wx.ID_CARET, enable)
        self.menubar.Enable(wx.ID_CARET, enable)
        self.toolbar.EnableTool(wx.ID_SPIKEWIN, enable)
        self.toolbar.EnableTool(wx.ID_CHARTWIN, enable)
        self.toolbar.EnableTool(wx.ID_LFPWIN, enable)
        self.file_control_panel.Show(enable)
        self.notebook.Show(enable)
        self.file_min_label.Show(enable)
        self.file_max_label.Show(enable)

    def get_detector(self):
        """Create a Detector object and bind it to self,
        overwriting any existing one"""
        detectorClass = self.get_detectorclass()
        self.det = detectorClass(stream=self.hpstream)
        self.update_detector()

    def update_detector(self):
        """Update current Detector object attribs from gui"""
        self.det.chans = self.chans_enabled # property
        self.det.fixedthresh = int(self.fixedthresh_spin_ctrl.GetValue())
        self.det.noisemult = int(self.noisemult_spin_ctrl.GetValue())
        #self.det.noisewindow = int(self.noisewindow_spin_ctrl) # not in the gui yet
        self.det.trange = self.get_detectortrange()
        self.det.maxnspikes = int(self.nspikes_spin_ctrl.GetValue()) or self.det.DEFMAXNSPIKES # if 0, use default
        self.det.blocksize = int(self.blocksize_combo_box.GetValue())
        self.det.slock = self.slock_spin_ctrl.GetValue()
        self.det.tlock = self.tlock_spin_ctrl.GetValue()

    def get_detectorclass(self):
        """Figure out which Detector class to use based on algorithm and
        threshmethod radio selections"""
        algorithm = self.algorithm_radio_box.GetStringSelection()
        if self.fixedthresh_radio_btn.GetValue():
            threshmethod = 'FixedThresh'
        elif self.dynamicthresh_radio_btn.GetValue():
            threshmethod = 'DynamicThresh'
        else:
            raise ValueError
        classstr = algorithm + threshmethod
        return eval('detect.'+classstr)

    def get_detectortrange(self):
        """Get detector time range from combo boxes, and convert
        start, now, and end to appropriate vals"""
        tstart = self.range_start_combo_box.GetValue()
        tend = self.range_end_combo_box.GetValue()
        try:
            tstart = self.str2t[tstart]
        except KeyError:
            tstart = int(float(tstart)) # convert to float first so you can use exp notation as shorthand
        try:
            tend = self.str2t[tend]
        except KeyError:
            tend = int(float(tend))
        return tstart, tend

    def findspike(self, which='next'):
        """Find next or previous spike, depending on which direction"""
        self.update_detector()
        self.det.maxnspikes = 1 # override whatever was in nspikes spin edit
        self.det.blocksize = 100000 # smaller blocksize, since we're only looking for 1 spike
        if which == 'next':
            self.det.trange = (self.t+1, self.hpstream.tend)
        elif which == 'previous':
            self.det.trange = (self.t-1, self.hpstream.t0)
        else:
            raise ValueError, which
        spike = self.det.search() # don't bind to self.spikes, don't update total_nspikes_label
        wx.SafeYield(win=self, onlyIfNeeded=True) # allow controls to update
        try: # if a spike was found
            t = spike[0, 0]
            self.seek(t) # seek to it
            print '%r' % spike
        except IndexError: # if not, do nothing
            pass

    def seek(self, offset=0):
        """Seek to position in surf file. offset is time in us"""
        self.oldt = self.t
        self.t = offset
        self.t = intround(self.t / self.hpstream.tres) * self.hpstream.tres # round to nearest (possibly interpolated) sample
        self.t = min(max(self.t, self.range[0]), self.range[1]) # constrain to within .range
        self.str2t['now'] = self.t # update
        # only plot if t has actually changed, though this doesn't seem to improve
        # performance, maybe mpl is already doing something like this?
        if self.t != self.oldt:
            # update controls first so they don't lag
            self.file_combo_box.SetValue(str(self.t)) # update file combo box
            self.slider.SetValue(self.t) # update slider
            wx.SafeYield(win=self, onlyIfNeeded=True) # allow controls to update
            self.plot()

    def tell(self):
        """Return current position in surf file"""
        return self.t

    def plot(self, frametypes=None):
        """Update the contents of all the data frames, or just specific ones.
        Center each data frame on self.t, don't left justify"""
        if frametypes == None: # update all visible frames
            frametypes = self.frames.keys()
        else: # update only specific frames, if visible
            frametypes = toiter(frametypes)
        frametypes = [ frametype for frametype in FRAMEUPDATEORDER if frametype in frametypes ] # reorder
        frames = [ self.frames[frametype] for frametype in frametypes ] # get frames in order
        for frametype, frame in zip(frametypes, frames):
            if frame.IsShown(): # for performance, only update if frame is shown
                if frametype == 'spike':
                    wave = self.hpstream[self.t-self.spiketw/2 : self.t+self.spiketw/2]
                elif frametype == 'chart':
                    wave = self.hpstream[self.t-self.charttw/2 : self.t+self.charttw/2]
                elif frametype == 'lfp':
                    wave = self.lpstream[self.t-self.lfptw/2 : self.t+self.lfptw/2]
                frame.panel.plot(wave, tref=self.t) # plot it


class DataFrame(wx.MiniFrame):
    """Base data frame to hold a custom spyke panel widget.
    Copied and modified from auto-generated wxglade_gui.py code"""

    # no actual maximize button, but allows caption double-click to maximize
    # need SYSTEM_MENU to make close box appear in a TOOL_WINDOW, at least on win32
    STYLE = wx.CAPTION|wx.CLOSE_BOX|wx.MAXIMIZE_BOX|wx.SYSTEM_MENU|wx.RESIZE_BORDER|wx.FRAME_TOOL_WINDOW

    def __init__(self, *args, **kwds):
        kwds["style"] = self.STYLE
        wx.MiniFrame.__init__(self, *args, **kwds)

    def set_properties(self):
        self.SetTitle("data window")
        self.SetSize((160, 24))

    def do_layout(self):
        dataframe_sizer = wx.BoxSizer(wx.HORIZONTAL)
        dataframe_sizer.Add(self.panel, 1, wx.EXPAND, 0)
        self.SetSizer(dataframe_sizer)
        self.Layout()

    def OnClose(self, event):
        frametype = self.__class__.__name__.lower().replace('frame', '') # remove 'Frame' from class name
        self.Parent.HideFrame(frametype)


class SpikeFrame(DataFrame):
    """Frame to hold the custom spike panel widget"""
    def __init__(self, parent=None, stream=None, tw=None, cw=None, *args, **kwds):
        DataFrame.__init__(self, parent, *args, **kwds)
        self.panel = SpikePanel(self, -1, stream=stream, tw=tw, cw=cw)

        self.Bind(wx.EVT_CLOSE, self.OnClose)

        self.set_properties()
        self.do_layout()

    def set_properties(self):
        self.SetTitle("spike window")


class ChartFrame(DataFrame):
    """Frame to hold the custom chart panel widget"""
    def __init__(self, parent=None, stream=None, tw=None, cw=None, *args, **kwds):
        DataFrame.__init__(self, parent, *args, **kwds)
        self.panel = ChartPanel(self, -1, stream=stream, tw=tw, cw=cw)

        self.Bind(wx.EVT_CLOSE, self.OnClose)

        self.set_properties()
        self.do_layout()

    def set_properties(self):
        self.SetTitle("chart window")


class LFPFrame(DataFrame):
    """Frame to hold the custom LFP panel widget"""
    def __init__(self, parent=None, stream=None, tw=None, cw=None, *args, **kwds):
        DataFrame.__init__(self, parent, *args, **kwds)
        self.panel = LFPPanel(self, -1, stream=stream, tw=tw, cw=cw)

        self.Bind(wx.EVT_CLOSE, self.OnClose)

        self.set_properties()
        self.do_layout()

    def set_properties(self):
        self.SetTitle("LFP window")


class SpykeAbout(wx.Dialog):
    text = '''
        <html>
        <body bgcolor="#D4D0C8">
        <center><table bgcolor="#000000" width="100%" cellspacing="0"
        cellpadding="0" border="0">
        <tr>
            <td align="center"><h1><font color="#00FF00">spyke</font></h1></td>
        </tr>
        </table>
        </center>
        <p><b>spyke</b> is a tool for neuronal spike sorting.
        </p>

        <p>Copyright &copy; 2008 Martin Spacek, Reza Lotun</p>
        </body>
        </html>'''

    def __init__(self, parent):
        wx.Dialog.__init__(self, parent, -1, 'About spyke', size=(350, 250))

        html = wx.html.HtmlWindow(self)
        html.SetPage(self.text)
        button = wx.Button(self, wx.ID_OK, "OK")

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(html, 1, wx.EXPAND|wx.ALL, 5)
        sizer.Add(button, 0, wx.ALIGN_CENTER|wx.ALL, 5)

        self.SetSizer(sizer)
        self.Layout()


class SpykeApp(wx.App):
    def OnInit(self, splash=False):
        if splash:
            bmp = wx.Image("res/splash.png").ConvertToBitmap()
            wx.SplashScreen(bmp, wx.SPLASH_CENTRE_ON_SCREEN | wx.SPLASH_TIMEOUT, 1000, None, -1)
            wx.Yield()
        self.spykeframe = SpykeFrame(None)
        self.spykeframe.Show()
        self.SetTopWindow(self.spykeframe)

        # key presses aren't CommandEvents, and don't propagate up the window hierarchy, but
        # if left unhandled, are tested one final time here in the wx.App. Catch unhandled keypresses
        # here and call appropriate methods in the main spyke frame
        self.Bind(wx.EVT_KEY_DOWN, self.spykeframe.OnKeyDown)

        return True


if __name__ == '__main__':
    app = SpykeApp(redirect=False) # output to stderr and stdout
    app.MainLoop()

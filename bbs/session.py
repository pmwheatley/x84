# -*- coding: utf-8 -*-
"""
Session engine for x/84, http://github.com/jquast/x84/
"""
import traceback
import threading
import logging
import struct
import math
import time
import imp
import sys
import os
import io

import bbs.ini
import bbs.exception

#pylint: disable=C0103
#        Invalid name "logger" for type constant (should match
logger = logging.getLogger()
SESSION = None # Singleton
TTYREC_UCOMPRESS = 15000
TTYREC_HEADER = unichr(27) + u'[8;%d;%dt'
TTYREC_ROTATE = 4

def getsession():
    """
    Return session, after a .run() method has been called on any 1 instance.
    """
    assert SESSION is not None, (
            'a Session() instance must be initialized with .run() method')
    return SESSION

def getterminal():
    """
    Return blessings terminal instance of this session.
    """
    return getsession().terminal

class Session(object):
    """
    A BBS Session engine, started by .run().
    """
    #pylint: disable=R0902,R0904
    #        Too many instance attributes (29/7)
    #        Too many public methods (25/20)

    def __init__ (self, terminal, pipe, source, env):
        """
        Instantiate a Session instanance, only one session may be instantiated
        per process. Arguments:
            terminal: blessings.Terminal,
            pipe: multiprocessing.Pipe child end
            source: origin of the connect (ip, port),
            env: dict of environment variables, such as 'TERM', 'USER'.
        """
        #pylint: disable=W0603
        #        Using the global statement
        global SESSION
        assert SESSION is None, 'Session may be instantiated only once'
        SESSION = self
        self.pipe = pipe
        self.terminal = terminal
        self.source = source
        self.env = env
        self.lock = threading.Lock()
        self._user = None
        self._script_stack = [(bbs.ini.CFG.get('matrix','script'),)]
        self._tap_input = bbs.ini.CFG.getboolean('session','tap_input')
        self._tap_output = bbs.ini.CFG.getboolean('session','tap_output')
        self._ttylog_folder = bbs.ini.CFG.get('session', 'ttylog_folder')
        self._record_tty = bbs.ini.CFG.getboolean('session', 'record_tty')
        self._ttyrec_folder = bbs.ini.CFG.get('session', 'ttylog_folder')
        self._script_module = None
        self._fp_ttyrec = None
        self._ttyrec_fname = None
        self._connect_time = time.time()
        self._last_input_time = time.time()
        self._enable_keycodes = True
        self._activity = u'<uninitialized>'
        self._source = '<undefined>'
        self._encoding = 'utf8'
        self._recording = False
        # event buffer
        self._buffer = dict()
        # save state for ttyrec compression
        self._ttyrec_sec = -1
        self._ttyrec_usec = -1
        self._ttyrec_len_text = 0

    @property
    def duration(self):
        """
        Return length of time since connection began (float).
        """
        return time.time() -self._connect_time

    @property
    def connect_time(self):
        """
        Return time when connection began (float).
        """
        return self._connect_time

    @property
    def last_input_time(self):
        """
        Return last time of keypress (epoch float).
        """
        return self._last_input_time

    @property
    def idle(self):
        """
        Return length of time since last keypress occured (float).
        """
        return time.time() - self._last_input_time

    @property
    def activity(self):
        """
        Current activity (arbitrarily set).
        """
        return self._activity

    @activity.setter
    def activity(self, value):
        #pylint: disable=C0111
        #         Missing docstring
        if self._activity != value:
            logger.debug ('activity=%s', value)
            self._activity = value

    @property
    def handle(self):
        """
        Returns User handle.
        """
        return self.user.handle

    @property
    def user(self):
        """
        User record of session.
        """
        if self._user is not None:
            return self._user
        return bbs.userbase.User()

    @user.setter
    def user(self, value):
        #pylint: disable=C0111
        #         Missing docstring
        self._user = value
        logger.info ('user=%r', value.handle)
        if self.is_recording and self._ttyrec_fname != value.handle:
            # mv None.0 -> userName.0
            self.rename_recording (self._ttyrec_fname, value.handle)

    @property
    def source(self):
        """
        A string describing the session source
        """
        return '%s' % (self._source,)

    @source.setter
    def source(self, value):
        #pylint: disable=C0111
        #         Missing docstring
        self._source = value
    @property

    def encoding(self):
        """
        Session terminal encoding; only 'utf8' and 'cp437' are supported.
        """
        return self._encoding

    @encoding.setter
    def encoding(self, value):
        #pylint: disable=C0111
        #         Missing docstring
        if value != self._encoding:
            logger.info ('encoding=%s', value)
            assert value in ('utf8', 'cp437')
            self._encoding = value

    @property
    def enable_keycodes(self):
        """
        Translate multibyte sequences to single keycodes for input events.

        It may be desirable to temporarily disable this when doing
        pass-through, to another curses application such as a door.
        """
        return self._enable_keycodes

    @enable_keycodes.setter
    def enable_keycodes(self, value):
        #pylint: disable=C0111
        #         Missing docstring
        if value != self._enable_keycodes:
            logger.debug ('enable_keycodes=%s', value)
            self._enable_keycodes = value

    @property
    def pid(self):
        """
        Returns Process ID.
        """
        #pylint: disable=R0201
        #        Method could be a function
        return os.getpid()

    def run(self):
        """
        Begin main execution flow.

        Client scripts manipulate control flow of scripts using goto and gosub.
        """
        fallback_stack = self._script_stack
        while len(self._script_stack) > 0:
            logger.debug ('script_stack: %r', self._script_stack)
            try:
                #pylint: disable=W0612
                #        Unused variable 'lastscript'
                #        Unused variable 'value'
                lastscript = self._script_stack[-1]
                value = self.runscript (*self._script_stack.pop())
                if not self._script_stack:
                    logger.error ('_script_stack = <fallback_stack: %r>', \
                        fallback_stack)
                    self._script_stack = fallback_stack
                    continue
            except bbs.exception.Goto, err:
                logger.debug ('Goto: %s', err)
                self._script_stack = [err[0] + tuple(err[1:])]
                continue
            except bbs.exception.Disconnect, err:
                logger.info ('User disconnected: %s', err)
                return
            except bbs.exception.ConnectionClosed, err:
                logger.info ('Connection Closed: %s', err)
                return
            except bbs.exception.ConnectionTimeout, err:
                logger.info ('Connection Timed out: %s', err)
                return
            except bbs.exception.ScriptError, err:
                logger.error ("ScriptError rasied: %s", err)
            except Exception, err:
                # Pokemon exception.
                e_type, e_value, e_tb = sys.exc_info()
                for line in traceback.format_tb(e_tb):
                    logger.error (line.rstrip())
                for line in traceback.format_exception_only(e_type, e_value):
                    logger.error (line.rstrip())

            if 0 != len(self._script_stack):
                # recover from a general exception or script error
                fault = self._script_stack.pop()
                logger.info ('%s after general exception with %s.', 'Resume' \
                    if 0 != len(self._script_stack) else 'Stop', fault)
        self.close ()


    def write (self, unibytes):
        """
        Write unicode data to telnet client and record to ttyrec when
        recording. Always write unicode intended for utf8 decoding.
        """
        if 0 == len(unibytes):
            return
        assert isinstance(unibytes, unicode)
        if self.encoding == 'cp437':
            from bbs.cp437 import CP437
            # first, encode as iso8859-1, this will replace a lot
            # of characters, including artwork, as '?'
            text = unibytes.encode('iso8859-1', 'replace')
            # then, iterate over all the unicode glyphs, mapping
            # back to their bytestring equivalents unless there isn't
            # a mapping, then use that position in text ('?').
            unibytes = u''.join([unichr(CP437.index(glyph)) if glyph in CP437
                else unicode(text[n]) for n, glyph in enumerate(unibytes)])
        self.terminal.stream.write (unibytes,
                is_cp437=(self.encoding == 'cp437'))
        if self._tap_output and logger.isEnabledFor(logging.DEBUG):
            logger.debug ('--> %r.', unibytes)
        if self._record_tty:
            if not self.is_recording:
                self.start_recording ()
            self._ttyrec_write (unibytes)

    def flush_event (self, event, timeout=-1):
        """
        Flush all data buffered for 'event'.
        """
        data = 1
        while None != data:
            data = self.read_event(event, timeout=timeout)
            if data is not None:
                logger.debug ('flushed (%s, %s)', event, data)


    def _event_pop(self, event):
        """
        Return foremost item buffered for event.
        """
        store = self._buffer[event].pop ()
        return store


    def buffer_event (self, event, data=None):
        """
        Push data into buffer keyed by event. Allow only the most recent
        refresh event to be buffered.
        """
        if event == 'ConnectionClosed':
            raise bbs.exception.ConnectionClosed (data)

        if not self._buffer.has_key(event):
            # create new buffer;
            self._buffer[event] = list()

        if event == 'input':
            self.buffer_input (data)
        elif event == 'refresh':
            if data[0] == 'resize':
                # inherit terminal dimensions values
                (self.terminal.columns, self.terminal.rows) = data[1]
            # store only most recent 'refresh' event
            self._buffer[event] = list((data,))
        else:
            self._buffer[event].insert (0, data)

    def buffer_input (self, data):
        """
        Update idle time, buffer input, and when Session.enable_keycodes is
        True, decode multibyte sequences as possible nvt, vt100, telnet, and
        curses input keysequences.

        The unfortunate side-effect is something might appear as an equivalent
        KEY_SEQUENCE that is better described as it was to traditional getch()
        users, '\r' and '\b', for example.
        """
        self._last_input_time = time.time()
        if self._tap_input and logger.isEnabledFor(logging.DEBUG):
            logger.debug ('<-- %r.', data)
        if False == self.enable_keycodes:
            # send keyboard bytes in as-is, unmanipulated
            self._buffer['input'].insert (0, data)
            return
        # translate ^L to KEY_REFRESH in getch() stream, but
        # also send a ('refresh', ('input',)) event for signaling
        ctrl_l = list((('input', self.terminal.KEY_REFRESH),))

        # perform keycode translation with modified blessings/curses
        for keystroke in self.terminal.trans_input(data, self.encoding):
            if keystroke == chr(12): # ^L
                self._buffer['refresh'] = ctrl_l
                self._buffer['input'].insert (0, self.terminal.KEY_REFRESH)
            self._buffer['input'].insert (0, keystroke)

    def send_event (self, event, data):
        """
           Send data to IPC pipe in form of (event, data).

           Supported events:
               'disconnect': Session wishes to disconnect.
               'logger': Data is logging record, used by IPCLogHandler.
               'output': Unicode data to write to client.
               'global': Broadcast event to other sessions.
               XX 'pos': Request cursor position.
               'db-<schema>': Request sqlite dict method result.
               'db=<schema>': Request sqlite dict method result as iterable.
               'lock-<name>': Fine-grained global bbs locking.
        """
        self.pipe.send ((event, data))

    def read_event(self, event, timeout=None):
        """
        Read a single event from the event pipe
        """
        return self.read_events (events=(event,), timeout=timeout)[1]

    def read_events (self, events, timeout=None):
        """
           S.read_events (events, timeout=None) --> (event, data)

           Poll for and return the first matched buffered IPC data for events
           that have arrived in the form of (event, data).

           A timeout of None waits indefinitely, otherwise (None, None) is
           returned when timeout has exceeded. when timeout of -1 is used, this
           call is non-blocking.
        """
        (event, data) = (None, None)
        # return immediately any events that are already buffered
        for (event, data) in ((e, self._event_pop(e)) for e in events \
            if e in self._buffer and 0 != len(self._buffer[e])):
            return (event, data)
        stime = time.time()
        timeleft = lambda cmp_time: \
            float('inf') if timeout is None \
            else timeout - (time.time() -cmp_time)
        waitfor = timeleft(stime)
        while waitfor > 0:
            if self.pipe.poll (None if waitfor == float('inf') else waitfor):
                event, data = self.pipe.recv()
                if event == 'exception':
                    raise getattr(bbs.exception, data[0]), data[1]
                self.buffer_event (event, data)
                if event in events:
                    return (event, self._event_pop(event))
                else:
                    logger.debug ('not seeking event %s; pass.', (event,))
            if timeout == -1:
                return (None, None)
            waitfor = timeleft(stime)
        return (None, None)

    def runscript(self, script_name, *args):
        """
        Execute the main() callable with optional *args of python script.
        """
        self._script_stack.append ((script_name,) + args)
        logger.info ('runscript %s, %s.', script_name, args,)
        def _load_script_module():
            """
            Load and return `scriptpath` as a module (cached).
            It really begs wether this should be called a 'bbs module' ..
            """
            if self._script_module is None:
                # load default/__init__.py as 'default',
                script_path = bbs.ini.CFG.get('session', 'scriptpath')
                base_script = os.path.basename(script_path)
                self._script_module = imp.load_module(base_script,
                        *imp.find_module(script_name, [script_path]))
                self._script_module.__path__ = script_path
            return self._script_module
        script_module = _load_script_module()
        #pylint: disable=W0142
        #        Used * or ** magic
        script = imp.load_module (script_name,
                *imp.find_module (script_name, [script_module.__path__]))
        if not hasattr(script, 'main'):
            raise bbs.exception.ScriptError (
                "%s: main() not found." % (script_name,))
        if not callable(script.main):
            raise bbs.exception.ScriptError (
                "%s: main not callable." % (script_name,))
        value = script.main(*args)
        toss = self._script_stack.pop()
        logger.debug ('%s popped: %s', toss, value)
        return value

    def close(self):
        """
        Close session.
        """
        if self.is_recording:
            self.stop_recording()

# TODO: Somehow move these to ttyrec.py to keep session.py terse;
    def rename_recording(self, src, dst):
        """
        Rotate ttyrec recording keyed by dst to make way for dst.0 by renaming
        src.0 to dst.0; not really sure this is correct ^_*
        """
        # acquire tty recording lock
        self.lock.acquire ()
        while True:
            self.send_event('lock-ttyrec', ('acquire', 5.0))
            if self.read_event('lock-ttyrec'):
                break
            logger.warn ('failed to acquire ttyrec lock')
            time.sleep (0.6)
        self.rotate_recordings (dst)
        os.rename (os.path.join(self._ttylog_folder, '%s.0' % (src,)),
            os.path.join(self._ttylog_folder, '%s.0' % (dst,)))
        # release tty recording lock
        self.send_event('lock-ttyrec', ('release', None))
        self._ttyrec_fname = dst
        self.lock.release ()

    def rotate_recordings(self, key):
        """
        Rotate any existing ttyrec files for key.
        """
        # if .8 exists, move .8 to .9, obliterating .9;
        # if .7 exists, move .7 to .8, obliterating .8; (..repeat)
        # aren't there helper functions for this stuff? :p
        for n in range(TTYREC_ROTATE):
            src = os.path.join(self._ttylog_folder,
                    '%s.%d' % (key, (TTYREC_ROTATE - 1) - (n)))
            dst = os.path.join(self._ttylog_folder,
                    '%s.%d' % (key, (TTYREC_ROTATE - 1) - (n - 1)))
            if os.path.exists(src):
                os.rename (src, dst)
                logger.debug ('rotate ttyrec %r --> %r', src, dst)
        dst = os.path.join(self._ttylog_folder, '%s.0' % (key,))
        assert TTYREC_ROTATE != 0 and not os.path.exists(dst), dst

    @property
    def is_recording(self):
        """
        True when session is being recorded to ttyrec file
        """
        return self._fp_ttyrec is not None

    def stop_recording(self):
        """
        Cease recording to ttyrec file (close).
        """
        assert self.is_recording
        self._fp_ttyrec.close ()
        self._fp_ttyrec = None

    def start_recording(self, dst=None):
        """
        Begin recording to ttyrec file keyed by 'dst'. When 'dst' is None
        (default), use the handle of the current session.
        """
        assert self._fp_ttyrec is None, ('already recording')
        if dst is None:
            dst = self.user.handle or 'None'
        self._ttyrec_fname = dst
        # acquire tty recording lock
        self.lock.acquire ()
        while True:
            self.send_event('lock-ttyrec', ('acquire', 5.0))
            if self.read_event('lock-ttyrec'):
                break
            logger.warn ('failed to acquire ttyrec lock')
            time.sleep (0.6)
        # rotate logfiles,
        self.rotate_recordings (self._ttyrec_fname)
        # open ttyrec logfile for writing
        filename = os.path.join(self._ttyrec_folder, '%s.0'
                % (self._ttyrec_fname,))
        if not os.path.exists(self._ttyrec_folder):
            logger.info('creating ttyrec folder, %s.', self._ttyrec_folder)
            os.makedirs (self._ttyrec_folder)
        self._fp_ttyrec = io.open(filename, 'wb+')
        self._ttyrec_sec = -1
        self._recording = True
        self._ttyrec_write_header ()
        logger.info ('REC %s' % (filename,))
        # release tty recording lock
        self.send_event('lock-ttyrec', ('release', None))
        self.lock.release ()

    def _ttyrec_write_header(self):
        """
        Write ttyrec header that identifies termianl height & width, and escape
        sequence to indicate UTF-8 mode.
        """
        (h, w) = self.terminal.height, self.terminal.width
        self._ttyrec_write (TTYREC_HEADER % (h, w,))
        # ESC %G activates UTF-8 with an unspecified implementation level from
        # ISO 2022 in a way that allows to go back to ISO 2022 again.
        self._ttyrec_write (unichr(27) + u'%G')

    def _ttyrec_write(self, unibytes):
        """
        Update ttyrec stream
        """
        # write bytestring to ttyrec file packed as timed byte.
        # If the current timed byte is within TTYREC_UCOMPRESS (default: 15,000
        # μsec), rewind stream and re-write the 'length' portion, and append
        # data to end of stream.
        assert self._recording, 'call start_recording() first'
        assert isinstance(unibytes, unicode), 'unibytes is not unicode'
        timeKey = self.duration

        # Round down timeKey to nearest whole number,
        # use the remainder for microseconds. Upconvert,
        # constructing a (seconds, microseconds) pair.
        sec = math.floor(timeKey)
        usec = (timeKey -sec) * 1e+6
        sec, usec = int(sec), int(usec)

        def write_chunk(tm_sec, tm_usec, textlen, u_text):
            """
            Write new timechunk record,
              bytes (sec, usec, len(text), text.. )
            """
            # build & write,
            bp1 = struct.pack('<I', tm_sec) + struct.pack('<I', tm_usec)
            bp2 = struct.pack('<I', textlen)
            self._fp_ttyrec.write (bp1 + bp2 + u_text)
            # save (time,len) state for compression
            self._ttyrec_sec = tm_sec
            self._ttyrec_usec = tm_usec
            self._ttyrec_len_text = textlen
            self._fp_ttyrec.flush ()
        text = unibytes.encode('utf8', 'replace')
        len_text = len(text)
        if (sec != self._ttyrec_sec
                or usec - self._ttyrec_usec > TTYREC_UCOMPRESS):
            while sec > 10:
                # when more than 10s have elapsed, padd with intervals anyway.
                write_chunk (sec, usec, 0, bytes())
                sec -= 10
            return write_chunk (sec, usec, len_text, text)
        # a sort of monkey patching (compression);
        #   1. rewind to last length byte
        last_bp2 = struct.pack('<I', self._ttyrec_len_text)
        new_bp2 = struct.pack('<I', self._ttyrec_len_text +len_text)
        self._fp_ttyrec.seek ((self._ttyrec_len_text +len(last_bp2)) *-1, 2)
        #   2. re-write length byte
        self._fp_ttyrec.write (new_bp2)
        #   3. append additional text after existing chunk record
        self._fp_ttyrec.seek (self._ttyrec_len_text, 1)
        self._fp_ttyrec.write (text)
        self._ttyrec_len_text = self._ttyrec_len_text + len_text
        self._fp_ttyrec.flush ()

class IPCLogHandler(logging.Handler):
    """
    Log handler that sends the log up the 'event pipe'. This is a rather novel
    solution that seems overlooked in documentation or exisiting code, try it!
    """
    def __init__(self, pipe):
        logging.Handler.__init__(self)
        self.pipe = pipe
    def emit(self, record):
        """
        emit log record via IPC pipe
        """
        try:
            e_inf = record.exc_info
            if e_inf:
                # side-effect: sets record.exc_text
                dummy = self.format(record)
                record.exc_info = None
            self.pipe.send (('logger', record))
        except (KeyboardInterrupt, SystemExit):
            raise
        except:
            self.handleError (record)

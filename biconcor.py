#!/usr/bin/env python

"""
$Id: $
"""

#----------------------------------------------------------------------------------------------------------------------------------
# includes

import datetime
import pprint
import re
import os
import subprocess
import threading
import time

#----------------------------------------------------------------------------------------------------------------------------------
# constants

MAX_TRANSLATIONS = 10
MAX_EXAMPLES_PER_TRANS = 8

#----------------------------------------------------------------------------------------------------------------------------------

class KillerThread (threading.Thread):
    """
    Takes a child process as argument, and kills it if a delay longer than INACTIVE_TIMEOUT passes without `record_activity' being
    called.
    """

    # When this long has passed since the last activity was recorded, the child is killed
    INACTIVE_TIMEOUT = datetime.timedelta (hours=24)

    def __init__ (self, child_proc):
        super(KillerThread,self).__init__ ()
        self.child_proc = child_proc
        self.last_activity = datetime.datetime.now()
        self.daemon = True
        self.aborted = False

    def record_activity (self):
        self.last_activity = datetime.datetime.now()

    def abort (self):
        self.aborted = True

    def run (self):
        while not self.aborted:
            time.sleep (60)
            last_activity = self.last_activity
            if last_activity is not None:
                now = datetime.datetime.now()
                elapsed = now - last_activity
                if elapsed > self.INACTIVE_TIMEOUT:
                    print "%s since last activity, killing biconcor binary" % self.INACTIVE_TIMEOUT
                    self.child_proc.terminate()
                    self.child_proc.wait()
                    break
            

#----------------------------------------------------------------------------------------------------------------------------------

class BiconcorProcess (object):
    """
    Convenience object that encapsulates all interaction with the biconcor binaries.
    """

    BICONCOR_CMDS = {
        'en-de': [
            '/disk7/pkoehn/catserver/biconcor/biconcor',
            '--load', '/disk2/cat-models/wmt13-en-de/biconcor',
            '--stdio',
            ],
        }

    def __init__ (self, lang_pair):
        try:
            self.cmd = self.BICONCOR_CMDS[lang_pair] + [
                '--translations', str(MAX_TRANSLATIONS),
                '--examples', str(MAX_EXAMPLES_PER_TRANS),
                ]
        except KeyError:
            raise ValueError, "Concordancer is not configured for language pair %r" % lang_pair
        self.child = None
        self.killer = None
        self.child_lock = threading.Lock()

    def is_warm (self):
        """ The object is 'warm' when the binary is running, loaded, and ready to accept requests. """
        if self.child is not None:
            assert self.killer is not None, repr(self.killer)
            child_is_running = self.child.poll() is None
            if not child_is_running:
                self.killer.abort()
                self.child = self.killer = None
        return self.child is not None

    def warm_up (self):
        """ Blocks until we have a biconcor process running and ready to accept requests """
        if not self.is_warm():
            assert self.child is None, repr(self.child)
            assert self.killer is None, repr(self.killer)
            self.child = subprocess.Popen (
                self.cmd,
                stdin = subprocess.PIPE,
                stdout = subprocess.PIPE,
                preexec_fn = lambda: os.nice(10),
                )
            expect (self.child.stdout, '-|||- BICONCOR START -|||-')
            self.killer = KillerThread (self.child)
            self.killer.start()

    def get_concordance (self, src_phrase):
        """
        Returns the raw biconcor output for the given source phrase. Output is returned as a list of strings, one per line.
        """
        self.warm_up()
        with self.child_lock:
            self.killer.record_activity()
            print >> self.child.stdin, src_phrase.encode ('UTF-8')
            self.child.stdin.flush()
            output = expect (self.child.stdout, '-|||- BICONCOR END -|||-')
            self.killer.record_activity()
            return output


#----------------------------------------------------------------------------------------------------------------------------------

# these must be considered one token by the detokenizer
BICONCOR_PHRASE_STARTS_HERE = 'BICONCORPHRASESTARTSHERE'
BICONCOR_PHRASE_STOPS_HERE = 'BICONCORPHRASESTOPSHERE'

def parse_biconcor_output_into_json_struct (raw_output, detokenize_and_postprocess):
    """
    Takes the raw output of the biconcor binary, as a list of strings (one per line), and returns plain data structures to be
    serialized to JSON
    """

    iter_raw_output_lines = iter (raw_output)
    re_cover (r'TOTAL: (\d+)', next(iter_raw_output_lines))
    ret_struct = []

    while True:

        try:
            header_line = next (iter_raw_output_lines)
        except StopIteration:
            break # means the binary is inconsistent in how many translations it announces vs. outputs
        if header_line == '-|||- BICONCOR END -|||-':
            break
        tgt_phrase,sent_pair_count = re_cover (r'(.+?)\((\d+)\)', header_line)
        sent_pair_count = int (sent_pair_count)

        sent_pair_structs = []
        tgt_phrase_struct = {
            'tgt_phrase': tgt_phrase,
            'sent_pairs': sent_pair_structs,
            }
        ret_struct.append (tgt_phrase_struct)

        for i in xrange(min(sent_pair_count,MAX_EXAMPLES_PER_TRANS)):

            try:
                raw_line = next (iter_raw_output_lines)
            except StopIteration:
                break # means the binary is inconsistent in how many translations it announces vs. outputs

            if u'\uFFFD' in raw_line:
                # Sometimes biconcor spits out data with unknown chars, presumably because there were encoding problems in the
                # training data. We just skip those, as they are unsightly
                continue

            # >>> src_tokens = [ "it", "was", "a", "bright", "cold", "day" ]
            src_sent,tgt_sent,src_phrase_pos,tgt_phrase_pos,alignment = raw_line.split (' ||| ')
            src_tokens,tgt_tokens = (s.split() for s in (src_sent,tgt_sent))

            # >>> src_phrase_pos = [ 3, 3 ]
            src_phrase_pos,tgt_phrase_pos = (
                map (int, p.split())
                for p in (src_phrase_pos,tgt_phrase_pos)
                )

            # Detokenize the text, keeping track of the position of the phrases to be highlighted (the positions are given as token
            # indices). In the output struct the phrases will be identified in the string with <concord>...</concord> tags
            pair_struct = []
            for tokens,pos in ((src_tokens,src_phrase_pos),(tgt_tokens,tgt_phrase_pos)):
                tokens.insert (pos[1]+1, BICONCOR_PHRASE_STOPS_HERE)
                tokens.insert (pos[0], BICONCOR_PHRASE_STARTS_HERE)
                sent_str = detokenize_and_postprocess (tokens)
                sent_str = re.sub (BICONCOR_PHRASE_STARTS_HERE + r'\s*', '<concord>', sent_str)
                sent_str = re.sub (r'\s*' + BICONCOR_PHRASE_STOPS_HERE, '</concord>', sent_str)
                pair_struct.append (sent_str)

            sent_pair_structs.append (pair_struct)

    return ret_struct


#----------------------------------------------------------------------------------------------------------------------------------
# utils

def expect (fh, expected, encoding='UTF-8', do_rstrip=True):
    """
    Reads lines from `fh', saving them all into a list, until one contains the string in `expected', at which point the accumulated
    line buffer is returned. Also performs decoding if `encoding' is not None.
    """
    ret = []
    while True:
        line = fh.readline()
        if not line:
            break
        else:
            if encoding:
                line = line.decode (encoding)
            if do_rstrip:
                line = line.rstrip() # remove EOL chars
            ret.append (line)
            if expected in line:
                break
    return ret

def re_cover (regex, text, flags=0):
    """ Ensures that the given regex matches the given text from start to end, and returns the captured groups, as a list """
    if isinstance (regex, basestring):
        regex = re.compile (regex, flags=flags)
    match = regex.match (text)
    if not match or match.end() != len(text):
        print (match and match.end(), len(text))
        raise ValueError, "%r does not match %r" % (text, regex.pattern)
    return match.groups()


#----------------------------------------------------------------------------------------------------------------------------------
# cmd line interface for debugging

def main ():
    biconcor = BiconcorProcess ('en-de')
    while True:
        try:
            src_phrase = raw_input ('Enter a phrase to query, or Ctrl-D to quit> ')
        except EOFError:
            break
        src_phrase = src_phrase.decode ('UTF-8')
        if not biconcor.is_warm():
            print "Biconcor is warming up, hang in there..."
        output = biconcor.get_concordance (src_phrase)
        json_struct = parse_biconcor_output_into_json_struct (
            output,
            detokenize_and_postprocess = lambda tokens: ' '.join(tokens).upper(),
            )
        pprint.pprint (json_struct)
    print "All done"

if __name__ == '__main__':
    main()

#----------------------------------------------------------------------------------------------------------------------------------

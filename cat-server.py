#! /usr/bin/env python
# -*- coding: utf-8 -*-

### includes ###

import cStringIO
import collections
import copy
import datetime
import functools
import hashlib
import logging
import os
import re
import subprocess
import sys
import time
import traceback
import urllib
import urllib2

try:
  import simplejson as json
except ImportError:
  import json

try:
  from tornado import web
except:
  print >> sys.stderr, """This software requires Tornado. Please, install the python-tornado package from your distribution."""

try:
  from tornadio2 import SocketConnection, TornadioRouter, SocketServer, event
except:
  print >> sys.stderr, """This software requires Tornadio2. Please, install Tornadio2 from here: https://github.com/mrjoes/tornadio2"""

from biconcor import BiconcorProcess, parse_biconcor_output_into_json_struct



### global vars ###


# root directory of the server
ROOT = os.path.normpath(os.path.dirname(__file__))


# BiconcorProcess objects, indexed by the language pair
biconcor_processes = {}


### generic utils ###

def cat_event (func):
  """ Use this in place of tornadio's `event'. Adds some debugging utils to the function """
  @functools.wraps (func)
  def wrapper (self, *args, **kwargs):
    try:
      print
      print "%s(%s)" % (
        func.__name__,
        ', '.join (map(repr,args) + ['%s=%r' % i for i in sorted(kwargs.iteritems())])
        )
      return func (self, *args, **kwargs)
    except Exception:
      # herve - if we raise an exception here, the whole websocket gets disconnected. So rather than do that, we print out the
      # error, and silence the exception. The client won't see a response, but at least they also won't get disconnected.
      print traceback.format_exc()
  return event (wrapper)


def toutf8(string):
  """ strings in python are unicode. We need to convert strings to uft8 before """
  return string.encode('utf-8')


class MRUDict (collections.MutableMapping):
    """ Container class that acts as a dictionary but only remembers the K items that were last accessed """

    def __init__ (self, max_size, items=()):
        self.max_size = int (max_size)
        self.impl = collections.OrderedDict ()
        if isinstance (items, dict):
            items = items.iteritems()
        for key,value in items:
            self[key] = value

    def __len__ (self):
        return len(self.impl)
    def __iter__ (self):
        return iter (self.impl)
    def __delitem__ (self, key):
        del self.impl[key]

    def __contains__ (self, key):
        if key in self.impl:
            # re-insert the item so that it is now the MRU item
            val = self.impl.pop (key)
            self.impl[key] = val
            return True
        else:
            return False

    def __getitem__ (self, key):
        # re-insert the item so that it is now the MRU item
        val = self.impl.pop (key)
        self.impl[key] = val
        return val

    def __setitem__ (self, key, val):
        while len(self.impl) >= self.max_size:
            # delete the LRU item
            self.impl.popitem (last=False)
        self.impl[key] = val


### Cached searchgraphs (index: source sentence. Remember to include language pair later)
searchGraph = MRUDict (1000)

### connection to server.py ###

# should this be per-connection?
server_py_cache = MRUDict (1000)

def request_to_server_py (text, action='translate', use_cache=False, target=''):
  port = 7754  # server.py

  if isinstance (text, unicode):
    text = text.encode ('UTF-8')
  if isinstance (target, unicode):
    target = target.encode ('UTF-8')

  # herve - several operations crash in server.py if there are spaces around the string. I'm not sure where to strip them -- here
  # in cat-server.py, or in the GUI itself. Trimming off whitespace in the GUI might solve the bug "at the root", but as far as I
  # know it might sometimes be required to preserve whitespace. So I've added this here.
  text = text and text.strip()
  target = target and target.strip()

  params = '' # additional parameters
  if action == 'translate':
    params = '&key=0&source=xx&target=xx&sg=true' # sg=true to return searchgraph
  elif action == 'align' or action == 'tokenize':
    params = '&t=%s' % urllib.quote_plus(target)

  url = 'http://127.0.0.1:%d/%s?%s' % (
    port,
    action,
    'q=%s' % urllib.quote_plus(text) + params,
    )
  logging.debug(url)

  missing = object()
  output_struct = missing
  if use_cache:
    from_cache = server_py_cache.get (url)
    if from_cache is not None:
      print "%s [cached]" % url
      output_struct = from_cache

  if output_struct is missing:
    print url
    req = urllib2.Request (url, '', {'Content-Type': 'application/json'})

    try:
      f = urllib2.urlopen (url)
    except urllib2.HTTPError, err:
      f = err
    json_str = f.read()
    f.close()

    try:
      output_struct = json.loads (json_str)
    except Exception:
      print "Can't parse JSON: %r" % json_str
      raise
    if use_cache:
      server_py_cache[url] = output_struct

  if output_struct.get('traceback'):
    print re.sub (r'^', 'server.py: ', output_struct['traceback'], flags=re.M)
  return copy.deepcopy (output_struct)


def request_translation_and_searchgraph(source, returnTranslation = True):

    translation = request_to_server_py (source, use_cache=True)
    logging.debug('translation')
    logging.debug(translation)
    target = translation[u'data'][u'translations'][0][u'translatedText']

    srcSpans = fix_span_mismatches(translation[u'data'][u'translations'][0][u'tokenization'][u'src']) #translation[u'data'][u'translations'][0][u'tokenization'][u'src'] #
    tgtSpans = fix_span_mismatches(translation[u'data'][u'translations'][0][u'tokenization'][u'tgt']) #translation[u'data'][u'translations'][0][u'tokenization'][u'tgt'] #fix_span_mismatches(translation[u'data'][u'translations'][0][u'tokenization'][u'tgt'])

    sg = translation[u'data'][u'translations'][0][u'searchGraph']

    sgId = hashlib.sha224(toutf8(source)).hexdigest() # unique searchgraph id generated by source

    output = cStringIO.StringIO()
    firstLine = True
    for row in sg:
        if firstLine:
            output.write("hyp,stack,back,score,transition,recombined,forward,fscore,covered-start,covered-end,out\n")
            output.write(str(row["hyp"])+','+str(row["stack"])+',0,0,-1,'+ str(int(row["forward"]))+','+str(row["fscore"])+'\n')
            firstLine = False
        else:
            try:
                output.write(str(row["hyp"])+','+str(row["stack"])+','+str(row["back"])+','+str(row["score"])+','+str(row["transition"])+','+str(row["recombined"])+','+str(int(row["forward"]))+','+str(row["fscore"])+','+ str(row["cover-start"])+','+str(row["cover-end"])+',"'+toutf8(row["out"])+'"\n')
            except:   # if no 'recombined' in line
                output.write(str(row["hyp"])+','+str(row["stack"])+','+str(row["back"])+','+str(row["score"])+','+str(row["transition"])+',-1,'+ str(int(row["forward"]))+','+str(row["fscore"])+','+str(row["cover-start"])+','+str(row["cover-end"])+',"'+toutf8(row["out"])+'"\n')
    output.write("ENDSG\n")
    searchGraph[sgId] = output.getvalue()

    output.close()
    if returnTranslation:
       # needs to have >1 translations
        res = { 'errors' : [],
    		  'data': { 'source': source, 'sourceSegmentation' : srcSpans,
                            'nbest': ( { 'target': target , 'targetSegmentation': tgtSpans } ,
                                       { 'target': target , 'targetSegmentation': tgtSpans }
                                     )
                            } }
        return res

# mismatch with span specifications, maybe should be changed in UI
def fix_span_mismatches(spans):
    for i in range(0, len(spans)):
        if spans[i][1] is not None:
          spans[i][1] += 1
        elif i > 0:
          spans[i] = [ spans[i-1][1], spans[i-1][1]+1 ]
        else:
          spans[i] = [0,0]
    return spans


# This class will handle our client/server API. Each function we would like to
# exports needs to be decorated with the @cat_event decorator (see example below).

class MinimalConnection(SocketConnection):

    def emit (self, *args, **kwargs):
      """ Print out everything we emit to stdout for debugging. This method can be commented out to disable this. """
      print "emit(%s)" % ', '.join (
        ['%r' % a for a in args] +
        ['%s=%r' % i for i in kwargs.iteritems()]
        )
      return super(MinimalConnection,self).emit (*args, **kwargs)

    # @cat_event is a decorator that exports the function to be used with the
    # socket.io javascript client.
    @cat_event
    # the on_open event is called when a socket.io connection  is opened.
    # This is the place to initialize session variables
    def on_open(self, info):
      print
      print '-' * 79
      print "%s: new connection from %s" % (datetime.datetime.now(), info.ip)
      print
      self.config = { 'enabled': True }

    @cat_event
    # the on_close event is called when a socket.io connection is closed.
    # This is the place to delete session variables
    def on_close(self):
      del self.config

    @cat_event
    def ping(self, data):
      res = { 'data': data }
      self.emit('pingResult', res)

    @cat_event
    def getServerConfig(self):
      res = { 'data' : 0 }
      self.emit('getServerConfigResult', res)

    @cat_event
    def configure(self, data):
      print "configure not implemented"

    @cat_event
    def decode(self, data):
      res = request_translation_and_searchgraph(toutf8(data[u'source']))
      self.emit('decodeResult', res)

    @cat_event
    def startSession(self, data):
      res = { 'errors' : [],
              'data': [] }
      self.emit('startSessionResult', res)

    @cat_event
    def rejectSuffix(self, data):
      print "rejectSuffix not implemented"


    @cat_event
    def setPrefix(self, data):
      start_time = time.time()
      errors = []
      source = toutf8(data[u'source'])
      target = toutf8(data[u'target'])
      caretPos = data[u'caretPos']
      prefix = target[0:caretPos]

      try:
        # tokenize prefix (change of var name to "userInput" because "prefix" needs to be returned to the client)
        pProcess  = request_to_server_py(prefix, action='tokenize', use_cache=True)
        userInput = pProcess[u'data'][u'translations'][0][u'tokenizedText']
        #truecase
        pProcess  = request_to_server_py(toutf8(userInput), action='truecase', use_cache=True)
        userInput = pProcess[u'data'][u'translations'][0][u'truecasedText']
        userInput = toutf8(userInput)
      except:
        userInput = prefix

      sgId = hashlib.sha224(source).hexdigest()
      if searchGraph.get(sgId) is None:
        logging.debug('request searchgraph')
        request_translation_and_searchgraph(source, returnTranslation = False)

      logging.debug("calling prediction binary")
      prediction = ''
      p = subprocess.Popen('./predict', stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn = lambda: os.nice(10),)

      p.stdin.write(searchGraph[sgId])
      p.stdin.flush()
      p.stdin.write(userInput+'\n')
      p.stdin.flush()
      # timeout?
      prediction = p.stdout.readline()
      p.kill()

      if prediction:

          # HACK - herve - not sure what happens here, but when the prefix is cut mid-word, sometimes words are repeated here. So
          # we try to mitigate that, although I think the proper solution would have to be implemented in the "predict" binary.
          prediction = prediction.lstrip()
          print "prefix: %r prediction: %r" % (prefix, prediction)
          for lookback in reversed (xrange(100)):
            if lookback <= len(prefix) \
                  and lookback <= len(prediction) \
                  and prefix[-lookback:].lower() == prediction[:lookback].lower():
                prediction = prediction[lookback:]
                break

          # add prefix to ensure correct tokenization (esp. of opening/closing quotes).
          prediction = prefix + prediction
          #postprocessing
          pProcess   = request_to_server_py(prediction, 'detokenize', use_cache=True)
          prediction = pProcess[u'data'][u'translations'][0][u'detokenizedText']

          pProcess   = request_to_server_py(toutf8(prediction), 'detruecase', use_cache=True)
          prediction = pProcess[u'data'][u'translations'][0][u'detruecasedText']
          prediction = toutf8(prediction)

      # call server and get relevant information from reponse
      response = request_to_server_py(source, action='tokenize', target=prediction, use_cache=True)
      srcSpans = fix_span_mismatches(response[u'data'][u'tokenization'][u'src'])
      tgtSpans = fix_span_mismatches(response[u'data'][u'tokenization'][u'tgt'])

      elapsed_time = time.time() - start_time
      res = { 'errors': errors,
              'data': {
                    'caretPos': caretPos,
                    'elapsedTime': elapsed_time,
                    'source': source ,
                    'sourceSegmentation' : srcSpans,
                    'nbest': [ { 'target': prediction, 'elapsedTime': elapsed_time, 'author': 'ITP' , 'targetSegmentation': tgtSpans }
                             ]
                      } }
      self.emit('setPrefixResult', res)


    @cat_event
    def Validate(self,data):
      print "Validate not implemented"


    @cat_event
    def getAlignments(self, data):

      # requires source and target text
      source = toutf8(data[u'source'])
      target = toutf8(data[u'target'])

      # call server and get relevant information from reponse
      response = request_to_server_py(source, action='align', target=target, use_cache=True)
      if response.get ('data'):
        srcSpans = fix_span_mismatches(response[u'data'][u'tokenization'][u'src'])
        tgtSpans = fix_span_mismatches(response[u'data'][u'tokenization'][u'tgt'])
        alignmentPoints = response[u'data'][u'alignment']
      else:
        srcSpans = []
        tgtSpans = []
        alignmentPoints = []

      # process alignment points into matrix
      print "alignmentPoints ", alignmentPoints
      alignmentMatrix = [[0 for i in range(len(tgtSpans))] for j in range(len(srcSpans))]
      for point in alignmentPoints:
        alignmentMatrix[ point[0] ][ point[1] ] = 1

      errors = []
      res = { 'errors': errors,
              'data': {
          'source': source,
          'sourceSegmentation': srcSpans,
          'target': target,
          'targetSegmentation': tgtSpans,
          'alignments': alignmentMatrix
          } }
      self.emit('getAlignmentsResult', res)


    @cat_event
    def getTokens(self, data):

      # requires source and target text
      source = toutf8(data[u'source'])
      target = toutf8(data[u'target'])

      # call server and get relevant information from reponse
      response = request_to_server_py(source, action='tokenize', target=target, use_cache=True)
      srcSpans = fix_span_mismatches(response[u'data'][u'tokenization'][u'src'])
      tgtSpans = fix_span_mismatches(response[u'data'][u'tokenization'][u'tgt'])

      errors = []
      res = { 'errors': errors,
              'data': {
                    'source': source,
                    'sourceSegmentation' : srcSpans,
                    'target': target,
		    'targetSegmentation': tgtSpans
                    } }
      self.emit('getTokensResult', res)

    @cat_event
    def getConfidences(self, data):
      print "getConfidences not implemented"

    @cat_event
    def setReplacementRule(self, data):
      print "setReplacementRule not implemented"

    @cat_event
    def getValidatedContributions(self, data):
      print "getValidatedContributions not implemented"

    @cat_event
    def biconcor (self, data):
      lang_pair = 'en-de' # '%s-%s' % (data['srcLang'], data['tgtLang'])
      src_phrase = data['srcPhrase']
      biconcor_proc = biconcor_processes.get (lang_pair)
      if biconcor_proc is None:
        biconcor_proc = biconcor_processes[lang_pair] = BiconcorProcess (lang_pair)
      if not biconcor_proc.is_warm():
        self.emit ('biconcorResult', {
            'errors': [],
            'data': {
              'warm': False,
              'srcPhrase': src_phrase,
              }
            })
      concor_struct = parse_biconcor_output_into_json_struct (
        biconcor_proc.get_concordance (src_phrase),
        detokenize_and_postprocess = lambda tokens: \
          request_to_server_py (' '.join(tokens), action='detokenize', use_cache=True) \
          ['data']['translations'][0]['detokenizedText'],
        )
      self.emit ('biconcorResult', {
          'errors': [],
          'data': {
            'warm': True,
            'srcPhrase': src_phrase,
            'concorStruct': concor_struct,
            }
          })


# We setup our connection handler. You can define different endpoints
# for additional socket.io services.
class RouterConnection(SocketConnection):
    __endpoints__ = {
                      '/cat': MinimalConnection
                    }

    def on_open(self, info):
      pass


# Create tornadio router
MinimalRouter = TornadioRouter(RouterConnection)



### cmd-line parsing and server init ###

if __name__ == "__main__":

    if len(sys.argv) == 1:
      port = 7666
    elif len(sys.argv) == 2:
      port = int(sys.argv[1])
    else:
      print >> sys.stderr, "usage: %s [port]" % sys.argv[0]
      sys.exit(1)

    LOG_FILENAME = '%s.catserver.log' %datetime.datetime.now().strftime("%Y%m%d-%H.%M.%S")
    logformat = '%(asctime)s %(thread)d - %(filename)s:%(lineno)s: %(message)s'
    logging.basicConfig(filename=LOG_FILENAME,level=logging.DEBUG,format=logformat)

    application = web.Application(
        MinimalRouter.apply_routes([]),
        socket_io_port = port
    )
    SocketServer(application)

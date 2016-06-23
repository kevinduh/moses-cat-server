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
import signal
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

sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 0)

### global vars ###

# root directory of the server
ROOT = os.path.normpath(os.path.dirname(__file__))

# BiconcorProcess objects, indexed by the language pair
biconcor_processes = {}
biconcor_model = None
biconcor_cmd = None

# Defaults for MT Server
mt_port = 9000
mt_host = 'localhost'
log_dir = '.'

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


class Alarm(Exception):
    pass

def alarm_handler(signum, frame):
    """ used for timeout when calling subprocess """
    raise Alarm

def toutf8(string):
  """ strings in python are unicode. We need to convert them to uft8 """
  if not isinstance (string, basestring):
    string = unicode (string)
  if isinstance (string, unicode):
    string = string.encode('utf-8')
  assert isinstance(string,str), repr(string)
  return string

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
translationOptions = MRUDict(1000)
predictProcess = MRUDict(1000)

### connection to server.py ###

# should this be per-connection?
server_py_cache = MRUDict (1000)

def request_to_server_py (text, action='translate', use_cache=False, target=''):

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
    params = '&key=0&source=xx&target=xx&sg=true&topt=true' # sg=true to return searchgraph; topt=true to return translation options
  elif action == 'align' or action == 'tokenize' or action == 'confidence':
    params = '&t=%s' % urllib.quote_plus(target)
    if action == 'align':
      params = params + '&mode=sym'
  elif action == 'update':
    params = "&t=%s&source=xx&target=xx" % urllib.quote_plus(target)

  url = 'http://%s:%d/%s?%s' % (
    mt_host,
    mt_port,
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
      try:
	dummy = output_struct[u'data']
        server_py_cache[url] = output_struct
      except:
        print "nope, not storing in cache, data faulty"

  if output_struct.get('traceback'):
    print re.sub (r'^', 'server.py: ', output_struct['traceback'], flags=re.M)
  return copy.deepcopy (output_struct)


def request_translation_and_searchgraph(source, returnTranslation = True, returnOptions = True):
    translation = request_to_server_py (source, use_cache=True)
    logging.debug('translation')
    logging.debug(translation)
    target = translation[u'data'][u'translations'][0][u'translatedText']

    srcSpans = fix_span_mismatches(translation[u'data'][u'translations'][0][u'tokenization'][u'src'])
    tgtSpans = fix_span_mismatches(translation[u'data'][u'translations'][0][u'tokenization'][u'tgt'])

    sgId = hashlib.sha224(toutf8(source)).hexdigest() # unique searchgraph/sentence id generated by source

    """ searchgraph """
    sg = translation[u'data'][u'translations'][0][u'searchGraph']

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

    """ translation options """
    tOptions = {}
    if returnOptions:
        tOptions = process_options(source, translation[u'data'][u'translations'][0][u'topt'], 5) # source sentence, options, max_level size

    if returnTranslation:
       # needs to have >1 translations
        res = { 'errors' : [],
    		  'data': { 'source': source, 'sourceSegmentation' : srcSpans, 'options' : tOptions,
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

""" process Translation Options. Same implementation as in Caitra
Sentence: already tokenized source sentence
Options: in JSON format, as receieved from server.py  """
def process_options(sentence, options, max_level):
  # init future cost spans
  cost = {}
  # TODO: edit server.py to return tokenizedSource by default (during decoding)
  pProcess  = request_to_server_py(sentence, action='tokenize')
  sentence = pProcess[u'data'][u'tokenizedSource']
  words = sentence.split(' ')
  wordsLength = len(words)

  for start in range(0,wordsLength):
    for end in range(start, wordsLength):
        cost[(start, end)] = -100 * (1+end-start)

  # get cheapest costs from options
  for option in options:
        start = option['start']
        end = option['end']
        fscore = option['fscore']
        if (cost[(start, end)] < fscore):
	  cost[(start, end)] = fscore

  # get cheapest (binary) combination
  for size in range(1, wordsLength+1):
    for start in range(0, wordsLength - size +1):
        cheapest = cost[(start, start+size-1)]
        for middle in range(1, size):
            combined = cost[(start, start+middle-1)] + cost[(start+middle, start+size-1)]
            if combined > cheapest:
                cheapest = combined

        cost[(start,start+size-1)] = cheapest
  path_cost = cost[(0,wordsLength-1)]

  # include future cost estimate in full cost of each option
  for option in options:
    start = option['start']
    end = option['end']
    option['full_cost'] = option['fscore'] - path_cost
    if start > 0:
      option['full_cost'] += cost[(0, start-1)]
    if (end+1 < wordsLength):
      option['full_cost'] += cost[(end+1, wordsLength-1)]
    del option['fscore'] # remove unnecessary items from the dict
    del option['scores']

  # compute level for each option
  filled = [-1] * wordsLength

  # sort options by full cost
  options.sort(key=lambda options: options['full_cost'], reverse=True)
  filtered_options = [] # to keep only options that have level < max_level

  for k in xrange(len(options)):
    level = 0

    for i in range(options[k]['start'], options[k]['end']+1):
      if filled[i]+1 > level:
	level = filled[i]+1

    if level <= max_level: # we want to get rid of some of the options
      for i in range(options[k]['start'], options[k]['end']+1):
	filled[i] = level # filled[i] +1 #

      options[k]['level'] = level
      filtered_options.append(options[k])

  filtered_options.sort(key=lambda filtered_options: filtered_options['start'])

  return filtered_options

"""This class will handle our client/server API. Each function we would like to
    export needs to be decorated with the @event decorator (see example below)."""
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
      start_time = time.time()
      res = request_translation_and_searchgraph(toutf8(data[u'source']))
      res.get('data',{}).setdefault ('segId', data.get('segId'))
      res.get('data',{}).setdefault ('isPreFetch', data.get('isPreFetch'))
      res[u'data'][u'elapsedTime'] = time.time()-start_time
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
      target = data[u'target'] # don't convert to utf8, it will complain after toutf8(prefix)
      caretPos = data[u'caretPos']
      prefix = target[0:caretPos]
      prefix_no_encoding = prefix
      prefix = toutf8(prefix)

      # tokenize prefix (change of var name to "userInput" because "prefix" needs to be returned to the client)
      pProcess  = request_to_server_py('', action='tokenize', target=prefix)
      userInput = pProcess[u'data'][u'tokenizedTarget']
      userInput = toutf8(userInput)

      sgId = hashlib.sha224(source).hexdigest()
      if searchGraph.get(sgId) is None:
        logging.debug('request searchgraph')
        request_translation_and_searchgraph(source, returnTranslation = False, returnOptions = False)

      logging.debug("calling prediction binary")
      prediction = ''
      timeout = 1 # seconds - only integer values allowed
      if predictProcess.get(sgId) is None:
        logging.debug('creating a new prediction process')
        try:
	    t = time.time()
	    err = open("%s/predict.%s.%s.err" % (log_dir,sgId,t),"w")
	    log = "%s/predict.%s.%s.in" % (log_dir,sgId,t)
            p = subprocess.Popen(['/opt/cat-server/predict','-W','-s','3','3','-t','0.4','-f','5','-m','0.1','-l',log], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, preexec_fn = lambda: os.nice(10),)
            p.stdin.write(searchGraph[sgId])
            p.stdin.flush()
	    predictProcess[ sgId ] = p
	    timeout = 3
        except:
            logging.debug("could not create prediction process")
            print sys.exc_info()[0]
      try:
          predictProcess[ sgId ].stdin.write(userInput+'\n')
          predictProcess[ sgId ].stdin.flush()

          """ timeout """
          signal.signal(signal.SIGALRM, alarm_handler)
          signal.alarm(timeout)

          try:
            prediction = predictProcess[ sgId ].stdout.readline()
            """ prediction, error =  p.communicate() """
            signal.alarm(0)  # reset the alarm
          except Alarm:
	    logging.debug("interaction with predict binary failed")
            predictProcess[ sgId ].kill()
            predictProcess[ sgId ].wait()
            del predictProcess[ sgId ]
      except:
        logging.debug("subprocess error")
	predictProcess[ sgId ].kill()
	predictProcess[ sgId ].wait()
	del predictProcess[ sgId ]
        '''prediction = p.stdout.readline()
          p.kill()'''

      logging.debug("prediction for prefix '" + prefix + "' is '" + prediction + "'")
      res = {}
      if prediction:
          # add prefix to ensure correct tokenization (esp. of opening/closing quotes).
	  # fix bug in binary, which may introduce extra spaces
	  if prefix.endswith(" ") and prediction.startswith(" "):
              prediction = prediction[1:]
              logging.debug("removed extra space, so that prefix '" + prefix + "' is followed by '" + prediction + "'")
          prediction = prefix + prediction
          #postprocessing
          pProcess   = request_to_server_py(prediction, 'detokenize', use_cache=True)
          prediction = pProcess[u'data'][u'translations'][0][u'detokenizedText']

          pProcess   = request_to_server_py(toutf8(prediction), 'detruecase', use_cache=True)
          prediction = pProcess[u'data'][u'translations'][0][u'detruecasedText']

	  # added for the case where the user has typed extra spaces
	  #(they are automatically removed in the postprocessing, and therefore
	  # the previous target suffix does not match the generated one, and prediction is not updated at the GUI)
	  # ' '.join(prefix.split() is the prefix string w/o excess whitespace
	  lenSplitPrefix = len(' '.join(prefix_no_encoding.split()))
	  lenPrefix = len(prefix_no_encoding)
	  if lenPrefix != lenSplitPrefix:
	    pos = caretPos - (lenPrefix - lenSplitPrefix)
	    correctedPrediction =  prefix_no_encoding + prediction[pos:]
	    correctedPrediction = toutf8(correctedPrediction)
	  else:
	    correctedPrediction = toutf8(prediction)
	  # call server and get relevant information from reponse
	  response = request_to_server_py(source, action='tokenize', target=correctedPrediction, use_cache=True)
	  srcSpans = fix_span_mismatches(response[u'data'][u'tokenization'][u'src'])
	  tgtSpans = fix_span_mismatches(response[u'data'][u'tokenization'][u'tgt'])

	  res = { 'errors': errors,
		  'data': {
			'caretPos': caretPos,
			'elapsedTime': time.time() - start_time,
			'source': source,
			'sourceSegmentation' : srcSpans,
			'nbest': [ { 'target': correctedPrediction, 'elapsedTime': time.time() - start_time, 'author': 'ITP' , 'targetSegmentation': tgtSpans }
				 ]
			  } }
      self.emit('setPrefixResult', res)

    # Validates source-target pair
    """ @param {Object}
    * @setup obj
    *   source {String}
    *   target {String}
    * @trigger validateResult
    * @return {Object}
    *   errors {Array} List of error messages
    *   data {Object}
    *   @setup data
    *     elapsedTime {Number} ms """
    @cat_event
    def validate(self,data):
      start_time = time.time()
      # requires source and target text
      source = toutf8(data[u'source'])
      target = toutf8(data[u'target'])

      # call server and get relevant information from reponse
      response = request_to_server_py(source, action='update', target=target, use_cache=True)

      # send response to client
      errors = []
      res = { 'errors': errors,
              'data': {
                 'elapsedTime': time.time() - start_time,
            } }
      self.emit('validateResult', res)

    @cat_event
    def getAlignments(self, data):
      start_time = time.time()
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
      if len(alignmentPoints) == 0:
        errors = ['could not establish any alignment points']
        alignmentMatrix = []
      else:
        errors = []
        alignmentMatrix = [[0 for i in range(len(tgtSpans))] for j in range(len(srcSpans))]
        for point in alignmentPoints:
          alignmentMatrix[ point[u'src_idx'] ][ point[u'tgt_idx'] ] = 1

      res = { 'errors': errors,
              'data': {'source': source,
                       'sourceSegmentation': srcSpans,
                       'target': target,
                       'targetSegmentation': tgtSpans,
                       'alignments': alignmentMatrix,
                       'elapsedTime': time.time() - start_time
          } }
      self.emit('getAlignmentsResult', res)


    @cat_event
    def getTokens(self, data):
      start_time = time.time()

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
		    'targetSegmentation': tgtSpans,
                    'elapsedTime': time.time() - start_time
                    } }
      self.emit('getTokensResult', res)


    # Retrieves confidence results for the current segment
    """ @param {Object}
    * @setup obj
    *   source {String}
    *   target {String}
    *   validatedTokens {Array} List of Booleans, where 1 indicates that the token is validated
    * @trigger getConfidencesResult
    * @return {Object}
    *   errors {Array} List of error messages
    *   data {Object}
    *   @setup data
    *     quality {Number} Quality measure of overall hypothesis
    *     confidences {Array} List of floats for each token
    *     source {String} Verified source
    *     sourceSegmentation {Array} Verified source segmentation
    *     target {String} Result
    *     targetSegmentation {Array}
    *     elapsedTime {Number} ms
    """
    @cat_event
    def getConfidences(self, data):
      start_time = time.time()

      source = toutf8(data[u'source'])
      target = toutf8(data[u'target'])

      response = request_to_server_py(source, action='confidence', target=target)
      srcSpans = fix_span_mismatches(response[u'data'][u'tokenization'][u'src'])
      tgtSpans = fix_span_mismatches(response[u'data'][u'tokenization'][u'tgt'])
      word_confidence = response[u'data'][u'confidence'][u'word']
      sent_confidence = response[u'data'][u'confidence'][u'word']

      self.emit ('getConfidencesResult', {
          'errors': [],
          'data': {
            'quality': sent_confidence,
            'confidences': word_confidence,
            'source': source,
            'sourceSegmentation': srcSpans,
            'target': target,
            'targetSegmentation': tgtSpans,
            'elapsedTime': time.time() - start_time
            }
          })


    """ Adds a replacement rule.
    * @param {Object}
    * @setup obj
    *   [ruleId] {Number}
    *   sourceRule {String}
    *   targetRule {String}
    *   targetReplacement {String}
    *   matchCase {Boolean}
    *   isRegExp {Boolean}
    *   persistent {Boolean} TODO
    * @trigger setReplacementRuleResult
    * @return {Object}
    *   errors {Array} List of error messages
    *   data {Object}
    *   @setup data
    *     elapsedTime {Number} ms
    *     ruleId {Number} ruleId of the rule
    """
    @cat_event
    def setReplacementRule(self, data):
      print "setReplacementRule not implemented"

    # GET REPLACEMENT RULE - Returns the list of rules
    """
    * @trigger getReplacementRulesResult
    * @return {Object}
    *   errors {Array} List of error messages
    *   data {Object}
    *   @setup data
    *     elapsedTime {Number} ms
    *     rules {Array} List of rules
    *     @setup rules
    *       ruleId {Number}
    *       sourceRule {String}
    *       targetRule {String}
    *       targetReplacement {String}
    *       isRegExp {Boolean}
    *       matchCase {Boolean}
    *       nFails {Number} Number of times the regex provoked an exception
    *       persistent {Boolean} TODO
    """
    @cat_event
    def getReplacementRule(self, data):
      print "called getReplacementRule", data
      print "getReplacementRule not implemented, request ignored"

    # Deletes replacement rule
    """ @param {Object}
    * @setup obj
    *   ruleId {Number}
    * @trigger delReplacementRuleResult
    * @return {Object}
    *   errors {Array} List of error messages
    *   data {Object}
    *   @setup data
    *     elapsedTime {Number} ms
    """
    @cat_event
    def delReplacementRule(self, data):
      print "called setReplacementRule", data
      print "delReplacementRule not implemented, request ignored"

    # Applies *all* replacement rules, so that the user does not need to type for an entered rule to become visible.
    """ @param {Object}
    * @setup obj
    *   source {String}
    *   target {String}
    * @trigger applyReplacementRulesResult
    * @return {Object}
    *   errors {Array} List of error messages
    *   data {Object}
    *   @setup data
    *     elapsedTime {Number} ms
    """
    @cat_event
    def applyReplacementRule(self, data):
      print "called setReplacementRule", data
      print "applyReplacementRule not implemented, request ignored"


    # Retrieves contributions that users completed after full supervision
    """ * @trigger getValidatedContributionsResult
    * @return {Object}
    *   errors {Array} List of error messages
    *   data {Object}
    *   @setup data
    *     contributions {Array} List of validated contributions
    *     @setup contributions
    *       source {String} Validated source
    *       target {String} Validated target
    *     elapsedTime {Number} ms
    """
    @cat_event
    def getValidatedContributions(self, data):
      print "getValidatedContributions not implemented"

    def _biconcor_proc (self, data):
      lang_pair = 'xx-xx' # not used
      biconcor_proc = biconcor_processes.get (lang_pair)
      if biconcor_proc is None and biconcor_model is not None:
        biconcor_proc = biconcor_processes[lang_pair] = BiconcorProcess (biconcor_cmd, biconcor_model)
      return biconcor_proc

    @cat_event
    def biconcor (self, data):
      start_time = time.time()
      try:
        biconcor_proc = self._biconcor_proc (data)
        src_phrase = data['srcPhrase']
      except Exception, ex:
        self.emit ('biconcorResult', {'errors':[str(ex)], 'data':{}})
        return
      if not biconcor_proc.is_warm():
        self.emit ('biconcorResult', {
            'errors': [],
            'data': {
              'warm': False,
	      'elapsedTime': time.time() - start_time,
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
            'elapsedTime': time.time() - start_time
            }
          })

    @cat_event
    def warmUpBiconcordancer (self, data):
      start_time = time.time()
      self._biconcor_proc(data).warm_up()

    @cat_event
    def redecode(self, data):
      start_time = time.time()

      source = toutf8(data[u'source'])
      target = toutf8(data[u'target'])
      annotation = data[u'annotation']

      # do something silly
      response = request_to_server_py(source, action='tokenize', target=target, use_cache=True)
      srcSpans = fix_span_mismatches(response[u'data'][u'tokenization'][u'src'])
      tgtSpans = fix_span_mismatches(response[u'data'][u'tokenization'][u'tgt'])
      target = ""
      for i in range(0, len(annotation)):
        if target != "":
          target = target + " "
        if (annotation[i] == 0):
          target = target + "neutral"
        else:
          target = target + "marked"
        annotation[i] = -annotation[i]
      response = request_to_server_py(source, action='tokenize', target=target, use_cache=True)
      tgtSpans = fix_span_mismatches(response[u'data'][u'tokenization'][u'tgt'])

      # send results
      self.emit ('redecodeResult', {
        'errors' : [],
        'data': { 'source': source,
	          'sourceSegmentation': srcSpans,
                  'nbest': ( { 'target': target , 'targetSegmentation': tgtSpans } ,
                             { 'target': target , 'targetSegmentation': tgtSpans } ),
                  'annotation': annotation,
                  'elapsedTime': time.time() - start_time }
      } )

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
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', help='server port to bind to, default: 9999', type=int, default=9999)
    parser.add_argument('--mt-host', help='host of the mt server (server.py), default '+mt_host, default=mt_host)
    parser.add_argument('--mt-port', help='port of the mt server (server.py), default '+str(mt_port), type=int, default=mt_port)
    parser.add_argument('--biconcor-model', help='model file for bilingual concordancer')
    parser.add_argument('--biconcor-cmd', help='command binary for bilingual concordancer')
    parser.add_argument('--log-dir', help='directory for log files', default=".")
    settings = parser.parse_args(sys.argv[1:])
    mt_host = settings.mt_host
    mt_port = settings.mt_port
    log_dir = settings.log_dir
    biconcor_model = settings.biconcor_model
    biconcor_cmd = settings.biconcor_cmd

    log_file = '%s.catserver.log' %datetime.datetime.now().strftime("%Y%m%d-%H.%M.%S")
    log_format = '%(asctime)s %(thread)d - %(filename)s:%(lineno)s: %(message)s'
    logging.basicConfig(filename=log_dir+"/"+log_file,level=logging.DEBUG,format=log_format)

    application = web.Application(
        MinimalRouter.apply_routes([]),
        socket_io_port = settings.port
    )
    SocketServer(application)

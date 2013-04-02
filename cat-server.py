#! /usr/bin/env python
# -*- coding: utf-8 -*-


### includes ###

import sys, os, urllib, urllib2, csv, time, collections
import subprocess

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

# global var searchgraph
searchGraph = ''
sgCsv = 'sg.csv'

# BiconcorProcess objects, indexed by the language pair
biconcor_processes = {}



### generic utils ###


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
        


### connection to server.py ###

server_py_cache = MRUDict (1000)

def request_to_server_py (text, action='translate', use_cache=False):
  port = 8755
  if isinstance (text, unicode):
    text = text.encode ('UTF-8')
  url = 'http://127.0.0.1:%d/%s?%s' % (
    port,
    action,
    'q=%s&key=0&source=xx&target=xx&sg=true' % urllib.quote_plus(text),
    )
  print url
  if use_cache:
    from_cache = server_py_cache.get (url)
    if from_cache is not None:
      return from_cache
  req = urllib2.Request (url, '', {'Content-Type': 'application/json'})
  f = urllib2.urlopen (url)
  output_struct = json.load(f)
  f.close()
  if use_cache:
    server_py_cache[url] = output_struct
  return output_struct



### main Tornadio class ###

# This class will handle our client/server API. Each function we would like to
# exports needs to be decorated with the @event decorator (see example below).

class MinimalConnection(SocketConnection):

    def emit (self, *args, **kwargs):
      """ Print out everything we emit to stdout for debugging. This method can be commented out to disable this. """
      print "emit(%s)" % ', '.join (
        ['%r' % a for a in args] +
        ['%s=%r' % i for i in kwargs.iteritems()]
        )
      return super(MinimalConnection,self).emit (*args, **kwargs)

    # @event is a decorator that exports the function to be used with the
    # socket.io javascript client.
    @event
    # the on_open event is called when a socket.io connection  is opened.
    # This is the place to initialize session variables
    def on_open(self, info):
      print >> sys.stderr, "Connection Info", repr(info.__dict__)
      self.config = { 'enabled': True }

    @event
    # the on_close event is called when a socket.io connection is closed.
    # This is the place to delete session variables
    def on_close(self):
      del self.config

    # PING
    # echos time stamp
    @event
    def ping(self, data):
      print "called ping", data
      res = { 'data': data }
      self.emit('pingResult', res)

    # GET SERVER CONFIG
    # does not seem to be used by anything, so we just return 0
    @event
    def getServerConfig(self):
      print "called getServerConfig"
      res = { 'data' : 0 }
      self.emit('getServerConfigResult', res)

    # CONFIGURE
    @event
    def configure(self, data):
      print "called configure", data

    # DECODE
    @event
    def decode(self, data):
      print "called decode", data
      global searchGraph, sgCsv

      translation = request_to_server_py (data['source'], use_cache=True)

      target = translation[u'data'][u'translations'][0][u'translatedText']
      #searchGraph = translation[u'data'][u'searchgraphs'] # reversed?
      searchGraph = [] # herve - 2013-03-29 - while debugging

      # csv file name will be dynamic (unique id in filename)
      sg = []
      for row in searchGraph:
        sg.append([row["hyp"],row["stack"],row["back"],row["score"],row["transition"],row["recombined"],row["forward"],row["fscore"],row["covered"],toutf8(row["out"])])

      with open(sgCsv, "wb") as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(["hyp","stack","back","score","transition","recombined","forward","fscore","covered","out"])
        sg.sort(key = lambda x:int(x[0]))

        for graph in sg:
            csv_writer.writerow(graph)

      # http://127.0.0.1:8730/translate?q=what+is+going+on&key=0&source=xx&target=xx&sg=true
      # -> {"data": {"translations": [{"translatedText": "Was ist los"}]}}
      # res: { 'target': target,'targetSegmentation': ( (0,5), (6,14), (15,17), (18,23), (23,24) ) }

      # needs to have >1 translations, check out why. (at least a dummy) TargetSegmentation needs to be included in the response as well
      # no need to send the searchgraph back to the client
      #print 'ready to emit';
      res = { 'data': { 'source': data['source'],
                        'nbest': ( { 'target': target }, #'targetSegmentation': ( (0,5), (6,14), (15,17), (18,23), (23,24) ) },
                                   #{ 'target': target }, #'targetSegmentation': ( (0,5), (6,14), (15,17), (18,23), (23,24) ) }
                                 )
                        } }
      self.emit('decodeResult', res)

    # START SESSION
    @event
    def startSession(self, data):
      print "called startSession", data

    # REJECT SUFFIX
    @event
    def rejectSuffix(self, data):
      print "called rejectSuffix", data

    # SET PREFIX
    @event
    def setPrefix(self, data):
      start_time = time.time()
      global searchGraph, sgCsv
      print "called setPrefix", data

      errors = []
      target = data[u'target']
      caretPos = data[u'caretPos']

      prefix = target[0:caretPos]
      prefix = toutf8(prefix)
      print prefix

      args = ['./predict', sgCsv, prefix]

      try:
        prediction = subprocess.check_output(args)
        #toutf8(prediction) # to raise exception
      except Exception as e: # or IOError
        print "Exception error", e
        errors.append (str(e))
        prediction = ''

      print prediction
      elapsed_time = time.time() - start_time
      # detokenize prediction
      res = { 'errors': errors,
              'data': {
                    'caretPos': caretPos,
                    'elapsedTime': elapsed_time,
                    'source': data['source'] ,
                    'sourceSegmentation' : ( (0,5), (6,14), (15,17), (18,23), (23,24) ),
                    'nbest': [ { 'target': prefix + prediction,
                                 'targetSegmentation': ( (0,5), (6,14), (15,17), (18,23), (23,24), (25,30),(31,32),(33,34),(35,36),(37,38) ),
                                 'elapsedTime': elapsed_time, 'author': 'ITP' }
                             ]
                      } }
      self.emit('setPrefixResult', res)

    @event
    def Validate(self,data):
        print "called Validate", data
    # getAlignments
    @event
    def getAlignments(self, data):
      print "called getAlignments", data

    # getTokens
    @event
    def getTokens(self, data):
      print "called getTokens", data

    @event
    def getConfidences(self, data):
      print "called getConfidences", data

    @event
    def setReplacementRule(self, data):
      print "called setReplacementRule", data

    @event
    def getValidatedContributions(self, data):
      print "called getValidatedContributions", data

    @event
    def biconcor (self, data):
      print "called biconcor", data
      lang_pair = 'en-de' # '%s-%s' % (data['srcLang'], data['tgtLang'])
      src_phrase = data['srcPhrase']
      biconcor_proc = biconcor_processes.get (lang_pair)
      if biconcor_proc is None:
        biconcor_proc = biconcor_processes[lang_pair] = BiconcorProcess (lang_pair)
      if not biconcor_proc.is_warm():
        self.emit ('biconcorResult', {
            'errors': None,
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
          'errors': None,
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
        print 'Router', repr(info)


# Create tornadio router
MinimalRouter = TornadioRouter(RouterConnection)



### cmd-line parsing and server init ###

if __name__ == "__main__":

    if len(sys.argv) == 1:
      port = 8660
    elif len(sys.argv) == 2:
      port = int(sys.argv[1])
    else:
      print >> sys.stderr, "usage: %s [port]" % sys.argv[0]
      sys.exit(1)

    # Create socket application
    application = web.Application(
        MinimalRouter.apply_routes([
                                      # Here you can add more handlers for
                                      # traditional services,
                                      # like a file or ajax server. See
                                      # Tornado documentation for that
                                      # (r"/", IndexHandler),
                                      # (r"/js/(.*)", JsHandler),
                                      # (r"/css/(.*)", CssHandler),
                                      # (r"/examples/(.*)", ExampleHandler)
                                    ]),
        # if you want the flash transport to work you need to place the
        # flashpolicy.xml and WebSocketMain.swf in the correct place (see docs)
        #flash_policy_port = 843,
        #flash_policy_file = os.path.join(ROOT, 'flashpolicy.xml'),
        socket_io_port = port
    )
    # Create and start tornadio server
    SocketServer(application)

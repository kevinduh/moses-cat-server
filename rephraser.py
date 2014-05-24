#!/usr/bin/env python
import BaseHTTPServer
import collections
import datetime
import json
import math
import re
import operator
import os
import subprocess
import threading
import time
import urllib
  
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


""" cache some rephrase results (for individual segments), at least until the rephrase table is ready """ 
cached_rephrase_table = MRUDict (1000)
""" cache rephrase candidates for fast LM scoring and results 
cached_final_rephrase_candidates =  MRUDict (1000)"""

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
                    print "%s since last activity, killing subprocess binary" % self.INACTIVE_TIMEOUT
                    self.child_proc.terminate()
                    self.child_proc.wait()
                    break

#----------------------------------------------------------------------------------------------------------------------------------
#path to query and queryPhraseTableMin
path_to_binaries = '/fs/lofn0/chara/rephraser/'
# Phrase Tables for en-es and es-en
PT_en_es = '/fs/lofn0/chara/phrase-table-en-es.minphr'
PT_es_en = '/fs/lofn0/chara/phrase-table-es-en-2403.minphr'
LM = '/fs/lofn0/chara/rephraser/toy.binlm.89' # path to language model

class Rephraser(object):
  def __init__ (self, LanguageModel, PT_ef, PT_fe, moses_binaries):
    PT_en_es = PT_ef
    PT_es_en = PT_fe
    LM = LanguageModel
    path_to_binaries = moses_binaries
    
    self.procEnEs = PersistentSubprocess('en-es')
    self.procEsEn = PersistentSubprocess('es-en')
    self.LM = PersistentSubprocess('LM')
  
  def return_rephrase_candidates(self, src_phrase):
    src_phrase = src_phrase.decode ('UTF-8')
    if not self.procEnEs.is_warm() or not self.procEsEn.is_warm() or not self.LM.is_warm() :
        print "The subprocesses are warming up..."
       
    """ make sure that the input has correct format """
    rephraseInput = src_phrase.split('||')
    if len(rephraseInput) > 1:
        text_to_rephrase = rephraseInput[1].strip(' \t\n\r')
        prefix = ' '.join(rephraseInput[0].strip(' \t\n\r').split(' ')[-4:]) # last 4 tokens, will be used for the LM scoring
        suffix = ' '.join(rephraseInput[2].strip(' \t\n\r').split(' ')[:4])  # first 4 tokens, will be used for the LM scoring
    else:
        # throw error message(expected format: prefix || to rephrase || suffix), but now use this for debugging
        text_to_rephrase = src_phrase
        prefix = ''
        suffix = ''
    
    rephrase_with_lm = {}
    possible_rephrases = {}
    parts_to_rephrase = text_to_rephrase.split(' ')
    inputSize = len(parts_to_rephrase)
    
    """ calculate rephrase scores. This will be reduntant once the rephrase table is ready """ 
    # compute rephrase scores for all ngram. format: [[u'give an', 0, 1], [u'an example', 1, 2]]
    for ngram in range(1, inputSize+1): 
        for part in ngrams(parts_to_rephrase, ngram):
            translated_phrases = {}
            temp_rephrases = {}
            covered_start = part[1]
            covered_end = part[2]
            ngram_phrase = part[0]
            if cached_rephrase_table.get(ngram_phrase) is None: 
                potential_translation = self.procEnEs.get_output(ngram_phrase)
                if (len(potential_translation)> 0):
                  for translation in potential_translation:
                      #print translation
                      split_translation = translation.split('|||')
                      try:
                          scores = split_translation[2].split(' ')
                          """ weighted score? r_score = TM0*math.log10(float(scores[1])) + TM1 * math.log10(float(scores[3])) """
                          """ split results in 1st value being ' ', so the actual scores index starts from 1, not 0 """
                          """ scores[1] is Pef and scores[3] Pfe """
                          r_score = math.log10(float(scores[1]) * float(scores[3]))
                          
                          """ format: translated_phrases['en el caso']= (0, 1, phrase table score) """
                          #translated_phrases[split_translation[1].strip(' \t\n\r')] = [covered_start, covered_end, r_score, float(scores[1]), float(scores[3]) ]
                          translated_phrases[split_translation[1].strip(' \t\n\r')] = r_score #[covered_start, covered_end, r_score]
                      except: 
                          pass
                        
                  """ for the top 15 (es) translations, query back their translations into English """
                  for possible_translation in translated_phrases.items():
                      initial_phrase_score = float(possible_translation[1])
                      """ for each translation, query phrase back table for es - en (Pef) """
                      rephrase_candidate = self.procEsEn.get_output(possible_translation[0]) 
                      for line in rephrase_candidate:
                          try:
                              phrase = line.split('|||')[1].strip(' \t\n\r')
                              """ to avoid cases where it's exactly the same phrase plus some e.g. punctuation marks 
                              if text_to_rephrase not in phrase: """
                              scores = line.split('|||')[2].split(' ')
                              """ weighted score? r_score = TM0*math.log10(float(scores[1])) + TM1 * math.log10(float(scores[3])) """
                              rephrase_table_score = math.log10(float(scores[1]) * float(scores[3])) + initial_phrase_score
                              #temp_rephrases[phrase] = [covered_start, covered_end, rephrase_table_score, float(possible_translation[1][3]), float(possible_translation[1][4]), float(scores[1]), float(scores[3]) ]
                              temp_rephrases[phrase] = [covered_start, covered_end, rephrase_table_score]
                          except:
                              pass
                  
                if (len(temp_rephrases)==0 and ngram == 1):
                  # OOV word (unigrams only), append with high rephrase score
                  temp_rephrases[ngram_phrase] = [covered_start, covered_end, -99.999]   
                """ sort temp rephrase dict, and keep top 10 (which are added to "possible_rephrases" dict) or top 5 """
                temp_rephrases_sorted = sorted(temp_rephrases.items(), key = lambda e: e[1][2], reverse=True)
                
                if ngram == inputSize: # if input is fully covered keep top 10 rephrase candidates, orelse top 5    
                    temp_rephrases_sorted = temp_rephrases_sorted[:10]
                else:
                    temp_rephrases_sorted = temp_rephrases_sorted[:5]
                
                '''if len(temp_rephrases_sorted) > 0 :'''
                for cache in temp_rephrases_sorted:
                    try:
                        cached_rephrase_table[ngram_phrase].update({cache[0]: cache[1][2]})
                    except:
                        cached_rephrase_table[ngram_phrase] = {cache[0]: cache[1][2]}
                '''else:
                    """ maybe not a necessary step, but indicates that segment does not exist in the en-es phrase table """
                    cached_rephrase_table[ngram_phrase] = {'': -999.999}'''
                    
                #print cached_rephrase_table[ngram_phrase]
                possible_rephrases.update(temp_rephrases_sorted)
            else:
                """ ngram_phrase exists in cached_rephrased_table, use information from there to update the possible_rephrases dict """
                for rephrased_item in cached_rephrase_table[ngram_phrase].items():
                    temp_rephrases[rephrased_item[0]] = [covered_start, covered_end, rephrased_item[1]]
                possible_rephrases.update(temp_rephrases)
                
    """ done with ngram.
    now combine possible_rephrases """
    
    ''' split according to covered_start '''
    covered_states = {}
    for i in range(0, inputSize):
      covered_states[i] = [(k, v) for k, v in possible_rephrases.items() if v[0] == i]
    
    #print '------- print COVERED states (dict. with key: covered_from) ------'''
    #print covered_states
   
    start_time = time.time()
    final_rephrase_candidates = {}
    rephrase_candidates = []
    for i in range(0, inputSize):
      rephrase_candidates.append({})
      
    for rephrase_candidate in covered_states[0]:
      phrase = rephrase_candidate[0]
      score = rephrase_candidate[1][2]
      to_state_covered = rephrase_candidate[1][1] + 1
      if to_state_covered < inputSize : # if not all states have been covered
        """ next candidate: list of [phrase, score] """
        rephrase_candidates = decode_candidates(to_state_covered, inputSize, covered_states, rephrase_candidates)
        for next_candidate in rephrase_candidates[to_state_covered].items():
            final_phrase = phrase + ' ' + next_candidate[0]
            if text_to_rephrase not in final_phrase: # to avoid combining phrases identical to the input
                final_score = score + next_candidate[1]
                final_rephrase_candidates[final_phrase] = final_score
      else:
        if text_to_rephrase not in phrase: # to avoid combining phrases identical to the input
            final_rephrase_candidates[phrase] = score
      
    sorted_final_rephrase_candidates = sorted(final_rephrase_candidates.iteritems(), key=operator.itemgetter(1), reverse=True)
    #print '----- all rephrase candidates (sorted by rephrase score only) -----' 
    #print sorted_final_rephrase_candidates
    
    """ now score with language model """
    for rephrased in sorted_final_rephrase_candidates[:30]:  # take top x (30?) items and score with LM
        #try:
        rephrase = rephrased[0]
        LM_score = self.LM.get_lm_score(prefix+' '+rephrase+' '+ suffix)
        #print LM_score
        total = re.search("(.+)Total: ([\d\-\.]+)", LM_score)
        if total:
            """ weighted sum? lm = LM0*float(total.group(2)) + rephrased[1] """
            lm = float(total.group(2)) + float(rephrased[1])
            rephrase_with_lm[rephrase] = lm
        '''except Exception,e:
        print str(e)  '''

    sorted_possible_rephrases = sorted(rephrase_with_lm.iteritems(), key=operator.itemgetter(1), reverse=True)
    #print '----- final rephrases (top 30) -----'
    return sorted_possible_rephrases    #[0:10] to output 10 most probable 
    #print  time.time() - start_time # time it took after the calculation of the rephrase scores


class PersistentSubprocess (object):
    SUBPROCESS_CMDS = {
        'en-es': [path_to_binaries + './queryPhraseTableMin -m 15 -n 12 -s -t ' + PT_en_es], 
        'es-en': [path_to_binaries + './queryPhraseTableMin -m 15 -n 12 -s -t '+ PT_es_en],
        'LM': [path_to_binaries + './query -n ' + LM]
        }
    def __init__ (self, action):
        self.cmd = self.SUBPROCESS_CMDS[action]
        self.child = None
        self.killer = None
        self.child_lock = threading.Lock()
        self.warm_up()

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
        """ Blocks until we have the process running and ready to accept requests """
        with self.child_lock:
            if not self.is_warm():
                assert self.child is None, repr(self.child)
                assert self.killer is None, repr(self.killer)
                print self.cmd
                try:
                    self.child = subprocess.Popen (
                        self.cmd,
                        stdin = subprocess.PIPE,
                        stdout = subprocess.PIPE,
                        preexec_fn = lambda: os.nice(10),
                        shell=True
                    )
                except Exception, e:
                    print str(e), 'expect'
                self.child.stdin.write('') 
                self.child.stdin.flush()
                self.child.stdout
                #expect (self.child.stdout, 'tcmalloc:')
                time.sleep(2)
                self.killer = KillerThread (self.child)
                self.killer.start()

    def get_output (self, src_phrase):
        """
        Returns the raw binary output for the given source phrase. Output is returned as a list of strings, one per line.
        """
        self.warm_up()
        with self.child_lock:
            self.killer.record_activity()
            print >> self.child.stdin, src_phrase.encode ('UTF-8')
            self.child.stdin.flush()
            
            output = expect (self.child.stdout)
            #self.killer.record_activity()
            return output
    
    def get_lm_score (self, src_phrase):
        """
        Returns the raw binary output for the given source phrase. Output is returned as a list of strings, one per line.
        """
        self.warm_up()
        with self.child_lock:
            self.killer.record_activity()
            print >> self.child.stdin, src_phrase.encode ('UTF-8')
            self.child.stdin.flush()
            output = self.child.stdout.readline()    #expect (self.child.stdout)
            #self.killer.record_activity()
            return output

#----------------------------------------------------------------------------------------------------------------------------------
# utils
def expect (fh, expected = '###', encoding='UTF-8', do_rstrip=True):
    """
    Reads lines from `fh', saving them all into a list, until one contains the string in `expected', at which point the accumulated
    line buffer is returned. Also performs decoding if `encoding' is not None.
    """
    #fcntl.fcntl(fh.fileno(), fcntl.F_SETFL, os.O_NONBLOCK)
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
            if expected in line:  # break if there is EOF indication (###)
                break
            ret.append (line)
    return ret

def ngrams(phrase, n):
  #already split phrase, orelse: phrase = phrase.split(' ')  
  segments = []
  for i in range(len(phrase)-n+1):
    segments.append([' '.join(phrase[i:i+n]), i, i+n-1])   # append string and position information
  return segments

"""
    to_state_covered: state number which has not been covered yet.
    inputSize: size of input (text to be rephrased)
    covered_states: list of possible rephrases for each source segment. key: covered_from
    rephrase_candidates: list of dictionaries that contain all possible rephrased combinations from covered_states. key:  covered_from
"""
def decode_candidates(to_state_covered, inputSize, covered_states, rephrase_candidates):
    if len(rephrase_candidates[to_state_covered]) > 0:  # states are combined already, no need to process further
        return rephrase_candidates
    else:
        current_state = inputSize - 1
        while current_state >= to_state_covered: 
          """ start from right to left and append to rephrase_candidates """
          for rephrase_candidate in covered_states[current_state]:
            candidate_phrase = rephrase_candidate[0]
            candidate_score = rephrase_candidate[1][2]
            to_current_state_covered = rephrase_candidate[1][1] + 1
            if to_current_state_covered < inputSize:   # forward combinations exist already, just append
              for forward_candidate in rephrase_candidates[to_current_state_covered].items():
                phrase = candidate_phrase + ' ' + forward_candidate[0]
                rephrase_candidates[current_state].update({ phrase : candidate_score + forward_candidate[1] })
            else:
                rephrase_candidates[current_state].update({ candidate_phrase : candidate_score })     
          current_state = current_state - 1
        return rephrase_candidates 

def handleRequestsUsing(rephraseProcess):
  return lambda *args: RequestHandler(rephraseProcess, *args)

class RequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):
  def __init__(self, rephraserProcess, *args):
    self.rephraseProcess = rephraserProcess
    BaseHTTPServer.BaseHTTPRequestHandler.__init__(self, *args)
    
  def do_GET(self):
    try:
      req = self.path.split('q=')[1]
      src_phrase = urllib.unquote_plus(req)
      #print src_phrase
      paraphrase_candidates = self.rephraseProcess.return_rephrase_candidates(src_phrase)
      self.request.sendall(json.dumps({'paraphrases': paraphrase_candidates, 'errors' : [] }))
      """ response e.g.:  {"paraphrases": [["make a conference", -32.82269941799895], ["give an speech", -34.81293157486423],
      ["give a presentation", -35.20896316054598], ["deliver a lecture", -35.286778641614625], ["hold a conference", -35.89656612670538],
      ["giving a talk", -36.24006942233497], ["speak at a conference", -36.597989111520846], ["to do a conference", -36.641099417998944],
      ["giving a lecture", -36.73462110495801], ["provide a sermon", -37.06399433758352], ["offer a sermon", -37.71286129494698],
      ["extend a conference", -38.33444240415715], ["render an conference", -38.769654307197264],
      ["offer , a conference", -39.5264153378513], ["to extend a conference", -39.82329557501232],
      ["provide , a conference", -39.887348380487836], ["giving a conference", -39.955251759993374],
      ["den a sermon", -40.06976869022952], ["den a lecture", -40.7228140653069], ["den a chat", -40.74619291189463],
      ["den , a conference", -41.096722733133845], ["da a sermon", -41.10277730930311], ["render an of conferences", -41.89462066958991],
      ["da , a conference", -42.12973135220743], ["render an give lessons", -42.62529734222892],
      ["to extend a of conferences", -42.94826193740498], ["to extend a give lessons", -43.67893861004397],
      ["ask yourself one conference", -44.10857397960082], ["to extend a give conferences", -45.159878133414146],
      ["den host a lecture", -45.27165199402606]], "errors": []} """
    except Exception, ex:
      self.request.sendall(json.dumps({'paraphrases': [], 'errors' : [str(ex)]}))
    return
    
#----------------------------------------------------------------------------------------------------------------------------------
# cmd line interface for debuggingd
def cmd_debug ():
  import argparse
  import sys
  
  parser = argparse.ArgumentParser()
  parser.add_argument('--PT-ef', help='Phrase table english to foreign (en-es)', default='/fs/lofn0/chara/phrase-table-en-es.minphr')
  parser.add_argument('--PT-fe', help='Phrase table foreign to english (es-en)', default='/fs/lofn0/chara/phrase-table-es-en-2403.minphr')
  parser.add_argument('--LM', help='Path to language model', default='toy.binlm.89')
  parser.add_argument('--moses-binaries', help='Path to queryPhraseTableMin and query', default='/fs/lofn0/chara/rephraser/')
  settings = parser.parse_args(sys.argv[1:])
  
  rephraseProcess = Rephraser(settings.LM, settings.PT_ef, settings.PT_fe, settings.moses_binaries)
  while True:
    try:
      src_phrase = raw_input ('What do you want to rephrase?> ')
    except EOFError:
      break
    
    print rephraseProcess.return_rephrase_candidates(src_phrase)

def main():
  """ API format: eg. 'http://localhost:8666/rephrase/q=I+want+to+||+give+a+lecture+||+in+Paris+next+week' """
  import argparse
  import sys
  
  parser = argparse.ArgumentParser()
  parser.add_argument('--PT-ef', help='Phrase table english to foreign (en-es)', default='/fs/lofn0/chara/phrase-table-en-es.minphr')
  parser.add_argument('--PT-fe', help='Phrase table foreign to english (es-en)', default='/fs/lofn0/chara/phrase-table-es-en-2403.minphr')
  parser.add_argument('--LM', help='Path to language model', default='toy.binlm.89')
  parser.add_argument('--moses-binaries', help='Path to queryPhraseTableMin and query', default='/fs/lofn0/chara/rephraser/')
  parser.add_argument('--port', help='API port', type=int, default=8666)
  parser.add_argument('--host', help='host of the API, default: localhost', default='localhost')
  settings = parser.parse_args(sys.argv[1:])
  rephraseProcess = Rephraser(settings.LM, settings.PT_ef, settings.PT_fe, settings.moses_binaries)

  """ for http requests """
  PORT_NUMBER = settings.port
  HOST = settings.host
  server_class = BaseHTTPServer.HTTPServer
  handler = handleRequestsUsing(rephraseProcess)
  httpd = server_class((HOST, PORT_NUMBER), handler)
  print time.asctime(), "Server Starts - %s:%s" % (HOST, PORT_NUMBER)
  try:
    httpd.serve_forever()
  except KeyboardInterrupt:
    pass
  httpd.server_close()
  print time.asctime(), "Server Stops - %s:%s" % (HOST, PORT_NUMBER)  
  
if __name__ == '__main__':
  # cmd_debug() call for cmd debugging. Orelse main() to start the API
  main()
  

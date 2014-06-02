#!/usr/bin/env python

import json
import optparse
import urllib
import sys

def main():
  parser = optparse.OptionParser("%prog [args] URL")
  parser.add_option("-n", "--number-options", dest="noptions",
                    help="Number of options", type="int")

  parser.set_defaults(
    noptions = 10
  )

  options,args = parser.parse_args(sys.argv)
  if len(args) < 2:
    parser.error("Need to specify a URL")
  url = args[1]

  print "e.g. I will || give a lecture || tomorrow"
  while True:
    print "in> ",
    line = sys.stdin.readline()
    if not line: break
    fields = line[:-1].split(" || ")
    if len(fields) != 3:
      print "Error: select a segment for rephrasing as in the example"
      continue
    params = urllib.urlencode({"q": line[:-1]})
    ufh = urllib.urlopen(url + "?%s" % params)
    raw_resp = ufh.read()
    resp = json.loads(raw_resp)
    if resp["errors"]:
      print "Error: "
      print "\n".join(resp["errors"])
    else:
      paraphrases = resp["paraphrases"][:options.noptions]
      print 
      for p in paraphrases:
        print "%4f\t\t" % p[1],
        print fields[0],"\033[94m",p[0],"\033[0m",fields[2]
      print

if __name__ == "__main__":
  main()

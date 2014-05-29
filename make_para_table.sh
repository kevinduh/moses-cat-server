#!/bin/sh

if [ $# -ne 1 ]; then
  echo "Usage: $0 phrase-table.gz"
  exit 1
fi

dir=`dirname $0`
extract_file=`basename $1 .gz`.extract

zcat $1  | $dir/paraphrase.perl > $extract_file
LC_ALL=C sort -T . -S 10G --parallel 8 $extract_file | $dir/consolidate.perl | gzip -c

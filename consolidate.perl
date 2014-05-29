#!/usr/bin/perl -w

use strict;

my $THRESHOLD = 10 ** -4;

my ($last_pp,$score);
while(<STDIN>) {
    chop;
    my @ITEM = split(/ \|\|\| /);
    my $pp = "$ITEM[0] ||| $ITEM[1]";
    if (defined($last_pp) && $pp ne $last_pp) {
	print $last_pp." ||| ".$score."\n" if $score > $THRESHOLD;
	$score = 0;
    }
    $score += $ITEM[2];
    $last_pp = $pp;
}
print $last_pp." ||| ".$score."\n" if $score > $THRESHOLD;

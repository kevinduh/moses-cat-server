#!/usr/bin/perl -w

use strict;

my $THRESHOLD = 10 ** -4;

my ($last_f,%P_F_GIVEN_E,%P_E_GIVEN_F);

while(<STDIN>) {
    my @ITEM = split(/ \|\|\| /);
    if (defined($last_f) && $ITEM[0] ne $last_f) {
	&finalize();
    }
    &add($ITEM[1],$ITEM[2]);
    $last_f = $ITEM[0];
}
&finalize();

sub add {
    my ($e,$scores) = @_;
    my ($p_f_given_e,$junk,$p_e_given_f,@MORE_JUNK) = split(/ /,$scores);
    if ($p_f_given_e > $THRESHOLD) {
	$P_F_GIVEN_E{$e} = $p_f_given_e;
    }
    if ($p_e_given_f > $THRESHOLD) {
	$P_E_GIVEN_F{$e} = $p_e_given_f;
    }
}

sub finalize {
    foreach my $e1 (keys %P_F_GIVEN_E) {
	foreach my $e2 (keys %P_E_GIVEN_F) {
	    print "$e1 ||| $e2 ||| ".($P_F_GIVEN_E{$e1} * $P_E_GIVEN_F{$e2})."\n";
	}
    }
    %P_F_GIVEN_E = ();
    %P_E_GIVEN_F = ();
}

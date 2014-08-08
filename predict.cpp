// Interactive Translatuion Prediction from Search Graphs
// originally implemented for Caitra by Philipp Koehn, 2009-2010
// adapted for Casmacat by Chara Tsoukala, 2013
// refinements by Philipp Koehn, 2014
 
#include <stdio.h>
#include <stdlib.h>
#include <vector>
#include <map>
#include <set>
#include <string>
#include <algorithm>
#include <sys/time.h>
// to read csv lines
#include <sstream>
#include <fstream>
// to lowercase
#include <cctype>
#include <iostream>

// header ////////

using namespace std;

typedef unsigned int Word; // words are stored as integers -- see add_to_lexicon
inline vector<Word> tokenize(const string& str, float score); // takes string, converts into vector of integers
string lowercase(string mixedcase);
bool equal_case_insensitive(string word1, string word2);

// transition (phrase translation) in decoder search graph
class Transition {
public:
	int to_state;
	float score;
	vector< Word > output;
	//Transition( int t, float s, char* o ) {
	Transition( int t, float s, string o, float path_score ) {
		to_state = t;
		score = s;
		output = tokenize( o, path_score );
	}
};

// state in prefix matching search
class BackTransition {
public:
	float score;
	int error;
	int prefix_matched;
  	int back_state;
	int back_matched;
	const Transition *transition;
	BackTransition( float s, int e, int pm, int b, int bm, const Transition *t ) {
		score = s;
		error = e;
		prefix_matched = pm;
		back_state = b;
		back_matched = bm;
		transition = t;
	}
};

// state in decoder search graph
class State {
public:
	int forward;
	float forward_score;
	float best_score;
	const Transition *best_transition;
	vector< Transition > transitions;
	vector< BackTransition > back;
	State( int f, float fs, float bs ) {
		forward = f;
		forward_score = fs;
		best_score = bs;
	}
};

// matching information in prefix matching search
class Match {
public:
	int error; // number of edits
	int prefixMatched; // number of user prefix words matched
	int transitionMatched; // number of words in last transition matched
	Match( int e, int p, int t ) {
		error = e;
		prefixMatched = p;
		transitionMatched = t;
	}
};

// stores the currently best completion
class Best {
public:
	int from_state;
	const Transition *transition;
	BackTransition *back;
	int output_matched;
	int back_matched;
	int prefix_matched;
	float score;
	Best() {
		from_state = -1;
	}
};

typedef vector< BackTransition >::iterator backIter;
typedef vector< Transition >::iterator transIter;
typedef vector< Match >::iterator matchIter;

void load_states_transitions ( float );
Word add_to_lexicon( string wordstring, float score );
int prefix_matching_search( float max_time, float threshold );
inline vector< Match > string_edit_distance( int, const vector< Word > & );
int letter_string_edit_distance( Word wordId1, Word wordId2b );

inline void process_match( int, const BackTransition &, const Match &, const Transition & );

// utils //////

double get_wall_time(){
	struct timeval time;
	if (gettimeofday(&time,NULL)){
        	//  Handle error
		return 0;
        }
        return (double)time.tv_sec + (double)time.tv_usec * .000001;
}

// globals ///////
map< string, Word > lexicon;
vector< float > word_score;
vector< string > surface;
vector< State > states;
vector<int> stateId2hypId;
map<int,int> hypId2stateId;
vector< Word > prefix;
Best best[1000];
bool last_word_may_match_partially, case_insensitive_matching, match_last_partial_word_desparately;
int error_unit;
float approximate_word_match_threshold;
int suffix_insensitive_min_match, suffix_insensitive_max_suffix;
int match_last_word_window;
set< Word > partially_matches_last_token;
set< pair< Word, Word > > approximate_word_match, lowercase_word_match, suffix_insensitive_word_match;
set< Word > already_processed;
FILE *log_in;

// main //////////

int main(int argc, char* argv[])
{
	float threshold = 999;
	float max_time = 0;
	last_word_may_match_partially = false;
	match_last_partial_word_desparately = false;
	error_unit = 1;
	case_insensitive_matching = false;
	suffix_insensitive_min_match = 0;
	suffix_insensitive_max_suffix = 0;
	approximate_word_match_threshold = 1.0;
	match_last_word_window = 0;
        string logfile_name;
	for (int i = 1; i < argc; ++i) {
		string arg = argv[i];
		if (arg == "-t" || arg.find("threshold") != string::npos) {
			if (i+1 == argc) {
				cerr << "ERROR: threshold switch -t without value\n";
				exit(1);
			}
			arg = argv[++i];
			threshold = atof( arg.c_str() );
		}
		else if (arg == "-l" || arg.find("logfile") != string::npos) {
			if (i+1 == argc) {
				cerr << "ERROR: logfile switch -l without file\n";
				exit(1);
			}
			logfile_name = argv[++i];
			log_in = fopen (logfile_name.c_str(), "w");
		}
		else if (arg == "-w" || arg.find("partial-word") != string::npos) {
			last_word_may_match_partially = true;
		}
		else if (arg == "-W") {
			match_last_partial_word_desparately = true;
			last_word_may_match_partially = true;
		}
		else if (arg == "-c" || arg.find("case-insensitive") != string::npos) {
			case_insensitive_matching = true;
		}
		else if (arg == "-a" || arg.find("approximate") != string::npos) {
			if (i+1 == argc) {
				cerr << "ERROR: approximate word match -a without threshold\n";
				exit(1);
			}
	 		approximate_word_match_threshold = atof(argv[++i]);
			error_unit = 2;
		}
		else if (arg == "-s" || arg.find("suffix-insensitive") != string::npos) {
			if (i+2 >= argc) {
				cerr << "ERROR: suffix insensitive switch -s without min stem length and max suffix length\n";
				exit(1);
			}
			suffix_insensitive_min_match = atoi(argv[++i]);
			suffix_insensitive_max_suffix = atoi(argv[++i]);
			error_unit = 2;
		}
		else if (arg == "-m" || arg.find("max-time") != string::npos) {
			if (i+1 == argc) {
				cerr << "ERROR: maximum time switch -m without time\n";
				exit(1);
			}
			max_time = atof(argv[++i]);
		}
		else if (arg == "-f" || arg.find("match-final") != string::npos) {
			if (i+1 == argc) {
				cerr << "ERROR: match final switch -f without window size\n";
				exit(1);
			}
			match_last_word_window = atoi(argv[++i]);
		}
	}

	// load the decoder search graph from stdin
	// this fills the golabl "states", transitions are attached to each state
	load_states_transitions( threshold );

	// process a prefix request at a time
	std::string line;
	int request_id = 0;
	while (std::getline(cin,line))
	{
		if (log_in) {
			fwrite(line.c_str(),1,line.size(),log_in);
			fputc((int)'\n', log_in);
			fflush(log_in);
		} 
	        double start_time = get_wall_time();
		// cerr << line << endl;
		// convert prefix string into our representation (vector of integers)
		bool prefix_has_final_space = (line[line.length()-1] == ' ');
		prefix = tokenize(line, 0);
		string last_token = surface[prefix[prefix.size()-1]];

		// allow partial matching of last token in prefix matching search
		if (last_word_may_match_partially && !prefix_has_final_space) {
			// also allow case-insensitive match
			string last_token_lowercase = lowercase(last_token);
		       	partially_matches_last_token.clear();
			// for all words in vocabulary
			for (map<string, Word>::iterator iter = lexicon.begin(); iter != lexicon.end(); iter++) {
				string word = lowercase(iter->first);
			        // check if could be a partial match
				if (last_token_lowercase.length() < word.length() &&
				    last_token_lowercase == word.substr(0,last_token.length())) {
					partially_matches_last_token.insert( iter->second );
				}
			}
		}

		// allow case-insensitive matching
		if (case_insensitive_matching) {
			// for all words in prefix
			for(int p=0; p<prefix.size(); p++) {
				if (already_processed.count( prefix[p] )) {
					continue;
				}
				// for all words in vocabulary
				for (map<string, Word>::iterator iter = lexicon.begin(); iter != lexicon.end(); iter++) {
					// if they match case-insensitive, take note
					if (equal_case_insensitive( surface[ prefix[p] ], iter->first )) {
						lowercase_word_match.insert( make_pair( prefix[p], iter->second ) );
					}
				}
			}
		}

		// consider mismatches of similarly spelled words as half an error
		if (approximate_word_match_threshold < 1.0) {
			// for all words in prefix
			for(int p=0; p<prefix.size(); p++) {
				if (already_processed.count( prefix[p] )) {
					continue;
				}
				int length_prefix_word = surface[ prefix[p] ].size();
				// for all words in vocabulary
				for (map<string, Word>::iterator iter = lexicon.begin(); iter != lexicon.end(); iter++) {
					int distance = letter_string_edit_distance( prefix[p], iter->second );
					int length_vocabulary_word = iter->first.size();
					int min_length = length_prefix_word < length_vocabulary_word ? length_prefix_word : length_vocabulary_word;
					if (distance <= min_length * approximate_word_match_threshold) {
						approximate_word_match.insert( make_pair( prefix[p], iter->second ) );
					}
				}
			}
		}

		// consider mismatches in word endings (presumably morphological variants) as half an error
		if (suffix_insensitive_max_suffix > 0) {
			// for all words in prefix
			for(int p=0; p<prefix.size(); p++) {
				if (already_processed.count( prefix[p] )) {
					continue;
				}
				// for all words in vocabulary
				int length_prefix_word = surface[ prefix[p] ].size();
				for (map<string, Word>::iterator iter = lexicon.begin(); iter != lexicon.end(); iter++) {
					int length_vocabulary_word = iter->first.size();
					if (abs(length_vocabulary_word-length_prefix_word) <= suffix_insensitive_max_suffix &&
					    length_prefix_word >= suffix_insensitive_min_match &&
					    length_vocabulary_word >= suffix_insensitive_min_match) {
						int specific_min_match = ( length_prefix_word > length_vocabulary_word ) ? length_prefix_word : length_vocabulary_word;
						specific_min_match -= suffix_insensitive_max_suffix;
						if (suffix_insensitive_min_match > specific_min_match) {
							specific_min_match = suffix_insensitive_min_match;
						}
						if (iter->first.substr(0,specific_min_match) ==
						      surface[ prefix[p] ].substr(0,specific_min_match)) {
							suffix_insensitive_word_match.insert( make_pair( prefix[p], iter->second ) );
						}
					}
				}
			}
		}

		// record seen words for caching pre-processing across requests
		if (case_insensitive_matching ||
		    approximate_word_match_threshold < 1.0 ||
		    suffix_insensitive_max_suffix > 0) {
			for(int p=0; p<prefix.size(); p++) {
				if (!already_processed.count( prefix[p] )) {
					already_processed.insert( prefix[p] );
				}
			}
		}
		cerr << "preparation took " << (get_wall_time() - start_time) << " seconds\n";

		// call the main search loop
		int errorAllowed = prefix_matching_search( max_time, 0 );
		if (max_time>0 && errorAllowed == -1) {
		       errorAllowed = prefix_matching_search( 0, 0.000001 );	
		}
	
		// we found the best completion, now construct suffix for output
		Best &b = best[errorAllowed];
		vector< Word > matchedPrefix, predictedSuffix;

		// add words from final prediction
		for(int i=b.output_matched-1;i>=0;i--) {
			matchedPrefix.push_back( b.transition->output[i] );
		}
		for(int i=b.output_matched;i<b.transition->output.size();i++) {
			predictedSuffix.push_back( b.transition->output[i] );
		}

		// add suffix words (best path forward)
		int suffixState = b.transition->to_state;
		while (states[suffixState].forward > 0) {
			Transition *transition = NULL;
			float best_score = -999;
			vector< Transition > &transitions = states[suffixState].transitions;
			for(int t=0; t<transitions.size(); t++) {
				if (transitions[t].to_state == states[suffixState].forward &&
				    transitions[t].score > best_score) {
					transition = &transitions[t];
					best_score = transition->score;
				}
			}
			for(int i=0;i<transition->output.size();i++) {
				predictedSuffix.push_back( transition->output[i] );
			}
			suffixState = states[suffixState].forward;
	 	}

		// add prefix words (following back transitions
		int prefixState = b.from_state;
		int prefix_matched = b.back_matched;
		while (prefixState > 0) {
			backIter back = states[prefixState].back.begin();
			for(; back != states[prefixState].back.end(); back++ ) {
				if (back->prefix_matched == prefix_matched) {
					break;
				}
			}
			const vector< Word > &output = back->transition->output;
			for(int i=output.size()-1; i>=0; i--) {
				matchedPrefix.push_back( output[i] );
			}
			back->transition->output.size();
			prefixState = back->back_state;
			prefix_matched = back->back_matched;
		}



		// handle final partial word (normal case)
		bool successful_partial_word_completion = false;
		if (!successful_partial_word_completion && last_word_may_match_partially && !prefix_has_final_space && matchedPrefix.size()>0) {
			Word last_matched_word = matchedPrefix[ 0 ];
			if (partially_matches_last_token.count( last_matched_word )) {
				cout << surface[ last_matched_word ].substr(last_token.length());
				successful_partial_word_completion = true;
			}
		}

		// try a bit harder to match the last word
		if (!successful_partial_word_completion && prefix.size()>0 && match_last_word_window>0 &&
		    (matchedPrefix.size() == 0 || matchedPrefix[ 0 ] != prefix[prefix.size()-1])) {
			// if we match it case-insensitive, that's okay
			bool is_okay = false;
			if (matchedPrefix.size()>0) {
				if (equal_case_insensitive(last_token, surface[ matchedPrefix[0] ])) {
					is_okay = true;
				}
			}
			// look for last word in window around current matched path position
			for(int i=0; !is_okay && i<=match_last_word_window; i++) {
				// is the word in the predicted suffix?
				if (predictedSuffix.size() > i &&
				    (equal_case_insensitive(last_token, surface[ predictedSuffix[i]]) ||
				     (last_word_may_match_partially && !prefix_has_final_space && partially_matches_last_token.count( predictedSuffix[i] )))) {
					// move predicted suffix words into matched prefix
					for(int j=0; j<=i; j++) {
						matchedPrefix.insert( matchedPrefix.begin(), predictedSuffix[0] );
						predictedSuffix.erase( predictedSuffix.begin() );
					}
					is_okay = true;
				}
				// is the word in the macthed prefix?
				else if (i>0 && matchedPrefix.size() > i &&
				         (equal_case_insensitive(last_token, surface[ matchedPrefix[i]]) ||
					  (last_word_may_match_partially && !prefix_has_final_space && partially_matches_last_token.count( matchedPrefix[i] )))) {
					// move matched prefix words into predicted suffix
					for(int j=0; j<i; j++) {
						predictedSuffix.insert( predictedSuffix.begin(), matchedPrefix[0] );
						matchedPrefix.erase( matchedPrefix.begin() );
					}
					is_okay = true;
				}	
			}
		}

		// desparation word completion: matching word with best path score
		if (match_last_partial_word_desparately && !prefix_has_final_space && !successful_partial_word_completion && word_score[ prefix[prefix.size()-1] ] == 0) {
			string best;
			float best_score = -9e9;
			bool best_case_sensitive = false;
			// for all words in vocabulary

			for (map<string, Word>::iterator iter = lexicon.begin(); iter != lexicon.end(); iter++) {
				// word known in different casing, use it
				if (equal_case_insensitive(iter->first, last_token) &&
				    word_score[ iter->second ] != 0) {
					best = iter->first;
					best_score = 0;
					break;
				}
				if (iter->first.length() >= last_token.length() &&
				    word_score[ iter->second ] != 0 &&
				    word_score[ iter->second ] > best_score &&
				    equal_case_insensitive(iter->first.substr(0,last_token.length()), last_token)) {
					// prefer case-sensitive match
					if (iter->first.substr(0,last_token.length()) == last_token) {
						best_case_sensitive = true;
						best = iter->first;
						best_score = word_score[ iter->second ];
					}
				        if (!best_case_sensitive) {
						best = iter->first;
						best_score = word_score[ iter->second ];
					}
				}
			}
			if (best_score > -8e9) {
				cout << best.substr(last_token.length());
				successful_partial_word_completion = true;
			}
		}

		//cerr << "predicted suffix:";
		//for( int i=0; i<predictedSuffix.size(); i++) {
		//	cerr << " " << surface[ predictedSuffix[i] ];
		//}
		//cerr << endl;
		//cerr << "matched prefix:";
		//for( int i=0; i<matchedPrefix.size(); i++) {
		//		cerr << " " << surface[ matchedPrefix[i] ];
		//}
		//cerr << endl;

		// output results
		for( int i=0; i<predictedSuffix.size(); i++) {
			if (i>0 || !prefix_has_final_space) { cout << " "; }
			cout << surface[ predictedSuffix[i] ];
		}

		// if no prediction, just output space
		if (predictedSuffix.size() == 0) {
			cout << " ";
		}
		cout << endl << flush;

		// clear out search
		for( int state = 0; state < states.size(); state++ ) {
			states[state].back.clear();
		}
		for (int errorAllowed = 0; errorAllowed < 1000; errorAllowed++ ) {
			best[errorAllowed].from_state = -1;
		}
		request_id++;
	 }
}	

// helper function to get BackTranstition that reached 
// * a particular state 
// * with a certain number of prefix words matched

// main search loop
int prefix_matching_search( float max_time, float threshold ) {		
	double start_time = get_wall_time();

	// intialize search - initial back transition (state in prefix matching search) 
	BackTransition initialBack( 0.0, 0, 0, -1, 0, NULL);
	// ... associated with initial hypothesis
	states[0].back.push_back( initialBack );
		
	// start search with maximum error 0, then increase maximum error one by one
	int errorAllowed = 0;
	while( errorAllowed <= prefix.size() * error_unit ) {
		// printf("error level %d\n",errorAllowed);
		// process decoder search graph, it is ordered, so we can just sequentially loop through states
		int valid_states = 0;
		int back_count = 0;
		int transition_count = 0;
		int match_count = 0;
		for( int state = 0; state < states.size(); state++ ) {
			// ignore state if it is too bad
			if (threshold > 0 && states[state].best_score < states[0].best_score+threshold) {
				continue;
			}
			valid_states++;
			// abort search if maximum time exceeded
			if (state % 100 == 0 && max_time > 0 && (get_wall_time()-start_time) > max_time) {
				return -1;
			}
			// if it has back transitions, it is reachable, so we have to process each
			for ( backIter back = states[state].back.begin(); back != states[state].back.end(); back++ ) {
				// only need to process back transitions with current error level
				// the ones with lower error have been processed in previous iteration
				if (back->error == errorAllowed) {
				back_count++;
					// loop through transitions out of this state
					for ( transIter transition = states[state].transitions.begin(); transition != states[state].transitions.end(); transition++ ) {
						if (threshold > 0 && states[transition->to_state].best_score < states[0].best_score+threshold) {
							continue;
						}
						transition_count++;

						// try to match this transition's phrase
						// starting at end word prefix position of previous back transition
						vector< Match > matches = string_edit_distance( back->prefix_matched, transition->output );
						// process all matches
						for ( matchIter match = matches.begin(); match != matches.end(); match++ ) {
							match_count++;
							// check if match leads to valid new back transition
							process_match( state, *back, *match, *transition );
						}
					} 
				}
			}
		}
		cerr << "explored " << valid_states << " valid states, " << back_count << " backs, " <<  transition_count << " transitions, " << match_count << " matches at error level " << errorAllowed << endl;
		// found a completion -> we are done
		if (best[errorAllowed].from_state != -1) {
			cerr << "search took " << (get_wall_time()-start_time) << " seconds.\n";
			return errorAllowed;
		}
		errorAllowed++;
	}
}

int letter_string_edit_distance( Word wordId1, Word wordId2 ) {
	string word1 = surface[ wordId1 ];
	string word2 = surface[ wordId2 ];
	int **cost = (int**) calloc( sizeof( int* ), word1.size() );
	for( int i=0; i<word1.size(); i++ ) {
		cost[i] = (int*) calloc( sizeof( int ), word2.size() );
		for( int j=0; j<word2.size(); j++ ) {
			if (i==0 && j==0) {
				cost[i][j] = 0;
			}
			else {
				cost[i][j] = 999;
				if (j>0 && cost[i][j-1]+1 < cost[i][j]) {
					cost[i][j] = cost[i][j-1]+1;
				}
				if (i>0 && cost[i-1][j]+1 < cost[i][j]) {
					cost[i][j] = cost[i-1][j]+1;
				}
				if (i>0 && j>0) {
					if (word1[i] != word2[j]) {
						if (cost[i-1][j-1]+1 < cost[i][j]) {
							cost[i][j] = cost[i-1][j-1]+1;
						}
					}
					else {
						if (cost[i-1][j-1] < cost[i][j]) {
							cost[i][j] = cost[i-1][j-1];
						}
					}
				}

			}
		}

	}
	int distance = cost[word1.size()-1][word2.size()-1];
	for( int i=0; i<word1.size(); i++ ) {
		free( cost[i] );
	}
	free( cost );
	return distance;
}


// match a phrase (output of a transition) against the prefix
// return a vector of matches with best error (multiple due to different number of prefix words matched)
inline vector< Match > string_edit_distance( int alreadyMatched, const vector< Word > &transition ) {
	vector< Match > matches;
	int toMatch = prefix.size() - alreadyMatched;
	int **cost = (int**) calloc( sizeof( int* ), toMatch+1 );

	//for( int j=1; j<=transition.size(); j++ )
		//printf("\t%s",surface[transition[ j-1 ]].c_str());
	for( int i=0; i<=toMatch; i++ ) {
		//if (i==0) printf("\n\t\t");
		//else printf("\n\t\t%s",surface[prefix[alreadyMatched+i-1]].c_str());
		cost[i] = (int*) calloc( sizeof(int), transition.size()+1 );
		for( int j=0; j<=transition.size(); j++ ) {
			if (i==0 && j==0) { // origin
				cost[i][j] = 0;
				//printf("\t0");
			}
			else {
				int lowestError = error_unit * (prefix.size()*2+2);

				if (i>0) { // deletion
					lowestError = cost[i-1][j] + error_unit;
				}
				if (j>0) { // insertion
					int thisError = cost[i][j-1] + error_unit;
					if (thisError < lowestError) {
						lowestError = thisError;
					}
				}
				if (i>0 && j>0) { // match or subsitution
					int thisError = cost[i-1][j-1];
					if (prefix[ alreadyMatched + i-1 ] != transition[ j-1 ]) {
						// mismatch -> substitution
						// ... unless partially matching last prefix token
						if (! (last_word_may_match_partially &&
						       alreadyMatched + i-1 == prefix.size()-1 &&
						       partially_matches_last_token.count( transition[ j-1 ] )) &&
						// ... and unless allowing case-insensitive matching
						    ! (case_insensitive_matching &&
						       lowercase_word_match.count( make_pair( prefix[ alreadyMatched + i-1 ], transition[ j-1 ] )))) {
							// if allowing approximate matching, count as half an error
							if ((approximate_word_match_threshold > 0.0 &&
							     approximate_word_match.count( make_pair( prefix[ alreadyMatched + i-1 ], transition[ j-1 ] ))) ||
							    (suffix_insensitive_max_suffix > 0 &&
							     suffix_insensitive_word_match.count( make_pair( prefix[ alreadyMatched + i-1 ], transition[ j-1 ] )))) {
								thisError += 1;
							}
							else {
								// really is a mismatch
								thisError += error_unit;
							}
						}
					}
					if (thisError < lowestError) {
						lowestError = thisError;
					}
				}
				cost[i][j] = lowestError;
				//printf("\t%d",lowestError);
			}
		}
	}
	
	// matches that consumed the prefix
	for(int j=1; j<transition.size(); j++ ) {
		Match newMatch( cost[toMatch][j], prefix.size(), j );
		matches.push_back( newMatch );	
	}

	// matches that consumed the transition	
	for(int i=1; i<=toMatch; i++ ) {
		Match newMatch( cost[i][transition.size()], alreadyMatched + i, transition.size() );
		matches.push_back( newMatch );
	}

	for( int i=0; i<=toMatch; i++ ) {
		free( cost[i] );
	}
	free( cost );
	
	return matches;
}

// given a back transition (state in the prefix matching search)
// and match (information how it can be extended with matching a transition)
// create a new back transition (new state in the prefix matching search)
inline void process_match( int state, const BackTransition &back, const Match &match, const Transition &transition ) {
	int transition_to_state = transition.to_state;
	float score = back.score + transition.score;
	int error = back.error + match.error;
	// common case: prefix is not yet fully matched
	if (match.prefixMatched < prefix.size() ) {
		// check how this new back transition compares against existing ones
		for( backIter oldBack = states[transition_to_state].back.begin(); oldBack != states[transition_to_state].back.end(); oldBack++ ) {
			if (oldBack->prefix_matched == match.prefixMatched) { // already a back path with same prefix match?
				// if better, overwrite
				if (oldBack->error > error ||
				     (oldBack->error == error && (oldBack->score < score ||
				       (oldBack->score == score && oldBack->back_matched < match.prefixMatched)))) {
					oldBack->error = error;
					oldBack->score = score;
					oldBack->back_state = state;
					oldBack->back_matched = back.prefix_matched;
					oldBack->prefix_matched = match.prefixMatched;
					oldBack->transition = &transition;
					// cerr << "\t\t\toverwriting\n";
				} 
				// if worse, ignore
				// done in any case
				return;
			}
		}
		// not recombinable with existing back translation -> just add it
		BackTransition newBack( score, error, match.prefixMatched, state, back.prefix_matched, &transition );
		states[transition_to_state].back.push_back( newBack );
		//cerr << "\t\t\tadding\n";
	}
	// special case: all of the prefix is consumed
	else {
		// add score to complete path
		score += states[transition_to_state].forward_score;
		// first completion ... or ... better than currently best completion?
		if ( best[error].from_state == -1 || 
		     score > best[error].score ||
		     (score == best[error].score && match.prefixMatched > best[error].prefix_matched) ) {
			best[error].score = score;
			best[error].from_state = state;
			best[error].transition = &transition;
			best[error].output_matched = match.transitionMatched;
			best[error].back_matched = back.prefix_matched;
			best[error].prefix_matched = match.prefixMatched;
			//cerr << "\t\t\tnew best\n";
		}
	}
}

// functions //////
Word add_to_lexicon( string wordstring, float score ) {
	map<string, Word>::iterator lookup = lexicon.find( wordstring );
	if (lookup != lexicon.end()) {
		if (score > 0 && word_score[ lookup->second ] > score) {
			word_score[ lookup->second ] = score;
		}
		return lookup->second;
	}
	// printf("[%d:%d:%s]",surface.size(),lexicon.size(),wordstring.c_str());
	lexicon[ wordstring ] = surface.size();
	word_score.push_back( score );
	surface.push_back( wordstring );
	return lexicon.size()-1;
}

inline vector<Word> tokenize(const string& str, float score)
{
	const string& delimiters = " \t\n\r";
	vector<Word> tokens;
	// Skip delimiters at beginning.
	string::size_type lastPos = str.find_first_not_of(delimiters, 0);
	// Find first "non-delimiter".
	string::size_type pos     = str.find_first_of(delimiters, lastPos);

	while (string::npos != pos || string::npos != lastPos)
	{
		// Found a token, add it to the vector.
		tokens.push_back(add_to_lexicon(str.substr(lastPos, pos - lastPos), score));
		// Skip delimiters.  Note the "not_of"
		lastPos = str.find_first_not_of(delimiters, pos);
		// Find next "non-delimiter"
		pos = str.find_first_of(delimiters, lastPos);
	}
	return tokens;
}

string lowercase(string mixedcase) {
	transform(mixedcase.begin(), mixedcase.end(), mixedcase.begin(), ::tolower);
	return mixedcase;
}

bool equal_case_insensitive(string word1, string word2) {
	transform(word1.begin(), word1.end(), word1.begin(), ::tolower);
	transform(word2.begin(), word2.end(), word2.begin(), ::tolower);
	return word1 == word2;
}

void load_states_transitions( float threshold ){ 
	float best_path_score;
	std::string line;
	int recombined;
	int thisKey;
	float forward_score; // score of best path to completion
	float backward_score;    // score of best path to this state
	float transition_score; // score of this transition
	int forward;
	int from_state;
	int to_state;
	int hyp;
	float ignore;
	char comma;
	map< int, int> recombination;

	std::getline(cin,line); // skip headers (find efficient alt)
	if (log_in) {
		fwrite(line.c_str(),1,line.size(),log_in);
		fputc((int)'\n', log_in);
	} 
	int i=0;
 		
	while (line.find("ENDSG") != 0)
	{
		std::getline(cin,line);
		if (log_in) {
			fwrite(line.c_str(),1,line.size(),log_in);
			fputc((int)'\n', log_in);
		} 
		std::string out;
		istringstream ss(line);
		
		// initial state does have no transition
		if (i==0) {
			ss >> ignore >> comma >> ignore >> comma >> ignore >> comma >> ignore >> comma >> ignore >> comma >> forward >> comma >> forward_score;
			best_path_score = forward_score; // but contains the best path score
			State newState( forward, forward_score, forward_score );
			states.push_back( newState );
		        stateId2hypId.push_back( hyp );
		}
		else {
		  ss >> hyp >> comma >> ignore >> comma >> from_state >> comma >> backward_score >> comma >> transition_score >> comma >> recombined >> comma >> forward >> comma >>  forward_score;
		  // output may be in quotes			
		  int pos = line.find('"');
		  if (pos > 0){
			line = line.substr(pos + 1);
			out = line.substr(0, line.size() - 1);
		  } 
		  // or is just the last item
		  else {
				pos = line.find_last_of(',') + 1;     
				out = line.substr(pos);	
		  }

		  // if not within threshold of best path score
		  if (backward_score + forward_score + threshold < best_path_score) {
			  // just record words
			  tokenize( out, backward_score + forward_score );
		  }
		  // ... otherwise add the state
		  else {
			if (recombined >= 0) {
				recombination[ hyp ] = recombined;
				to_state = recombined;
			}
			else {
				to_state = hyp;
			}
			Transition newTransition( to_state, transition_score, out, backward_score + forward_score);
			thisKey = hypId2stateId[ from_state ];
			states[ thisKey ].transitions.push_back( newTransition );
			if (recombined == -1) {
				State newState( forward, forward_score, backward_score+forward_score );
				states.push_back( newState );
				hypId2stateId[ hyp ] = stateId2hypId.size();
		        	stateId2hypId.push_back( hyp );
			}
		   }
		} 
		i++;
	}
	// renumber from hypothesis ids (contained in search graph) to state ids (consecutive)
	for(int state=0; state<states.size(); state++) {
		int forward = states[state].forward;
		if (recombination.count(forward)) {
			forward = recombination[ forward ];
		}
		states[state].forward = hypId2stateId[ forward ];
		for ( transIter transition = states[state].transitions.begin(); transition != states[state].transitions.end(); transition++ ) {
			transition->to_state = hypId2stateId[ transition->to_state ];
		}
	}
	cerr << "graph has " << states.size() << " states, pruned down from " << i << endl;
}	

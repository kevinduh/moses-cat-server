#include <stdio.h>
#include <stdlib.h>
#include <vector>
#include <map>
#include <string>
#include <algorithm>
// to read csv lines
#include <sstream>
#include <fstream>
// to lower
#include <cctype>
#include<iostream>
// header ////////

using namespace std;

typedef unsigned int Word;
inline vector<Word> tokenize(const string& str);

class Transition {
public:
	int to_state;
	float score;
	vector< Word > output;
	//Transition( int t, float s, char* o ) {
	Transition( int t, float s, string o ) {
		to_state = t;
		score = s;
		output = tokenize( o );
	}
};

class BackTransition {
public:
	float score;
	int error;
	int matched;
	int back_state;
	int back_matched;
	// to print output
	vector< Word > output;
	BackTransition( float s, int e, int m, int b, int bm, vector< Word > o ) {
		score = s;
		error = e;
		matched = m;
		back_state = b;
		back_matched = bm;
		output = o;
	}
};

class State {
public:
	int forward;
	float forward_score;
	vector< Transition > transitions;
	vector< BackTransition > back;
	// forward prediction
	vector<Word> foutput;
	State( int f, float fs, string o ) {
		forward = f;
		forward_score = fs;
		foutput = tokenize( o );
	}
};

class Match {
public:
	int error;
	int prefixMatched;
	int transitionMatched;
	Match( int e, int p, int t ) {
		error = e;
		prefixMatched = p;
		transitionMatched = t;
	}
};

class Best {
public:
	int state;
	int transition;
	int partialMatch;
	int back_state;
	int back_matched;
	float score;
    vector< Word > partialOutput;
	Best() {
		state = -1;
	}
};

typedef vector< BackTransition >::iterator backIter;
typedef vector< Transition >::iterator transIter;
typedef vector< Match >::iterator matchIter;

void load_states_transitions ();
Word add_to_lexicon( string wordstring );
inline vector< Match > string_edit_distance( int, const vector< Word > & );

inline int getStatePosition(std::vector<int>& vec, size_t size, int c)
 {
	for (size_t i = 0; i < size; i++)
	{
		if (vec[i] == c)
			return (int)i;
	}
    return 0; // ERROR
 }

inline void processMatch( int, const BackTransition &, const Match &, const Transition & );

// globals ///////
map< string, Word > lexicon;
vector< string > surface;
vector< State > states;
vector<int> hypKey; // added to keep position of hyp
vector< Word > prefix;
Best best[1000];

// main //////////

int main()
{
	load_states_transitions();
	std::string line;
	while (std::getline(cin,line))
	{
		prefix = tokenize(line);
		Word last_token = prefix[prefix.size()-1];
		vector<Word> n;
		BackTransition initialBack( 0.0, 0, 0, -1, 0, n);
		states[0].back.push_back( initialBack );
		int errorAllowed = 0;
		
		while( errorAllowed <= prefix.size() ) {
			// printf("error level %d\n",errorAllowed);
			for( int state = 0; state < states.size(); state++ ) {
				for ( backIter back = states[state].back.begin(); back != states[state].back.end(); back++ ) {
					if (back->error == errorAllowed) {
						for ( transIter transition = states[state].transitions.begin(); transition != states[state].transitions.end(); transition++ ) {
							// printf("\ttransition to %d\n",transition->to_state);
							vector< Match > matches = string_edit_distance( back->matched, transition->output );
							for ( matchIter match = matches.begin(); match != matches.end(); match++ ) {
								// printf("\t\tmatching prefix %d/%d transition %d/%d error %d\n",match->prefixMatched,prefix.size(),match->transitionMatched,transition->output.size(),match->error);
								processMatch( state, *back, *match, *transition );
							}
						} 
					}
				}
			}
			if (best[errorAllowed].state != -1) {
				break;
			}
			errorAllowed++;
		}
		
		vector<string> matchedOutput;
		Best &b = best[errorAllowed];
		int nextState = 1;
		if (b.transition > 0) {
			nextState = b.transition;
		} else if (b.state > 0) {
			nextState = b.state;
		} else if (b.back_state > 0 )
		{
			nextState = getStatePosition(hypKey, hypKey.size(), states[b.back_state].forward); 
		}
		string partialMatch;
		
		bool firstIter = true;
		vector <string> frwOutput;
		while (nextState  > 0)
		{	
			if (firstIter)  //start output from 2nd state
			{
				firstIter = false;		
				for(int i=0;i<states[nextState].foutput.size();i++) 
				{
					partialMatch  += surface[states[nextState].foutput[i]] + ' ';
				}
			} else { 
				for(int i=0;i<states[nextState].foutput.size();i++) 
				{
					frwOutput.push_back( surface[ states[nextState].foutput[i]] );
				}		
			}
			nextState = getStatePosition(hypKey, hypKey.size(), states[nextState].forward);
		}
		// match last token to the partial match or first predicted word
		string lastUserToken = ' '+ surface[last_token]; // add word boundary to the left
		// prepare case insensitive search
		std::transform(lastUserToken.begin(), lastUserToken.end(), lastUserToken.begin(), ::tolower);
		string partialMatchToLower = ' ' + partialMatch;
		std::transform(partialMatchToLower.begin(), partialMatchToLower.end(), partialMatchToLower.begin(), ::tolower);
		/*cout << "##" << partialMatchToLower << "##";
		cout << "##" << lastUserToken << "##";*/
		bool continueSearching = true;
		unsigned found = partialMatchToLower.find(lastUserToken);
		if ( found != std::string::npos && found < partialMatchToLower.size()) 
		{
			continueSearching = false;
			printf("%s",partialMatch.substr(found+ lastUserToken.size() -1).c_str()); // partialMatch is case sensitive (1 is because of the extra space)
		} 
		
		int initIt = 0;
		if (continueSearching) // check the first next state output as well
		{
			if (frwOutput.size() > 1){ 
				partialMatch = frwOutput[0];
				string partialMatchToLower = ' ' + partialMatch;
				std::transform(partialMatchToLower.begin(), partialMatchToLower.end(), partialMatchToLower.begin(), ::tolower);
				//cout << "##" << partialMatchToLower << "##";
				unsigned found = partialMatchToLower.find(lastUserToken);
				if ( found != std::string::npos && found < partialMatchToLower.size()) 
				{
					initIt = 1; // do not print it again
					printf("%s",partialMatch.substr(found+ lastUserToken.size()-1).c_str()); // (1 is because of the extra space)
				} 
			}
		}
		//print rest of prediction
		for(int it = initIt ; it != frwOutput.size(); ++it)
		{
			printf(" %s",frwOutput[it].c_str());
		} 
		printf("\n");
		std::cout.flush();
	}
}	

inline vector< Match > string_edit_distance( int alreadyMatched, const vector< Word > &transition ) {
	vector< Match > matches;
	int toMatch = prefix.size() - alreadyMatched;
	int **cost = (int**) calloc( sizeof( int* ), toMatch+1 );

	//for( int j=1; j<=transition.size(); j++ )
		//printf("\t%s",surface[transition[ j-1 ]].c_str());
	for( int i=0; i<=toMatch; i++ ) {
		//if (i==0) printf("\n\t\t");
		//else printf("\n\t\t%s",surface[prefix[alreadyMatched+i-1]].c_str());
		cost[i] = (int*) calloc( sizeof(float), transition.size()+1 );
		for( int j=0; j<=transition.size(); j++ ) {
			if (i==0 && j==0) { // origin
				cost[i][j] = 0;
				//printf("\t0");
			}
			else {
				int lowestError = prefix.size()*2+2;

				if (i>0) { // deletion
					lowestError = cost[i-1][j] + 1;
				}
				if (j>0) { // insertion
					int thisError = cost[i][j-1] + 1;
					if (thisError < lowestError) {
						lowestError = thisError;
					}
				}
				if (i>0 && j>0) { // match or subsitution
					int thisError = cost[i-1][j-1];
					if (prefix[ alreadyMatched + i-1 ] != transition[ j-1 ]) {
						thisError++; // mismatch -> substitution
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
	
	return matches;
}

inline void processMatch( int state, const BackTransition &back, const Match &match, const Transition &transition ) {
	int transition_to_state = getStatePosition(hypKey, hypKey.size(),transition.to_state);
	if (match.prefixMatched < prefix.size() ) {
		// not done yet
		float score = back.score + transition.score;
		int error = back.error + match.error;
		int matched = match.prefixMatched;
		for( backIter oldBack = states[transition_to_state].back.begin(); oldBack != states[transition_to_state].back.end(); oldBack++ ) {
			if (oldBack->matched == matched) { // already a back path with same prefix match?
				if (oldBack->error > error || // if better, overwrite
						(oldBack->error == error && oldBack->score < score)) {
					oldBack->error = error;
					oldBack->score = score;
					oldBack->back_state = state;
					oldBack->back_matched = back.matched;
					// printf("\t\t\toverwriting\n");
				} // else: ignore
				return;
			}
		}
		BackTransition newBack( score, error, matched, state, back.matched, transition.output );
		states[transition_to_state].back.push_back( newBack );
		// printf("\t\t\tadding\n");
	}
	else {
	// new best path?
		int error = back.error + match.error;
		float thisScore = back.score + transition.score + states[transition_to_state].forward_score;
		if ( best[error].state == -1 || thisScore > best[error].score ) {
			// printf("\t\tNEW BEST THING (%f) at error level %d\n",thisScore,error);
			// partial matching last transition?
			if ( match.transitionMatched < transition.output.size() ) {
				best[error].transition = transition_to_state;
				best[error].partialMatch = match.transitionMatched;
				best[error].partialOutput = transition.output;
				best[error].state = state;
			}
			// full match
			else {
				best[error].transition = -1;
				best[error].state = transition_to_state;
			}
			best[error].score = thisScore;
			best[error].back_state = state;
			best[error].back_matched = back.matched;
		}
	}
}

// functions //////
Word add_to_lexicon( string wordstring ) {
	//transform(wordstring.begin(), wordstring.end(), wordstring.begin(), (int(*)(int))std::tolower);
	map<string, Word>::iterator lookup = lexicon.find( wordstring );
	if (lookup != lexicon.end()) 
		return lookup->second;
	// printf("[%d:%d:%s]",surface.size(),lexicon.size(),wordstring.c_str());
	lexicon[ wordstring ] = surface.size();
	surface.push_back( wordstring );
	return lexicon.size()-1;
}

inline vector<Word> tokenize(const string& str)
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
		tokens.push_back(add_to_lexicon(str.substr(lastPos, pos - lastPos)));
		// Skip delimiters.  Note the "not_of"
		lastPos = str.find_first_not_of(delimiters, pos);
		// Find next "non-delimiter"
		pos = str.find_first_of(delimiters, lastPos);
	}
	return tokens;
}

void load_states_transitions(){ 
	std::string line;
	int recombined;
	int thisKey;
	float forward_score;
	float score;
	int forward;
	int from_state;
	int to_state;
	int hyp;
	float ignore;
	char comma;

	std::getline(cin,line); // skip headers (find efficient alt)
	bool skip = true;
 		
	while (line.find("ENDSG") != 0)
	{
		std::getline(cin,line);
		std::string out;
		istringstream ss(line);
		ss >> hyp >> comma >> ignore >> comma >> from_state >> comma >> ignore >> comma >> score >> comma >> recombined >> comma >> forward >> comma >>  forward_score;
		
		hypKey.push_back( hyp );
		// from state: back, //to state: defined($recombined) ? $recombined : $hyp, "score" => $transition, output = out
		if (skip) {//first iter
			skip = false;
		}
		else 
		{
			/// if output is in quotes			
			int pos = line.find('"');
			if (pos > 0){
				line = line.substr(pos + 1);
				out = line.substr(0, line.size() - 1);
			} else {
				pos = line.find_last_of(',') + 1;     
				out = line.substr(pos);	
			}
			
			to_state = (recombined == -1) ? hyp : recombined;
			Transition newTransition( to_state, score, out);
			thisKey  = getStatePosition(hypKey, hypKey.size(), from_state);
			states[ thisKey ].transitions.push_back( newTransition );
		} 
		State newState( forward, forward_score, out );
		states.push_back( newState );
	}
}	
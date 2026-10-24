# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                             #
#   OpenBench is a chess engine testing framework authored by Andrew Grant.   #
#   <https://github.com/AndyGrant/OpenBench>           <andrew@grantnet.us>   #
#                                                                             #
#   OpenBench is free software: you can redistribute it and/or modify         #
#   it under the terms of the GNU General Public License as published by      #
#   the Free Software Foundation, either version 3 of the License, or         #
#   (at your option) any later version.                                       #
#                                                                             #
#   OpenBench is distributed in the hope that it will be useful,              #
#   but WITHOUT ANY WARRANTY; without even the implied warranty of            #
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the             #
#   GNU General Public License for more details.                              #
#                                                                             #
#   You should have received a copy of the GNU General Public License         #
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.     #
#                                                                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

# Conditions of IF Nodes, evaluated against the results of finished SPRTs:
#
#   >>> evaluate('failed && elo > 3', { None : { 'passed' : False, 'failed' : True, 'elo' : 3.4, ... } })
#   True
#   >>> evaluate('x.passed && y.elo > 2', { 'x' : {...}, 'y' : {...} })
#
# A plain variable refers to the IF's own SPRT (None), a variable prefixed with
# a Node key to that Node's SPRT. references() tells which results are needed.
#
# The grammar is deliberately tiny, and parsed by hand, so that nothing a user
# types is ever handed to Python's eval(). Types are checked while parsing, so
# a condition that parses can always be evaluated.
#
#   or      := and   ( ( '||' | 'or'  ) and )*
#   and     := not   ( ( '&&' | 'and' ) not )*
#   not     := ( '!' | 'not' ) not  |  compare
#   compare := operand ( ( '<' | '<=' | '>' | '>=' | '==' | '!=' ) operand )?
#   operand := number  |  '-' operand  |  [ key '.' ] variable  |  '(' or ')'

import functools
import operator
import re

# Variables of an SPRT result, and whether each is a boolean or a number
VARIABLES = {
    'passed'    : bool,  'failed'    : bool,
    'elo'       : float, 'elo_lower' : float, 'elo_upper' : float,
    'llr'       : float, 'games'     : float,
}

COMPARISONS = {
    '<'  : operator.lt, '<=' : operator.le, '>'  : operator.gt,
    '>=' : operator.ge, '==' : operator.eq, '!=' : operator.ne,
}

ALIASES = { 'and' : '&&', 'or' : '||', 'not' : '!' }

# A qualified variable is tried first, so that keys may start with a digit, or contain '-'
TOKEN_PATTERN = re.compile(r'\s*(?:([A-Za-z0-9_-]{1,32})\.([A-Za-z_]+)|(\d+(?:\.\d+)?)|([A-Za-z_]+)|(&&|\|\||<=|>=|==|!=|[<>!()\-]))')

MAX_LENGTH = 256
MAX_DEPTH  = 32

class ConditionError(Exception):
    pass

def check_variable(name):
    if name not in VARIABLES:
        raise ConditionError('Unknown variable "%s", expected one of %s' % (name, ', '.join(VARIABLES)))

def tokenize(text):

    tokens, position, text = [], 0, text.rstrip()

    while position < len(text):

        if not (match := TOKEN_PATTERN.match(text, position)):
            raise ConditionError('Unexpected "%s"' % (text[position:].strip()[:16]))

        key, qualified, number, word, symbol = match.groups()
        position = match.end()

        if qualified:
            check_variable(qualified)
            tokens.append(('var', (key, qualified)))

        elif number:
            tokens.append(('number', float(number)))

        elif word and word.lower() in ALIASES:
            tokens.append(('op', ALIASES[word.lower()]))

        elif word:
            check_variable(word)
            tokens.append(('var', (None, word)))

        else:
            tokens.append(('op', symbol))

    return tokens

class Parser:

    def __init__(self, tokens):
        self.tokens = tokens
        self.index  = 0
        self.depth  = 0

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else (None, None)

    def accept(self, *ops):
        kind, value = self.peek()
        if kind == 'op' and value in ops:
            self.index += 1
            return value

    def expect_type(self, node, wanted, context):
        if node[1] != wanted:
            names = { bool : 'a condition', float : 'a number' }
            raise ConditionError('%s needs %s' % (context, names[wanted]))
        return node

    # Every node of the tree is ( function of the variables, type )

    def parse_or(self):

        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ConditionError('Condition is nested too deeply')

        left = self.parse_and()
        while self.accept('||'):
            right = self.expect_type(self.parse_and(), bool, '||')
            self.expect_type(left, bool, '||')
            left = (lambda v, a=left[0], b=right[0]: a(v) or b(v), bool)

        self.depth -= 1
        return left

    def parse_and(self):

        left = self.parse_not()
        while self.accept('&&'):
            right = self.expect_type(self.parse_not(), bool, '&&')
            self.expect_type(left, bool, '&&')
            left = (lambda v, a=left[0], b=right[0]: a(v) and b(v), bool)

        return left

    def parse_not(self):

        if self.accept('!'):
            inner = self.expect_type(self.parse_not(), bool, '!')
            return (lambda v, a=inner[0]: not a(v), bool)

        return self.parse_compare()

    def parse_compare(self):

        left = self.parse_operand()

        kind, value = self.peek()
        if kind == 'op' and value in COMPARISONS:
            self.index += 1
            right = self.expect_type(self.parse_operand(), float, value)
            self.expect_type(left, float, value)
            return (lambda v, a=left[0], b=right[0], f=COMPARISONS[value]: f(a(v), b(v)), bool)

        return left

    def parse_operand(self):

        kind, value = self.peek()

        if kind == 'number':
            self.index += 1
            return (lambda v, x=value: x, float)

        if kind == 'var':
            self.index += 1
            return (lambda v, key=value[0], name=value[1]: v[key][name], VARIABLES[value[1]])

        if self.accept('-'):
            inner = self.expect_type(self.parse_operand(), float, '-')
            return (lambda v, a=inner[0]: -a(v), float)

        if self.accept('('):
            inner = self.parse_or()
            if not self.accept(')'):
                raise ConditionError('Missing ")"')
            return inner

        raise ConditionError('Unexpected end of condition' if kind is None else 'Unexpected "%s"' % (value))

def parse(text):

    # Returns a function of the results, or raises a ConditionError
    text = str(text or '')

    if not text.strip():
        raise ConditionError('Condition is empty')

    if len(text) > MAX_LENGTH:
        raise ConditionError('Condition may have at most %d characters' % (MAX_LENGTH))

    parser = Parser(tokenize(text))
    tree   = parser.parse_or()

    if parser.index != len(parser.tokens):
        raise ConditionError('Unexpected "%s"' % (parser.tokens[parser.index][1]))

    if tree[1] != bool:
        raise ConditionError('Condition must be true or false, like "elo > 3"')

    return tree[0]

def references(text):
    return parsed_references(str(text or ''))

@functools.lru_cache(maxsize=4096)
def parsed_references(text):

    # Keys of the SPRTs whose results the condition needs, with None for the IF's own.
    # Cached, since every step of the watcher asks for them many times. Errors are not cached.
    parse(text)
    return frozenset(value[0] for kind, value in tokenize(text) if kind == 'var')

def evaluate(text, results):

    # results maps None and keys to the variables of an SPRT, as in VARIABLES
    return bool(parse(text)(results))

def describe(variables):

    # Shown on the IF Node, so that it is clear why it decided as it did
    return '%s, Elo %+.2f [%+.2f, %+.2f], LLR %.2f, %d games' % (
        'passed' if variables['passed'] else 'failed' if variables['failed'] else 'undecided',
        variables['elo'], variables['elo_lower'], variables['elo_upper'], variables['llr'], variables['games'])

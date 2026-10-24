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

# UCI option strings, as stored in Test.dev_options and Test.base_options

import re

# Name=Value, where Value may be quoted to contain spaces, as in extract_option()
OPTION_PATTERN = re.compile(r'''(\S+?)=("[^"]*"|'[^']*'|\S*)''')

def parse_options(options):
    return [match.groups() for match in OPTION_PATTERN.finditer(options)]

def merge_options(options, overrides):

    # Overrides replace options of the same name, and new ones are appended.
    # UCI option names are case-insensitive, so Hash= also replaces hash=
    if not overrides.strip():
        return options

    merged = {}
    for name, value in parse_options(options) + parse_options(overrides):
        merged[name.lower()] = (name, value)

    return ' '.join('%s=%s' % (name, value) for name, value in merged.values())

def is_option_string(options):

    # Nothing but Name=Value pairs may be given
    return not OPTION_PATTERN.sub('', options).strip()

def estimate_tuned_options(spsa_inputs):

    # Longest Name=Value string an SPSA could produce, without knowing the tuned values
    try:
        options = []
        for line in spsa_inputs.splitlines():
            name, dtype, value, minimum, maximum = map(str.strip, line.split(',')[:5])
            width = 24 if dtype == 'float' else max(len(str(int(float(x)))) for x in (minimum, maximum))
            options.append('%s=%s' % (name, '9' * width))
        return ' '.join(options)

    except: return '' # Malformed SPSA inputs are reported by verify_spsa_inputs()

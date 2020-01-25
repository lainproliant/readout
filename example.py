#!/usr/bin/env python
# --------------------------------------------------------------------
# example.py
#
# Author: Lain Musgrove (lain.proliant@gmail.com)
# Date: Saturday January 18, 2020
#
# Distributed under terms of the MIT license.
# --------------------------------------------------------------------
from readout import sensor, gauge, when, state, start, sh

# --------------------------------------------------------------------
# Predicate syntax legend:
#
# `a>1`, `a<1`, `a>=1`, `a<=1`: gauge predicate
# `a=1`, `a=blue`: sensor predicate
# `a@b`: state predicate, a enters b state
# `a@b->c`: state predicate, a enters c state from b state
# `a@b->`: state predicate, a exits b state to any other state
#
# --------------------------------------------------------------------

# --------------------------------------------------------------------
@sensor()
def load():
    return 'mew'


# --------------------------------------------------------------------
@gauge(freq=5)
async def temp():
    return int(await sh("cat /sys/class/thermal/thermal_zone0/temp")) / 1000


# --------------------------------------------------------------------
@when('temp<=60', 'load=mew')
@state('power@cool')
def on_low_demand():
    print("Low demand mode activated.")


# --------------------------------------------------------------------
@when('power@hot->')
def on_cooldown():
    print("It's not hot anymore!")


# --------------------------------------------------------------------
@when('load = mew')
def i_love_kitty():
    print("I love Jenna")


# --------------------------------------------------------------------
@when('temp > 60')
@state('power@cool->hot')
def on_hot():
    print("It was cool, now it's hot!")


# --------------------------------------------------------------------
start()

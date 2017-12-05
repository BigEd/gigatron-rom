#!/usr/bin/env python
#-----------------------------------------------------------------------
#
#  Core video, sound and interpreter loop for Gigatron TTL color computer
#  - 6.25MHz clock
#  - Rendering 160x120 pixels at 6.25MHz with flexible videoline programming
#  - Must stay above 31 kHz horizontal sync --> 200 cycles/scanline
#  - Must stay above 59.94 Hz vertical sync --> 521 scanlines/frame
#  - 4 channels sound
#  TODO: add screen font
#  TODO: add date/time clock
#
#-----------------------------------------------------------------------

from asm import *
import gcl
import font

# Output pin assignment for VGA
R, G, B, hSync, vSync = 1, 4, 16, 64, 128
syncBits = hSync+vSync # Both pulses negative

# When the XOUT register is in the circuit, the rising edge triggers its update.
# The loop can therefore not be agnostic to the horizontal pulse polarity.
assert(syncBits & hSync != 0)

# VGA defaults
vFront = 10     # Vertical front porch
vPulse = 2      # Vertical sync pulse
vBack = 33      # Vertical back porch
vgaLines = vFront + vPulse + vBack + 480
vgaClock = 25.175e6

# Adjustments for our system:
# 1. Get refresh rate back above minimum 59.94 Hz by cutting lines from vertical front porch
vFrontAdjust = vgaLines - int(4 * 6.25e6 / vgaClock * vgaLines)
vFront -= vFrontAdjust
# 2. Extend vertical sync pulse so we can feed the game controller the same signal
vPulseExtension = max(0, 8-vPulse)
vPulse += vPulseExtension
# 3. Borrow these lines from the back porch so the refresh rate is unaffected
vBack -= vPulseExtension

# Game controller bits
buttonRight     = 1
buttonLeft      = 2
buttonDown      = 4
buttonUp        = 8
buttonStart     = 16
buttonSelect    = 32
buttonA         = 64
buttonB         = 128

#-----------------------------------------------------------------------
#
#  RAM page 0: variables
#
#-----------------------------------------------------------------------

# Memory size in pages from auto-detect
memSize = zpByte()

# The current channel number for sound generation. Advanced every scanline
# and independent of the vertical refresh to maintain constant oscillation.
channel = zpByte()

# Next sound sample being synthesized
sample = zpByte()
# To save one instruction in the critical inner loop, `sample' is always
# reset with its own address instead of, for example, the value 0. Compare:
# 1 instruction reset
#       st sample,[sample]
# 2 instruction reset:
#       ld 0
#       st [sample]
# The difference is unhearable. This is fine when the reset/address
# value is low and doesn't overflow with 4 channels added to it.
# There is an alternative, but it requires pull-down diodes on the data bus:
#       st [sample],[sample]
assert 4*63 + sample < 256
# We pin this reset/address value to 3, so `sample' swings from 3 to 255
assert sample == 3

# Booting
bootCount = zpByte() # 0 for cold boot
bootCheck = zpByte() # Checksum

# Extended output (blinkenlights in bit 0:3 and audio in but 4:7). This
# value must be present in AC during a rising hSync edge. It then gets
# copied to the XOUT register by the hardware. The XOUT register is only
# accessible in this indirect manner because it isn't part of the core
# CPU architecture.
xout      = zpByte()

# Status of blinkenlights. Keep bit 4:7 clear.
# (Simplest is to keep the sequencer responsible for that)
leds      = zpByte()

# Visible video
screenY   = zpByte() # Counts up from 0 to 238 in steps of 2
frameX    = zpByte() # Starting byte within page
frameY    = zpByte() # Page number of current pixel row (updated by videoA)
nextVideo = zpByte()
videoDorF = zpByte() # Scanline mode ('D' or 'F')

# Vertical blank, reuse some variables
blankY     = screenY # Counts down during vertical blank (44 to 0)
videoSync0 = frameX  # Vertical sync type on current line (0xc0 or 0x40)
videoSync1 = frameY  # Same during horizontal pulse

# Generic function return address
returnTo   = zpByte(2)

# Two bytes of havested entropy
# XXX Consider a larger entropy buffer
entropy    = zpByte(2)

#videoMode = zpByte()
#time0     = zpByte() # 1/60 seconds
#time1     = zpByte() # 1 seconds
#time2     = zpByte() # 256 seconds (4 minutes)
#time3     = zpByte() # 2^16 seconds (18 hours)

ledTimer        = zpByte() # Number of ticks until next LED change
ledState        = zpByte() # Current LED state
ledTempo        = zpByte() # Next value for ledTimer after LED state change
# Fow now the LED state machine itself is hard-coded in the program ROM

# Serial input (game controller)
serialInput     = zpByte()
buttonState     = zpByte() # Filtered button state

# High level interpreter
vPC     = zpByte(2)             # Interpreter program counter (points into RAM)
vAC     = zpByte(2)             # Interpreter accumulator (16-bits)

# All bytes above, except 0x80, are free for temporary/scratch/stacks etc
zpFree     = zpByte()

# XXX GCL variables start at 0x81

#-----------------------------------------------------------------------
#
#  RAM page 1: video line table
#
#-----------------------------------------------------------------------

# Byte 0-239 define the video lines
scanTablePage = 0x01       # Indirection table: Y[0] dX[0]  ..., Y[119] dX[119]

# Highest bytes are for channel 1 variables

# Sound synthesis  ch1   ch2   ch3   ch4
wavX = 249
keyL = 250
keyH = 251
     # 252 reserved for shift table
wavA = 253
oscL = 254
oscH = 255

#-----------------------------------------------------------------------
#
#  RAM page 2: shift table
#
#-----------------------------------------------------------------------

shiftTablePage = 0x02

#-----------------------------------------------------------------------
#
#  RAM page 3-7: application code GCL
#
#-----------------------------------------------------------------------

bStart = 0x0300
bTop   = 0x07ff

#-----------------------------------------------------------------------
#  Memory layout
#-----------------------------------------------------------------------

screenPages   = 0x80 - 120 # Default start of screen memory: 0x0800 to 0x7fff

#-----------------------------------------------------------------------
#  Application definitions
#-----------------------------------------------------------------------

maxTicks = 28/2 # Duration of slowest virtual opcode (ADDW)
vOverhead = 9 # Overhead of jumping in and out. Cycles, not ticks
def runVcpu(n):
  """Run interpreter for exactly n cycles"""
  print 'runVcpu %s cycles' % n
  if n % 2 != (7 + vOverhead) % 2:
    nop()
    n -= 1
  n -= 7 + 2*maxTicks + vOverhead
  assert n >= 0 and n % 2 == 0
  n /= 2
  returnPc = pc() + 7
  ld(val(returnPc&255))         #0
  st(d(returnTo))               #1
  ld(val(returnPc>>8))          #2
  st(d(returnTo+1))             #3
  ld(val(hi('ENTER')),regY)     #4
  jmpy(d(lo('ENTER')))          #5
  ld(val(n))                    #6

#-----------------------------------------------------------------------
#
#  ROM page 0: Boot
#
#-----------------------------------------------------------------------

align(0x100, 0x100)

# Give a first sign of life that can be checked with a voltmeter
ld(val(0b0000))                 # Physical: [oooo]
ld(val(syncBits^hSync), regOUT) # Prepare XOUT update, hSync goes down, RGB to black
ld(val(syncBits), regOUT)       # hSync goes up, updating XOUT

# Simple RAM test and size check by writing to [1<<n] and see if [0] changes.
ld(val(1))
label('.countMem0')
st(d(memSize), busAC|ea0DregY)
ld(val(255))
xora(d(0), busRAM|eaYDregAC)
st(d(0), busAC|eaYDregAC)       # Test if we can change and read back ok
st(d(0))                        # Preserve (inverted) memory value in [0]
xora(d(0), busRAM|eaYDregAC)
bne(d(pc()))                    # Just hang here on apparent RAM failure
ld(val(255))
xora(d(0), busRAM|eaYDregAC)
st(d(0), busAC|eaYDregAC)
xora(d(0), busRAM)
beq(d(lo('.countMem1')))        # Wrapped and [0] changed as well
ldzp(d(memSize))
bra(d(lo('.countMem0')))
adda(busAC)
label('.countMem1')

# Momentarily wait to allow for debouncing of the reset switch by spinning
# roughly 2^15 times at 2 clocks per loop: 6.5ms@10MHz to 10ms@6.3MHz
# Real-world switches normally bounce shorter than that.
# "[...] 16 switches exhibited an average 1557 usec of bouncing, with,
#  as I said, a max of 6200 usec" (From: http://www.ganssle.com/debouncing.htm)
# Relevant for the breadboard version, as the kit doesn't have a reset switch.

#ld(val(255))
#label('.debounce')
#st(d(0))
#bne(d(pc()))
#suba(val(1))
#ldzp(d(0))
#bne(d(lo('.debounce')))
#suba(val(1))

# Update LEDs (memory is present and counted, reset is stable)
ld(val(0b0001))                 # Physical: [*ooo]
ld(val(syncBits^hSync), regOUT)
ld(val(syncBits), regOUT)

# Scan the entire RAM space to collect entropy for a random number generator.
# This loop also serves as a debouncing delay for the reset button, if present.
# The 16-bit space is scanned, even if less RAM was detected.
ld(val(0))
st(d(zpFree+0), busAC|ea0DregX)
st(d(zpFree+1), busAC|ea0DregY)
label('.initEnt0')
ldzp(d(entropy+0))
bpl(d(lo('.initEnt1')))
adda(busRAM|eaYXregAC)
adda(val(191))
label('.initEnt1')
st(d(entropy+0))
ldzp(d(entropy+1))
bpl(d(lo('.initEnt2')))
adda(d(entropy+0),busRAM)
adda(val(193))
label('.initEnt2')
st(d(entropy+1))
ldzp(d(zpFree+0))
adda(val(1))
bne(d(lo('.initEnt0')))
st(d(zpFree+0), busAC|ea0DregX)
ldzp(d(zpFree+1))
adda(val(1))
bne(d(lo('.initEnt0')))
st(d(zpFree+1), busAC|ea0DregY)

# Update LEDs (debounce)
ld(val(0b0011))                  # Physical: [**oo]
ld(val(syncBits^hSync), regOUT)
ld(val(syncBits), regOUT)

# Determine if this is a cold or a warm start. We do this by checking the
# boot counter and comparing it to a simplistic checksum. The assumption
# is that after a cold start the checksum is invalid.

ldzp(d(bootCount))
adda(d(bootCheck), busRAM)
adda(d(0x5a))
bne(d(lo('cold')))
ld(val(0))
ldzp(d(bootCount))              # if warm start: bootCount += 1
adda(val(1))
label('cold')
st(d(bootCount))                # if cold start: bootCount = 0
xora(val(255))
suba(val(0x5a-1))
st(d(bootCheck))

# Initialize scan table for default video layout
ld(val(scanTablePage), regY)
ld(val(0), regX)
ld(val(screenPages))
st(eaYXregOUTIX)         # Yi  = 0x08+i
label('.initVideo')
st(val(0), eaYXregOUTIX) # dXi = 0
adda(val(1))
bge(d(lo('.initVideo'))) # stops at $80
st(eaYXregOUTIX)         # Yi  = 0x08+i
ld(d(lo('videoF')))
st(d(videoDorF))

# Init the shift2-right table for sound
ld(val(shiftTablePage), regY)
ld(val(0))
st(d(channel))
label('.loop')
st(d(zpFree+0))
adda(busAC)
adda(busAC, regX)
ldzp(d(zpFree+0))
st(eaYXregAC)
adda(val(1))
xora(val(0x40))
bne(d(lo('.loop')))
xora(val(0x40))

# Init LED sequencer
ld(val(60))
st(d(ledTimer))
ld(val(0))
st(d(ledState))
ld(val(60/5))
st(d(ledTempo))

# Setup a G-major chord to play
G3, G4, B4, D5 = 824, 1648, 2064, 2464

ld(val(1), regY) # Channel 1
ld(val(keyL), regX)
st(d(G3 & 0x7f), eaYXregOUTIX)
st(d(G3 >> 7), eaYXregAC)

ld(val(2), regY) # Channel 2
ld(val(keyL), regX)
st(d(G4 & 0x7f), eaYXregOUTIX)
st(d(G4 >> 7), eaYXregAC)

ld(val(3), regY) # Channel 3
ld(val(keyL), regX)
st(d(B4 & 0x7f), eaYXregOUTIX)
st(d(B4 >> 7), eaYXregAC)

ld(val(4), regY) # Channel 4
ld(val(keyL), regX)
st(d(D5 & 0x7f), eaYXregOUTIX)
st(d(D5 >> 7), eaYXregAC)

# Setup serial input and derived button state
ld(val(-1))
st(d(serialInput))
st(d(buttonState))

# Update LEDs (low pages are initialized)
ld(val(0b0111))                 # Physical: [***o]
ld(val(syncBits^hSync), regOUT)
ld(val(syncBits), regOUT)

# Compile test GCL program
# XXX load applications from ROM instead
program = gcl.Program(bStart)
for line in open('game.gcl').readlines():
  program.line(line)
program.end()
bLine = program.vPC
print bLine-bStart, 'GCL bytes loaded'
print bTop-bLine+1, 'GCL bytes free'

# Set start of user program ready to run
ld(val((bStart&255)-2))
st(d(vPC))
ld(val(bStart>>8))
st(d(vPC+1))

# Update LEDs (subroutines are working)
ld(val(0b1111))                 # Physical: [****]
ld(val(syncBits^hSync), regOUT)
ld(val(syncBits), regOUT)
st(d(leds)) # Setup for control by video loop
st(d(xout))

ld(d(hi('videoLoop')), busD|ea0DregY)
jmpy(d(lo('videoLoop')))
ld(val(syncBits))

#-----------------------------------------------------------------------
#
#  ROM page 1: Vertical blank part of video loop
#
#-----------------------------------------------------------------------
align(0x100, 0x100)
label('videoLoop')              # Enter vertical blank

st(d(videoSync0))               #32
ld(val(syncBits^hSync))         #33
st(d(videoSync1))               #34

# (Re)initialize carry table for robustness
st(d(0x00), ea0DregAC|busD)     #35
ld(val(0x01))                   #36
st(d(0x80), ea0DregAC|busAC)    #37

# --- Uptime clock

# XXX TODO...

# --- LED sequencer (20 cycles)

ldzp(d(ledTimer))               #38
bne(d(lo('.leds4')))            #39

ld(d(lo('.leds0')))             #40
adda(d(ledState)|busRAM)        #41
bra(busAC)                      #42
bra(d(lo('.leds1')))            #43

label('.leds0')
ld(d(0b1111))                   #44
ld(d(0b0111))                   #44
ld(d(0b0011))                   #44
ld(d(0b0001))                   #44
ld(d(0b0010))                   #44
ld(d(0b0100))                   #44
ld(d(0b1000))                   #44
ld(d(0b0100))                   #44
ld(d(0b0010))                   #44
ld(d(0b0001))                   #44
ld(d(0b0011))                   #44
ld(d(0b0111))                   #44
ld(d(0b1111))                   #44
ld(d(0b1110))                   #44
ld(d(0b1100))                   #44
ld(d(0b1000))                   #44
ld(d(0b0100))                   #44
ld(d(0b0010))                   #44
ld(d(0b0001))                   #44
ld(d(0b0010))                   #44
ld(d(0b0100))                   #44
ld(d(0b1000))                   #44
ld(d(0b1100))                   #44
ld(d(0b1110+128))               #44

label('.leds1')
st(d(leds))                     #45 Temporarily park here

bmi(d(lo('.leds2')))            #46
bra(d(lo('.leds3')))            #47
ldzp(d(ledState))               #48
label('.leds2')
ld(val(-1))                     #48
label('.leds3')
adda(val(1))                    #49
st(d(ledState))                 #50

ldzp(d(leds))                   #51 Low 4 bits are the LED output
anda(val(0b00001111))           #52
st(d(leds))                     #53
bra(d(lo('.leds5')))            #54
ldzp(d(ledTempo))               #55 Setup the LED timer for the next period

label('.leds4')
wait(54-41)                     #41
ldzp(d(ledTimer))               #54
suba(d(1))                      #55

label('.leds5')
st(d(ledTimer))                 #56

# When the total number of scanlines per frame is not an exact multiple of the (4) channels,
# there will be an audible discontinuity if no measure is taken. This static noise can be
# suppressed by swallowing the first `lines%4' partial samples after transitioning into
# vertical blank. This is easiest if the modulo is 0 (do nothing) or 1 (reset sample while in
# the first blank scanline). For the two other cases there is no solution yet: give a warning.
soundDiscontinuity = (vFront+vPulse+vBack) % 4
extra = 0
if soundDiscontinuity == 1:
  st(val(sample), ea0DregAC|busD)
  extra += 1
if soundDiscontinuity > 1:
  print "Warning: sound discontinuity not supressed"
runVcpu(198-57-extra)           #57 # Application cycles (scanline 0)

ld(val(vFront+vPulse+vBack-2))  #198 `-2' because first and last are different
st(d(blankY))                   #199
ld(d(videoSync0), busRAM|regOUT)#0

label('sound1')
ldzp(d(channel))                #1 Advance to next sound channel
anda(val(3))                    #2
adda(val(1))                    #3
ld(d(videoSync1), busRAM|regOUT)#4 Start horizontal pulse
st(d(channel), busAC|ea0DregY)  #5
ld(val(0x7f))                   #6
anda(d(oscL), busRAM|eaYDregAC) #7
adda(d(keyL), busRAM|eaYDregAC) #8
st(d(oscL), busAC|eaYDregAC)    #9
anda(val(0x80), regX)           #10
ld(busRAM|ea0XregAC)            #11
adda(d(oscH), busRAM|eaYDregAC) #12
adda(d(keyH), busRAM|eaYDregAC) #13
st(d(oscH), busAC|eaYDregAC)    #14
nop()                           #15 Was: xora [y,wavX]
nop()                           #16 Was: adda [y,wavA]
anda(val(0b11111100),regX)      #17
ld(d(shiftTablePage), regY)     #18
ld(busRAM|eaYXregAC)            #19
adda(d(sample), busRAM|ea0DregAC)#20
st(d(sample))                   #21
wait(26-22)                     #22
ldzp(d(xout))                   #26
nop()                           #27
ld(d(videoSync0), busRAM|regOUT)#28 End horizontal pulse

# Count down the vertical blank interval until its last scan line
ldzp(d(blankY))                 #29
beq(d(lo('vBlankLast0')))       #30
suba(d(1))                      #31
st(d(blankY))                   #32

# Determine if we're in the vertical sync pulse
suba(d(vBack-1))                #33
bne(d(lo('vSync0')))            #34 Tests for end of vPulse
suba(d(vPulse))                 #35
ld(val(syncBits))               #36 Entering vertical back porch
bra(d(lo('vSync2')))            #37
st(d(videoSync0))               #38
label('vSync0')
bne(d(lo('vSync1')))            #36 Tests for start of vPulse
ld(val(syncBits^vSync))         #37
bra(d(lo('vSync3')))            #38 Entering vertical sync pulse
st(d(videoSync0))               #39
label('vSync1')
ldzp(d(videoSync0))             #38 Load current value
label('vSync2')
nop()                           #39
label('vSync3')
xora(d(hSync))                  #40 Precompute, as during the pulse there is no time
st(d(videoSync1))               #41

# Capture the serial input
ldzp(d(blankY))                 #42
xora(val(vBack-1-1))            #43 Exactly when the 74HC595 has captured all 8 controller bits
bne(d(lo('.ser0')))             #44
bra(d(lo('.ser1')))             #45
st(d(serialInput),busIN)        #46
label('.ser0')
nop()                           #46
label('.ser1')

# Update [xout] with the next sound sample every 4 scan lines.
# Keep doing this on 'videoC equivalent' scan lines in vertical blank.
ldzp(d(blankY))                 #47
anda(d(3))                      #48
bne(d(lo('vBlankRegular')))     #49
ldzp(d(sample))                 #50
anda(d(0xf0))                   #51
ora(d(leds), busRAM|ea0DregAC)  #52
st(d(xout))                     #53
st(val(sample), ea0DregAC|busD) #54 Reset for next sample

runVcpu(199-55)                 #55 Appplication cycles (scanline 1-43 with sample update)
bra(d(lo('sound1')))            #199
ld(d(videoSync0), busRAM|regOUT)#0 # Ends the vertical blank pulse at the right cycle

label('vBlankRegular')
runVcpu(199-51)                 #51 Application cycles (scanline 1-43 without sample update)
bra(d(lo('sound1')))            #199
ld(d(videoSync0), busRAM|regOUT)#0 Ends the vertical blank pulse at the right cycle

# Last blank line before transfering to visible area
label('vBlankLast0')
ld(val(0))                      #32
st(d(frameX))                   #33
st(d(nextVideo))                #34

label('vBlankLast1')

# The serial game controller freaks out when two buttons are pressed: it just sends zeroes.
# When we see this, preserve the old value and assume buttonA was added.
ldzp(d(serialInput))            #35
beq(lo('.multi0'))              #36
bra(lo('.multi1'))              #37
st(d(buttonState),busAC)        #38
label('.multi0')
ld(val(buttonA))                #38
label('.multi1')
ora(d(buttonState),busRAM)      #39
st(d(buttonState),busAC)        #40

# --- Switch video mode when (only) select is pressed
ldzp(d(buttonState))            #41
xora(val(~buttonSelect))        #42
beq(d(lo('.sel0')))             #43
bra(d(lo('.sel1')))             #44
ld(val(0))                      #45
label('.sel0')
ld(val(lo('videoD')^lo('videoF')))#45
label('.sel1')
xora(d(videoDorF),busRAM)       #46
st(d(videoDorF))                #47

runVcpu(199-48)                 #48 Application cycles (scanline 44)
ldzp(d(channel))                #199 Advance to next sound channel
anda(val(3))                    #0
adda(val(1))                    #1
ld(d(hi('sound2')), busD|ea0DregY)#2
jmpy(d(lo('sound2')))           #3
ld(val(syncBits^hSync), regOUT) #4 Start horizontal pulse

#-----------------------------------------------------------------------
#
#  ROM page 2: Visible part of video loop
#
#-----------------------------------------------------------------------
align(0x100, 0x100)
label('visiblePage')

# Back porch A: first of 4 repeated scanlines
# - Fetch next Yi and store it for retrieval in the next scanlines
# - Calculate Xi from dXi, but there is no cycle time left to store it as well
label('videoA')
assert(lo('videoA') == 0)       # videoA starts at the page boundary
ld(d(lo('videoB')))             #29
st(d(nextVideo))                #30
ld(d(scanTablePage), regY)      #31
ld(d(screenY), busRAM|regX)     #32
ld(eaYXregAC, busRAM)           #33
st(eaYXregOUTIX)                #34 # Just to increment X
st(d(frameY))                   #35
ld(eaYXregAC, busRAM)           #36
adda(d(frameX), busRAM|regX)    #37
ld(d(frameY), busRAM|regY)      #38
ld(val(syncBits))               #39

# Stream 160 pixels from memory location <Yi,Xi> onwards
# Superimpose the sync signal bits to be robust against misprogramming
label('pixels')
for i in range(160):           
  ora(eaYXregOUTIX, busRAM)     #40-199
ld(val(syncBits), regOUT)       #0 Back to black

# Front porch
ldzp(d(channel))                #1 Advance to next sound channel
label('soundF')
anda(val(3))                    #2
adda(val(1))                    #3
ld(val(syncBits^hSync), regOUT) #4 Start horizontal pulse

# Horizontal sync
label('sound2')
st(d(channel), busAC|ea0DregY)  #5 Sound
ld(val(0x7f))                   #6
anda(d(oscL), busRAM|eaYDregAC) #7
adda(d(keyL), busRAM|eaYDregAC) #8
st(d(oscL), busAC|eaYDregAC)    #9
anda(val(0x80), regX)           #10
ld(busRAM|ea0XregAC)            #11
adda(d(oscH), busRAM|eaYDregAC) #12
adda(d(keyH), busRAM|eaYDregAC) #13
st(d(oscH), busAC|eaYDregAC)    #14
nop()                           #15 Was: xora [y,wavX]
nop()                           #16 Was: adda [y,wavA]
anda(val(0xfc),regX)            #17
ld(d(shiftTablePage), regY)     #18
ld(busRAM|eaYXregAC)            #19
adda(d(sample), busRAM|ea0DregAC)#20
st(d(sample))                   #21
wait(26-22)                     #22
ldzp(d(xout))                   #26
bra(d(nextVideo) | busRAM)      #27
ld(val(syncBits), regOUT)       #28 End horizontal pulse

# Back porch B: second of 4 repeated scanlines
# - Recompute Xi from dXi and store for retrieval in the next scanlines
label('videoB')
ld(d(lo('videoC')))             #29
st(d(nextVideo))                #30
ld(d(scanTablePage), regY)      #31
ldzp(d(screenY))                #32
adda(d(1), regX)                #33
ldzp(d(frameX))                 #34
adda(eaYXregAC, busRAM)         #35
st(d(frameX), busAC|ea0DregX)   #36 Undocumented opcode "store in RAM and X"!
ld(d(frameY), busRAM|regY)      #37
bra(d(lo('pixels')))            #38
ld(val(syncBits))               #39

# Back porch C: third of 4 repeated scanlines
# - Nothing new to do, Yi and Xi are known
label('videoC')
ldzp(d(sample))                 #29 First something that didn't fit in the audio loop
anda(d(0xf0))                   #30
ora(d(leds), busRAM|ea0DregAC)  #31
st(d(xout))                     #32 Update [xout] with new sample (4 channels just updated)
st(val(sample), ea0DregAC|busD) #33 Reset for next sample
ldzp(d(videoDorF))              #34 Now back to video business
st(d(nextVideo))                #35
ld(d(frameX), busRAM|regX)      #36
ld(d(frameY), busRAM|regY)      #37
bra(d(lo('pixels')))            #38
ld(val(syncBits))               #39

# Back porch D: last of 4 repeated scanlines
# - Calculate the next frame index
# - Decide if this is the last line or not
label('videoD')                 # Default video mode
ld(d(frameX), busRAM|regX)      #29
ldzp(d(screenY))                #30
suba(d((120-1)*2))              #31
beq(d(lo('last')))              #32
ld(d(frameY), busRAM|regY)      #33
adda(d(120*2))                  #34 # More pixel lines to go
st(d(screenY))                  #35
ld(d(lo('videoA')))             #36
st(d(nextVideo))                #37
bra(d(lo('pixels')))            #38
ld(val(syncBits))               #39
label('last')
wait(36-34)                     #34 # No more pixel lines
ld(d(lo('videoE')))             #36
st(d(nextVideo))                #37
bra(d(lo('pixels')))            #38
ld(val(syncBits))               #39

# Back porch "E": after the last line
# - Go back to program page 0 and enter vertical blank
label('videoE') # Exit visible area
ld(d(hi('videoLoop')),ea0DregY) #29
jmpy(d(lo('videoLoop')))        #30
ld(val(syncBits))               #31

# Back porch "F": scanlines and fast mode
label('videoF')                 # Fast video mode
ldzp(d(screenY))                #29
suba(d((120-1)*2))              #30
bne(d(lo('notlast')))           #31
adda(d(120*2))                  #32
bra(d(lo('.join')))             #33
ld(d(lo('videoE')))             #34 No more visible lines
label('notlast')
st(d(screenY))                  #33 More visible lines
ld(d(lo('videoA')))             #34
label('.join')
st(d(nextVideo))                #35
runVcpu(199-36)                 #36 Application (every 4th of scanlines 45-524)
ld(d(hi('soundF')), busD|ea0DregY)#199
jmpy(d(lo('soundF')))           #0
ldzp(d(channel))                #1 Advance to next sound channel

#-----------------------------------------------------------------------
#
#  ROM page 4: Application interpreter
#
#-----------------------------------------------------------------------
align(0x100,0x100)
#-----------------------------------------------------------------------

vTicks  = zpFree                # Interpreter ticks are units of 2 clocks
vTmp    = zpFree+1

#-----------------------------------------------------------------------

#
# Enter the timing-aware application interpreter (aka virtual CPU, vCPU)
#
# This routine will execute as many as possible instructions in the
# alotted time. When time runs out, it synchronizes such that the total
# duration matches the caller's request. Durations are counted in `ticks',
# which are multiples of 2 clock cycles.
#
# Use the runVcpu() macro as entry point
#
label('ENTER')
bra(d(lo('.next2')))            #0 Enter at '.next2' (so no startup overhead)
ld(d(vPC+1),busRAM|regY)        #1

label('next14')
st(d(vAC))                      #14
ld(val(-16/2))                  #15
# Fetch next instruction and execute it, but only if there are sufficient
# ticks left for the slowest instruction.
label('NEXT')
adda(d(vTicks),busRAM)          #0 Actually counting down (AC<0)
blt(d(lo('RETURN')))            #1
label('.next2')
st(d(vTicks))                   #2
ldzp(d(vPC))                    #3 Advance vPC
adda(val(2))                    #4
st(d(vPC),busAC|ea0DregX)       #5
ld(busRAM|eaYXregAC)            #6 Fetch opcode (actually a branch target)
st(eaYXregOUTIX)                #7 Just to increment X
bra(busAC)                      #8 Execute opcode
ld(busRAM|eaYXregAC)            #9 Prefetch operand

# Resync with caller and return
label('RETURN')
adda(val(maxTicks))             #3
bgt(d(pc()))                    #4
suba(val(1))                    #5
ld(d(returnTo+1),busRAM|regY)   #6
jmpy(d(returnTo+0)|busRAM)      #7
nop()                           #8
assert vOverhead ==              9

# Instruction LDI: Load immediate constant (AC=$DD), 16 cycles
label('LDI')
st(d(vAC))                      #10
ld(val(0))                      #11
st(d(vAC+1))                    #12
ld(val(-16/2))                  #13
bra(d(lo('NEXT')))              #14
nop()                           #15

# Instruction LDWI: Load immediate constant (AC=$DD), 20 cycles
label('LDWI')
st(d(vAC))                      #10
st(eaYXregOUTIX)                #11 Just to increment X
ld(busRAM|eaYXregAC)            #12 Fetch second operand
st(d(vAC+1))                    #13
ldzp(d(vPC))                    #14 Advance vPC one more
adda(val(1))                    #15
st(d(vPC))                      #16
ld(val(-20/2))                  #17
bra(d(lo('NEXT')))              #18
#nop()                          #(19)
#
# Instruction LD: Load from zero page (AC=[D]), 18 cycles
label('LD')
ld(busAC,regX)                  #10 (overlap with LDWI)
ldzp(busRAM|ea0XregAC)          #11
st(d(vAC))                      #12
ld(val(0))                      #13
st(d(vAC+1))                    #14
ld(val(-18/2))                  #15
bra(d(lo('NEXT')))              #16
#nop()                          #(17)
#
# Instruction LDW: Word load from zero page (AC=[D],[D+1]), 20 cycles
label('LDW')
ld(busAC,regX)                  #10 (overlap with LD)
adda(val(1))                    #11
st(d(vTmp))                     #12 Address of high byte
ld(busRAM|ea0XregAC)            #13
st(d(vAC))                      #14
ld(d(vTmp),busRAM|regX)         #15
ld(busRAM|ea0XregAC)            #16
st(d(vAC+1))                    #17
bra(d(lo('NEXT')))              #18
ld(val(-20/2))                  #19

# Instruction STW: Word load from zero page (AC=[D],[D+1]), 20 cycles
label('STW')
ld(busAC,regX)                  #10
adda(val(1))                    #11
st(d(vTmp))                     #12 Address of high byte
ldzp(d(vAC))                    #13
st(ea0XregAC)                   #14
ld(d(vTmp),busRAM|regX)         #15
ldzp(d(vAC+1))                  #16
st(ea0XregAC)                   #17
bra(d(lo('NEXT')))              #18
ld(val(-20/2))                  #19

# Instruction SIGNW: Test signedness of word (0xffff,0,1), 24 cycles
label('SIGNW')
ldzp(d(vPC))                    #10 Swallow operand
suba(val(1))                    #11
st(d(vPC))                      #12
ldzp(d(vAC+1))                  #13 First inspect high byte ACH
bne(d(lo('.testw3')))           #14
bmi(d(lo('.testw4')))           #15
st(d(vAC+1))                    #16 Clear ACH
ldzp(d(vAC))                    #17 Additionally inspect low byte ACL
bne(d(lo('.testw1')))           #18
bra(d(lo('.testw2')))           #19
label('.testw0')
ld(val(0))                      #20 ACH==0 and ACL==0
label('.testw1')
ld(val(1))                      #20 ACH==0 and ACL!=0
label('.testw2')
st(d(vAC))                      #21
bra(d(lo('NEXT')))              #22
ld(val(-24/2))                  #23
label('.testw3')
ld(val(0))                      #16 ACH>0
bra(d(lo('.testw0')))           #17 testw0 is labeled 20, but from here it is 19
st(d(vAC+1))                    #18
label('.testw4')
ld(val(-1))                     #17 ACH<0
st(d(vAC+1))                    #18
bra(d(lo('.testw2')))           #19
nop()                           #20

# Instruction BEQ: Branch if zero (if(ALC==0)PCL=D), 16 cycles
label('BEQ')
ldzp(d(vAC))                    #10
bne(d(lo('br1')))               #11
ld(busRAM|eaYXregAC)            #12
label('br0')
st(d(vPC))                      #13
bra(d(lo('NEXT')))              #14
#ld(val(-16/2))                 #(15)
label('br1')
ld(val(-16/2))                  #13
bra(d(lo('NEXT')))              #14
#nop()                          #(15)
#
# Instruction ST: Store in zero page ([D]=ACL), 16 cycles
label('ST')
ld(busAC,regX)                  #10 (overlap with BEQ)
ldzp(d(vAC))                    #11
bra(d(lo('next14')))            #12
st(d(vAC),busAC|ea0XregAC)      #13

# Instruction BNE: Branch if not zero (if(ALC!=0)PCL=D), 16 cycles
label('BNE')
ldzp(d(vAC))                    #10
bne(d(lo('br0')))               #11
ld(busRAM|eaYXregAC)            #12
ld(val(-16/2))                  #13
bra(d(lo('NEXT')))              #14
#nop()                          #(15)
#
# Instruction AND: Logical-AND with zero page (ACL&=[D]), 16 cycles
label('AND')
ld(busAC,regX)                  #10 (overlap with BNE)
ldzp(d(vAC))                    #11
bra(d(lo('next14')))            #12
anda(busRAM,ea0XregAC)          #13

# Instruction ANDI: Logical-AND with constant (AC&=D), 16 cycles
label('ANDI')
anda(d(vAC),busRAM)             #10
st(d(vAC))                      #11
ld(val(0))                      #12 Clear high byte
st(d(vAC+1))                    #13
bra(d(lo('NEXT')))              #14
ld(val(-16/2))                  #15

# Instruction ORI: Logical-OR with constant (AC|=D), 14 cycles
label('ORI')
ora(d(vAC),busRAM)              #10
st(d(vAC))                      #11
bra(d(lo('NEXT')))              #12
ld(val(-14/2))                  #13

# Instruction XORI: Logical-XOR with constant (AC^=D), 14 cycles
label('XORI')
xora(d(vAC),busRAM)             #10
st(d(vAC))                      #11
bra(d(lo('NEXT')))              #12
ld(val(-14/2))                  #13

# Instruction BGT: Branch if positive (if(ALC>0)PCL=D), 16 cycles
label('BGT')
ldzp(d(vAC))                    #10
bgt(d(lo('br0')))               #11
ld(busRAM|eaYXregAC)            #12
ld(val(-16/2))                  #13
bra(d(lo('NEXT')))              #14
#nop()                          #(15)
#
# Instruction OR: Logical-OR with zero page (ACL|=[D]), 16 cycles
label('OR')
ld(busAC,regX)                  #10 (overlap with BGT)
ldzp(d(vAC))                    #11
bra(d(lo('next14')))            #12
ora(busRAM,ea0XregAC)           #13

# Instruction BLT: Branch if negative (if(ALC<0)PCL=D), 16 cycles
label('BLT')
ldzp(d(vAC))                    #10
blt(d(lo('br0')))               #11
ld(busRAM|eaYXregAC)            #12
ld(val(-16/2))                  #13
bra(d(lo('NEXT')))              #14
#nop()                          #(15)
#
# Instruction XOR: Logical-XOR with zero page (ACL^=[D]), 16 cycles
label('XOR')
ld(busAC,regX)                  #10 (overlap with BLT)
ldzp(d(vAC))                    #11
bra(d(lo('next14')))            #12
xora(busRAM,ea0XregAC)          #13

# Instruction ADDI: Add immediate (ACL+=D), 14 cycles
label('ADDI')
adda(d(vAC),busRAM)             #10
st(d(vAC))                      #11
bra(d(lo('NEXT')))              #12
ld(val(-14/2))                  #13

# Instruction BRA: Branch unconditionally (PCL=D), 14 cycles
label('BRA')
st(d(vPC))                      #10
ld(val(-14/2))                  #11
bra(d(lo('NEXT')))              #12
nop()                           #13

# Instruction BGE: Branch if positive or zero (if(ALC>=0)PCL=D), 16 cycles
label('BGE')
ldzp(d(vAC))                    #10
bge(d(lo('br0')))               #11
ld(busRAM|eaYXregAC)            #12
ld(val(-16/2))                  #13
bra(d(lo('NEXT')))              #14
#nop()                          #(15)
#
# Instruction ADD: Addition with zero page (ACL+=[D]), 16 cycles
label('ADD')
ld(busAC,regX)                  #10 (overlap with BGE)
ldzp(d(vAC))                    #11
bra(d(lo('next14')))            #12
adda(busRAM,ea0XregAC)          #13

# Instruction BLE: Branch if negative or zero (if(ALC<=0)PCL=D), 16 cycles
label('BLE')
ldzp(d(vAC))                    #10
ble(d(lo('br0')))               #11
ld(busRAM|eaYXregAC)            #12
ld(val(-16/2))                  #13
bra(d(lo('NEXT')))              #14
#nop()                          #(15)
#
# Instruction SUB: Subtraction with zero page (ACL-=[D]), 16 cycles
label('SUB')
ld(busAC,regX)                  #10 (overlap with BLE)
ldzp(d(vAC))                    #11
bra(d(lo('next14')))            #12
suba(busRAM,ea0XregAC)          #13

# Instruction ADDW: Word addition with zero page (AC+=[D]+256*[D+1]), 28 cycles
label('ADDW')
# The non-carry paths could be 26 cycles at the expense of (much) more code.
# But a smaller size is better so more instructions fit in this code page.
# 28 cycles is still 4.5 usec. The 6502 equivalent takes 20 cycles or 20 usec.
ld(busAC,regX)                  #10 Address of low byte to be added
adda(val(1))                    #11
st(d(vTmp))                     #12 Address of high byte to be added
ldzp(d(vAC))                    #13 Add the low bytes
adda(busRAM|ea0XregAC)          #14
st(d(vAC))                      #15 Store low result
bmi(d(lo('.addw0')))            #16 Now figure out if there was a carry
suba(busRAM|ea0XregAC)          #17 Gets back the initial value of vAC
bra(d(lo('.addw1')))            #18
ora(busRAM|ea0XregAC)           #19 Bit 7 is our lost carry
label('.addw0')
anda(busRAM|ea0XregAC)          #18 Bit 7 is our lost carry
nop()                           #19
label('.addw1')
anda(val(0x80),regX)            #20 Move the carry to bit 0 (0 or +1)
ld(busRAM,ea0XregAC)            #21
adda(d(vAC+1),busRAM)           #22 Add the high bytes with carry
ld(d(vTmp),busRAM|regX)         #23
adda(busRAM|ea0XregAC)          #24
st(d(vAC+1))                    #25 Store high result
bra(d(lo('NEXT')))              #26
ld(val(-28/2))                  #27

# Instruction SUBW: Word subtraction with zero page (AC-=[D]+256*[D+1]), 28 cycles
label('SUBW')
ld(busAC,regX)                  #10 Address of low byte to be subtracted
adda(val(1))                    #11
st(d(vTmp))                     #12 Address of high byte to be subtracted
ldzp(d(vAC))                    #13
bmi(d(lo('.subw0')))            #14
suba(busRAM|ea0XregAC)          #15
st(d(vAC))                      #16 Store low result
bra(d(lo('.subw1')))            #17
ora(busRAM|ea0XregAC)           #18 Bit 7 is our lost carry
label('.subw0')
st(d(vAC))                      #16 Store low result
anda(busRAM|ea0XregAC)          #17 Bit 7 is our lost carry
nop()                           #18
label('.subw1')
anda(val(0x80),regX)            #19 Move the carry to bit 0
ldzp(d(vAC+1))                  #20
suba(busRAM,ea0XregAC)          #21
ld(d(vTmp),busRAM|regX)         #22
suba(busRAM|ea0XregAC)          #23
st(d(vAC+1))                    #24
nop()                           #25
bra(d(lo('NEXT')))              #26
ld(val(-28/2))                  #27

# Instruction PEEK (AC=[[D+1],[D]]), 24 cycles
label('PEEK')
st(d(vTmp))                     #10
adda(val(1),regX)               #11
ld(busRAM,ea0XregAC)            #12
ld(busAC,regY)                  #13
ld(d(vTmp),busRAM|regX)         #14
ld(busRAM,ea0XregAC)            #15
ld(busAC,regX)                  #16
ld(busRAM|eaYXregAC)            #17
st(d(vAC))                      #18
ld(val(0))                      #19
st(d(vAC+1))                    #20
ld(d(vPC+1),busRAM|regY)        #21
bra(d(lo('NEXT')))              #22
ld(val(-24/2))                  #23

# Instruction POKE ([[D+1],[D]] = ACL), 22 cycles
label('POKE')
st(d(vTmp))                     #10
adda(val(1),regX)               #11
ld(busRAM,ea0XregAC)            #12
ld(busAC,regY)                  #13
ld(d(vTmp),busRAM|regX)         #14
ld(busRAM,ea0XregAC)            #15
ld(busAC,regX)                  #16
ldzp(d(vAC))                    #17
st(eaYXregAC)                   #18
ld(d(vPC+1),busRAM|regY)        #19
bra(d(lo('NEXT')))              #20
ld(val(-22/2))                  #21

# Instruction LOOKUP, 28 cycles
label('LOOKUP')
st(d(vTmp))                     #10
adda(val(1),regX)               #11
ld(busRAM,ea0XregAC)            #12
ld(busAC,regY)                  #13
ld(d(vTmp),busRAM|regX)         #14
jmpy(d(0))                      #15
ld(busRAM,ea0XregAC)            #16
label('.lookup0')
st(d(vAC))                      #23
ld(val(0))                      #24
st(d(vAC+1))                    #25
bra(d(lo('NEXT')))              #26
ld(val(-28/2))                  #27

#init_rng(s1,s2,s3) //Can also be used to seed the rng with more entropy during use.
#{
#//XOR new entropy into key state
#a ^=s1;
#b ^=s2;
#c ^=s3;

#x++;
#a = (a^c^x);
#b = (b+a);
#c = (c+(b>>1)^a);
#}

#unsigned char randomize()
#{
#x++;               //x is incremented every round and is not affected by any other variable
#a = (a^c^x);       //note the mix of addition and XOR
#b = (b+a);         //And the use of very few instructions
#c = (c+(b>>1)^a);  //the right shift is to ensure that high-order bits from b can affect  
#return(c)          //low order bits of other variables
#}

#-----------------------------------------------------------------------
#
#  ROM page 5-6: Gigatron font data
#
#-----------------------------------------------------------------------
align(0x100)

align(0x100, 0x100)
bra(busAC)                    #17
bra(val(2))                   #18
ld(d(hi('.lookup0')),regY)    #20
jmpy(d(lo('.lookup0')))       #21
ld(d(vPC+1),busRAM|regY)      #22

for ch in range(32, 64):
  for byte in font.font[ch-32]:
    ld(val(byte))

#-----------------------------------------------------------------------
# Finish assembly
#-----------------------------------------------------------------------
end()
